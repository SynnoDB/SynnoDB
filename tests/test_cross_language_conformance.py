"""The anti-divergence gate: the C++ and Rust runtimes must agree value-for-value.

`cpp_helpers/column_ingest.hpp` + `column_egress.hpp` and `synno_rt::{ingest,
egress}` are two implementations of one contract. Nothing makes them agree
except this test. A divergence between them does not fail a build and does not
crash an engine -- it produces an engine that is quietly wrong on some queries
(a decimal that rounds where the other truncates, a NULL read as 0), which is
exactly the failure mode the exactness gate exists to prevent, arriving through
the back door.

So the cases live HERE, once, as data, and both runtimes are graded against
them: `tests/cpp/column_ingest_test.cpp` and `tests/rust/conformance` print the
same ``key=value`` line for the same mode, and this test asserts the two lines
are identical and that the values are right.

Adding a case: add it to both drivers with the same key name. If a key exists in
only one, `test_runtimes_print_the_same_keys` fails -- otherwise the two drivers
could drift apart while every assertion still passed.

Skips when a toolchain (C++/Arrow, or cargo) is unavailable.
"""

from __future__ import annotations

import datetime
import decimal
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CPP_HELPERS = REPO / "src" / "synnodb" / "cpp_runner" / "cpp_helpers"
CPP_DRIVER = REPO / "tests" / "cpp" / "column_ingest_test.cpp"
CPP_SHM_DRIVER = REPO / "tests" / "cpp" / "shm_io_test.cpp"
RUST_DRIVER_DIR = REPO / "tests" / "rust" / "conformance"

LANGS = ("cpp", "rust")


# ---------------------------------------------------------------- toolchains --
def _cpp_ok() -> bool:
    if not (shutil.which("g++") or shutil.which("clang++")):
        return False
    return shutil.which("pkg-config") is not None and (
        subprocess.run(["pkg-config", "--exists", "arrow", "parquet"]).returncode == 0
    )


def _rust_ok() -> bool:
    return shutil.which("cargo") is not None


@pytest.fixture(scope="module")
def drivers(tmp_path_factory) -> dict[str, Path]:
    """Both conformance drivers, built. Skips the language whose toolchain is absent."""
    out: dict[str, Path] = {}

    if _cpp_ok():
        cc = "g++" if shutil.which("g++") else "clang++"
        cflags = subprocess.run(
            ["pkg-config", "--cflags", "arrow", "parquet"],
            capture_output=True,
            text=True,
        ).stdout.split()
        libs = subprocess.run(
            ["pkg-config", "--libs", "arrow", "parquet"], capture_output=True, text=True
        ).stdout.split()
        binary = tmp_path_factory.mktemp("cpp") / "column_conformance"
        proc = subprocess.run(
            [
                cc,
                "-std=c++20",
                "-O2",
                "-I",
                str(CPP_HELPERS),
                *cflags,
                str(CPP_DRIVER),
                *libs,
                "-o",
                str(binary),
            ],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            pytest.fail(f"C++ conformance driver failed to compile:\n{proc.stderr}")
        out["cpp"] = binary

    if _rust_ok():
        proc = subprocess.run(
            ["cargo", "build", "--release"],
            cwd=RUST_DRIVER_DIR,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            pytest.fail(f"Rust conformance driver failed to build:\n{proc.stderr}")
        out["rust"] = RUST_DRIVER_DIR / "target" / "release" / "column_conformance"

    if not out:
        pytest.skip("no C++ (Arrow) and no Rust toolchain")
    return out


@pytest.fixture(scope="module")
def synth_parquet(tmp_path_factory) -> Path:
    """The long tail a hand-written type switch misses: BOOLEAN, DICTIONARY-encoded
    string, TIMESTAMP, decimal. Both runtimes read this same file."""
    pa = pytest.importorskip("pyarrow")
    import pyarrow.parquet as pq

    table = pa.table(
        {
            "dec_col": pa.array(
                [
                    decimal.Decimal("1.50"),
                    decimal.Decimal("2.25"),
                    decimal.Decimal("0.00"),
                    decimal.Decimal("10.10"),
                ],
                pa.decimal128(15, 2),
            ),
            "int_col": pa.array([10, 20, 30, 40], pa.int32()),
            "bool_col": pa.array([True, False, True, True]),
            "dict_col": pa.array(["A", "B", "A", "C"]).dictionary_encode(),
            "ts_col": pa.array(
                [
                    datetime.datetime(2023, 1, 2, 5),
                    datetime.datetime(2023, 1, 2, 23),
                    datetime.datetime(2024, 6, 1),
                    datetime.datetime(2020, 12, 31),
                ],
                pa.timestamp("us"),
            ),
            "date_col": pa.array(
                [
                    datetime.date(2023, 1, 2),
                    datetime.date(2023, 1, 2),
                    datetime.date(2024, 6, 1),
                    datetime.date(2020, 12, 31),
                ],
                pa.date32(),
            ),
            "dbl_col": pa.array([1.5, 2.5, 3.0, 4.0], pa.float64()),
        }
    )
    path = tmp_path_factory.mktemp("synth") / "synth.parquet"
    pq.write_table(table, path)
    return path


# ------------------------------------------------------------------- helpers --
def _run(driver: Path, *args: str) -> str:
    proc = subprocess.run(
        [str(driver), *args], capture_output=True, text=True, timeout=600
    )
    assert proc.returncode == 0, f"{driver.name} {args} failed:\n{proc.stderr}"
    return proc.stdout


def _keys(stdout: str) -> dict[str, float | int]:
    """The first line's key=value pairs. Both drivers print the shared contract
    there; a driver may print extra lines below it for its own extra coverage."""
    first = stdout.strip().splitlines()[0]
    return {
        k: (int(v) if v.lstrip("-").isdigit() else float(v))
        for k, v in re.findall(r"(\w+)=(-?[\d.]+)", first)
    }


# --------------------------------------------------------------------- tests --
@pytest.mark.parametrize("lang", LANGS)
def test_nullable_validity_roundtrip(drivers, lang):
    """A NULL captured on ingest is re-emitted as a real Arrow NULL on egress -- not
    silently turned into 0 -- while the default dense path still reads it as 0.
    This is the mechanism a generated engine uses to honour SQL null semantics."""
    if lang not in drivers:
        pytest.skip(f"no {lang} toolchain")
    out = _keys(_run(drivers[lang], "nullable"))
    assert out["validity"] == 101  # [valid, NULL, valid]
    assert out["dense1"] == 0  # default path reads the NULL as 0
    assert out["egress_nulls"] == 1  # exactly one real Arrow NULL emitted
    assert out["egress_isnull1"] == 1  # at the NULL row
    assert out["egress_isnull0"] == 0  # and only there


@pytest.mark.parametrize("lang", LANGS)
def test_diverse_types(drivers, lang, synth_parquet):
    """BOOLEAN, DICTIONARY-string, TIMESTAMP, decimal -- via delegation to Arrow's cast."""
    if lang not in drivers:
        pytest.skip(f"no {lang} toolchain")
    out = _keys(_run(drivers[lang], "synth", str(synth_parquet)))
    assert out["dec"] == 1385  # (1.50+2.25+0.00+10.10) * 10^2, exact
    assert out["dec16"] == 1385  # same value narrowed to i16 -- no truncation
    assert out["int"] == 100
    assert out["bool"] == 3
    assert out["dictA"] == 2  # dictionary-encoded string decoded, not read as a code
    assert out["ts0"] == 19359  # TIMESTAMP -> days since epoch (2023-01-02)
    assert out["date0"] == 19359
    assert out["dbl"] == pytest.approx(11.0)


def test_runtimes_agree_on_diverse_types(drivers, synth_parquet):
    """The gate. Both runtimes, same input, identical output."""
    if len(drivers) < 2:
        pytest.skip("need both toolchains to compare them")
    cpp = _keys(_run(drivers["cpp"], "synth", str(synth_parquet)))
    rust = _keys(_run(drivers["rust"], "synth", str(synth_parquet)))
    assert cpp == rust


def test_runtimes_agree_on_nullable(drivers):
    if len(drivers) < 2:
        pytest.skip("need both toolchains to compare them")
    assert _keys(_run(drivers["cpp"], "nullable")) == _keys(
        _run(drivers["rust"], "nullable")
    )


def _lineitem() -> Path | None:
    """Real TPC-H lineitem, if this machine has the dataset."""
    from synnodb import settings

    try:
        data_dir = settings.get_data_dir()
    except RuntimeError:
        # No data dir configured (SYNNO_DATA_DIR unset) -> no dataset to test against.
        return None

    for sf in ("sf1", "sf2", "sf5"):
        p = data_dir / "workloads" / "tpch" / "tpch_parquet" / sf / "lineitem.parquet"
        if p.exists():
            return p
    return None


@pytest.mark.parametrize("lang", LANGS)
def test_lineitem_exact_decimals(drivers, lang):
    """The exactness contract on real data: 6M rows of DECIMAL summed as the exact
    unscaled fixed-point integer. This is the number that goes wrong if a runtime
    routes a decimal through a float, and it is why sum_ep is asserted exactly."""
    if lang not in drivers:
        pytest.skip(f"no {lang} toolchain")
    lineitem = _lineitem()
    if lineitem is None:
        pytest.skip("no TPC-H parquet on this machine")

    out = _keys(_run(drivers[lang], "lineitem", str(lineitem)))
    assert out["rows"] > 0
    # sum(l_extendedprice) * 10^2 as an exact integer -- never a float.
    assert out["sum_ep"] == out["sum_ep"] // 1 == int(out["sum_ep"])
    assert out["sd_min"] < out["sd_max"]  # dates decoded, not zeroed


def test_runtimes_agree_on_lineitem(drivers):
    """Both runtimes over 6M rows of real TPC-H: every aggregate identical."""
    if len(drivers) < 2:
        pytest.skip("need both toolchains to compare them")
    lineitem = _lineitem()
    if lineitem is None:
        pytest.skip("no TPC-H parquet on this machine")

    cpp = _keys(_run(drivers["cpp"], "lineitem", str(lineitem)))
    rust = _keys(_run(drivers["rust"], "lineitem", str(lineitem)))
    assert cpp == rust, f"runtimes diverge on real TPC-H data:\nC++ ={cpp}\nRust={rust}"


def test_runtimes_print_the_same_keys(drivers, synth_parquet):
    """A key present in only one driver means the two have drifted apart: every
    assertion above could still pass while the runtimes silently stopped being
    compared on whatever the missing key covers."""
    if len(drivers) < 2:
        pytest.skip("need both toolchains to compare them")
    for args in (("nullable",), ("synth", str(synth_parquet))):
        cpp = set(_keys(_run(drivers["cpp"], *args)))
        rust = set(_keys(_run(drivers["rust"], *args)))
        assert cpp == rust, (
            f"conformance drivers disagree on which keys they report for {args[0]}: "
            f"only in C++={sorted(cpp - rust)}, only in Rust={sorted(rust - cpp)}"
        )


# --------------------------------------------------------------- shm plane --
# The zero-copy /dev/shm hot-load plane: the Python transport writes an Arrow-IPC
# segment, and the engine's loader maps it. The Rust loader must read a segment
# written by router/shm_transport.py byte-for-byte the same as the C++ loader --
# both are validated against the one canonical Python transport.
@pytest.fixture(scope="module")
def shm_segment(tmp_path_factory) -> Path:
    """A segment written by the REAL transport (write_arrow_segments), as
    ShmHotLoadEngine.ingest writes it. First column int64 so the drivers can sum
    it (sum 0..999 = 499500); a string column exercises variable-length buffers."""
    pytest.importorskip("pyarrow")
    import pyarrow as pa

    from synnodb.router.shm_transport import write_arrow_segments

    table = pa.table(
        {
            "a": pa.array(list(range(1000)), pa.int64()),
            "label": pa.array([f"r{i % 7}" for i in range(1000)], pa.utf8()),
        }
    )
    seg_dir = tmp_path_factory.mktemp("shm")
    write_arrow_segments(seg_dir, {"seg": table})
    return seg_dir / "seg.arrow"


@pytest.fixture(scope="module")
def cpp_shm_reader(tmp_path_factory) -> Path | None:
    """The C++ shm reader (shm_io_test.cpp), or None without a C++/Arrow toolchain."""
    if not _cpp_ok():
        return None
    cc = "g++" if shutil.which("g++") else "clang++"
    cflags = subprocess.run(
        ["pkg-config", "--cflags", "arrow"], capture_output=True, text=True
    ).stdout.split()
    libs = subprocess.run(
        ["pkg-config", "--libs", "arrow"], capture_output=True, text=True
    ).stdout.split()
    binary = tmp_path_factory.mktemp("cppshm") / "shm_io_test"
    proc = subprocess.run(
        [cc, "-std=c++20", "-O2", "-I", str(CPP_HELPERS), *cflags,
         str(CPP_SHM_DRIVER), *libs, "-o", str(binary)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        pytest.fail(f"C++ shm driver failed to compile:\n{proc.stderr}")
    return binary


def test_rust_reads_python_shm_segment(drivers, shm_segment):
    """The Rust loader maps a Python-written segment and recovers it exactly."""
    if "rust" not in drivers:
        pytest.skip("no rust toolchain")
    out = _keys(_run(drivers["rust"], "shm-read", str(shm_segment)))
    assert out == {"rows": 1000, "cols": 2, "sum0": 499500}


def test_shm_readers_agree(drivers, shm_segment, cpp_shm_reader):
    """The C++ and Rust loaders map the same segment to the same result -- the
    zero-copy plane's cross-language gate."""
    if "rust" not in drivers or cpp_shm_reader is None:
        pytest.skip("need both toolchains to compare them")
    cpp = _keys(_run(cpp_shm_reader, "read", str(shm_segment)))
    rust = _keys(_run(drivers["rust"], "shm-read", str(shm_segment)))
    assert cpp == rust, f"shm loaders diverge:\nC++ ={cpp}\nRust={rust}"
