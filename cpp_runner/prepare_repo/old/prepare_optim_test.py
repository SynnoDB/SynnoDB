import sys
import tempfile
import unittest
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent))

from cpp_runner.prepare_repo.old.prepare_optim import prepare_repo_for_optim

QUERY_IMPL_FILENAME = "query_impl.cpp"


def _run(tmp_path: Path, query_impl_src: str) -> str:
    """Write query_impl.cpp, run prepare_repo_for_optim, return modified contents."""
    (tmp_path / QUERY_IMPL_FILENAME).write_text(query_impl_src)
    prepare_repo_for_optim(tmp_path, QUERY_IMPL_FILENAME)
    return (tmp_path / QUERY_IMPL_FILENAME).read_text()


class TestPrepareOptim(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def run_src(self, src: str) -> str:
        return _run(self.tmp_path, src)

    def assert_instrumented(self, result: str) -> None:
        lines = result.splitlines()
        reset_idx = next(i for i, line in enumerate(lines) if "TRACE_RESET()" in line)
        flush_idx = next(i for i, line in enumerate(lines) if "TRACE_FLUSH()" in line)
        self.assertLess(reset_idx, flush_idx)
        self.assertEqual(lines[flush_idx + 1].strip(), "}")

    def test_basic(self):
        src = """\
void query(Database* db) {
    do_work();
}
"""
        result = self.run_src(src)
        self.assertIn("TRACE_RESET();", result)
        self.assertIn("TRACE_FLUSH();", result)
        self.assert_instrumented(result)

    def test_brace_on_next_line(self):
        src = """\
void query(Database* db)
{
    do_work();
}
"""
        self.assert_instrumented(self.run_src(src))

    def test_nested_braces(self):
        """Brace counting must skip nested {} inside the function body."""
        src = """\
void query(Database* db) {
    if (x) {
        for (int i = 0; i < n; ++i) {
            process(i);
        }
    } else {
        fallback();
    }
}
"""
        self.assert_instrumented(self.run_src(src))

    def test_function_not_last_in_file(self):
        """TRACE_FLUSH must go before query()'s }, not the file's last }."""
        src = """\
void helper() {
    // some other function before query
}

void query(Database* db) {
    do_work();
}

void after() {
    // another function after query
}
"""
        result = self.run_src(src)
        self.assert_instrumented(result)
        # after() must still be intact after query's closing brace
        lines = result.splitlines()
        flush_idx = next(i for i, line in enumerate(lines) if "TRACE_FLUSH()" in line)
        tail = "\n".join(lines[flush_idx:])
        self.assertIn("after()", tail)

    def test_reset_is_first_statement(self):
        """TRACE_RESET() must be the very first statement in the function."""
        src = """\
void query(Database* db) {
    do_work();
}
"""
        result = self.run_src(src)
        lines = result.splitlines()
        open_idx = next(
            i for i, line in enumerate(lines) if "void query(" in line and "{" in line
        )
        self.assertIn("TRACE_RESET()", lines[open_idx + 1])


if __name__ == "__main__":
    unittest.main()
