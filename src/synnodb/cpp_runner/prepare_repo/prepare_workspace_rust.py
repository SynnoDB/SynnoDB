"""Scaffold a Rust engine workspace.

The Rust sibling of ``OLAPPrepareWorkspace``. It writes a cargo workspace of
three crates -- loader / builder / query -- which compile to the same three .so
plugins the C++ engine produces, behind the same C ABI (``api/plugin_abi.h``).
The host does not know the difference.

What the model writes: ``builder/src/lib.rs`` (the Database + build) and
``query/src/q<N>.rs`` (one per query). Everything else is read-only scaffold.
"""

from pathlib import Path

from synnodb.conversations.filenames import get_plan_filename
from synnodb.cpp_runner.prepare_repo.assemble_rust import (
    assemble_args_file,
    assemble_loader_file,
    assemble_query_files,
    assemble_query_lib_file,
)
from synnodb.cpp_runner.prepare_repo.prepare_features import PrepareFeatures
from synnodb.cpp_runner.prepare_repo.prepare_workspace import PrepareWorkspace
from synnodb.utils.cli_config import Usecase
from synnodb.workloads.workload_provider_olap import OLAPWorkloadProvider

_TEMPLATES = Path(__file__).parent / "templates" / "rust"

# Where synno_rt lives inside the installed package. The generated Cargo.toml
# path-depends on it, exactly as the C++ compiler -I's cpp_helpers/.
_SYNNO_RT = Path(__file__).parent.parent.parent / "rust_runner" / "synno_rt"


class RustPrepareWorkspace(PrepareWorkspace):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        assert isinstance(self.workload_provider, OLAPWorkloadProvider), (
            f"Expected an OLAPWorkloadProvider, got {type(self.workload_provider)}"
        )

    @property
    def plan_filename(self) -> str:
        return get_plan_filename(Usecase.OLAP)

    # The crates holding types + logic, and the cdylib shims that export the C ABI.
    # They are separate crates for the same reason builder_api.cpp is its own
    # translation unit: plugin_query is #[no_mangle] and must land in exactly one .so.
    _LIB_CRATES = ("loader", "builder", "query")
    _PLUGIN_CRATES = ("loader", "builder", "query")

    @classmethod
    def _readonly_scaffold_files(cls) -> set[str]:
        # The model owns builder/src/lib.rs and query/src/q<N>.rs; everything else
        # in the scaffold is regenerated on every prepare.
        files = {
            "Cargo.toml",
            ".cargo/config.toml",
            "loader/src/lib.rs",
            "query/src/lib.rs",
            "query/src/args.rs",
        }
        files |= {f"{c}/Cargo.toml" for c in cls._LIB_CRATES}
        files |= {f"plugins/{c}/Cargo.toml" for c in cls._PLUGIN_CRATES}
        files |= {f"plugins/{c}/src/lib.rs" for c in cls._PLUGIN_CRATES}
        return files

    def build_scaffold_files(self, features: PrepareFeatures) -> dict[str, str]:
        if features.storage == "ssd":
            raise NotImplementedError(
                "The Rust engine currently targets in-memory storage only. The SSD "
                "plane needs its own Rust buffer pool / ColumnHandle scaffold "
                "(templates/olap/ssd on the C++ side); use language='cpp' for SSD."
            )

        assert isinstance(self.workload_provider, OLAPWorkloadProvider)
        provider = self.workload_provider
        query_ids = provider.query_ids
        tables = provider.dataset_tables

        files: dict[str, str] = {}

        # The workspace manifest points cargo at synno_rt inside the package.
        workspace_toml = (_TEMPLATES / "workspace_cargo.toml").read_text()
        files["Cargo.toml"] = workspace_toml.replace(
            "$synno_rt_path", _SYNNO_RT.resolve().as_posix()
        )
        files[".cargo/config.toml"] = (_TEMPLATES / "cargo_config.toml").read_text()

        for crate in self._LIB_CRATES:
            files[f"{crate}/Cargo.toml"] = (
                _TEMPLATES / crate / "Cargo.toml"
            ).read_text()

        # The cdylib shims: the only crates that export plugin_query().
        for crate in self._PLUGIN_CRATES:
            files[f"plugins/{crate}/Cargo.toml"] = (
                _TEMPLATES / "plugins" / crate / "Cargo.toml"
            ).read_text()
            files[f"plugins/{crate}/src/lib.rs"] = (
                _TEMPLATES / "plugins" / crate / "lib.rs"
            ).read_text()

        # loader: ParquetTables + the reads, from the workload's table list.
        files["loader/src/lib.rs"] = assemble_loader_file(tables)

        # builder: the model's file. Written once, then owned by the model.
        files["builder/src/lib.rs"] = (_TEMPLATES / "builder" / "lib.rs").read_text()

        # query: dispatch + args (generated), and one file per query (the model's).
        files["query/src/lib.rs"] = assemble_query_lib_file(query_ids)
        files["query/src/args.rs"] = assemble_args_file(
            query_ids=query_ids,
            gen_placeholders_fn=provider.get_placeholders_fn(),
        )
        files.update(assemble_query_files(query_ids, provider.sql_dict))

        files["queries.md"] = "\n".join(
            f"# Query **{q}**:\n```\n{provider.sql_dict[f'Q{q}']}\n```\n\n---\n"
            for q in query_ids
        )

        return files
