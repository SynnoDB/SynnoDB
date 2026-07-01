import argparse
import concurrent.futures
import json
import logging
import os
import shlex
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


def _cmd_str(cmd: list[str]) -> str:
    return " ".join(shlex.quote(c) for c in cmd)


def sanitize_flags_from_env() -> list[str]:
    """Compiler/linker flags for the optional sanitizer build profile.

    Gated by ``SYNNO_SANITIZE`` (e.g. ``address`` or ``address,undefined``). Empty / unset
    / a falsy value means no sanitizer. The same flags must go on both the compile and the
    link line, so callers add the result to cxxflags (objects + exe) and to ldflags (.so).
    Used to validate correctness at small scale factors, where AddressSanitizer's ~2-3x
    overhead is negligible but it pinpoints out-of-bounds / use-after-free with file:line.
    """
    val = os.environ.get("SYNNO_SANITIZE", "").strip().lower()
    if val in ("", "0", "off", "none", "false", "no"):
        return []
    sanitizers = [s for s in (part.strip() for part in val.split(",")) if s]
    if not sanitizers:
        return []
    return [f"-fsanitize={','.join(sanitizers)}", "-fno-omit-frame-pointer"]


def _run(cmd: list[str]) -> str:
    logger.debug(_cmd_str(cmd))
    proc = subprocess.run(cmd, text=True, capture_output=True)
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(
            proc.returncode, cmd, output=proc.stdout, stderr=proc.stderr
        )
    return proc.stdout


def build_id(path: Path) -> str | None:
    try:
        out = subprocess.check_output(["readelf", "-n", str(path)], text=True)
    except Exception:
        return None
    for line in out.splitlines():
        if "Build ID" in line:
            return line.strip()
    return None


def _format_cmd_error(exc: subprocess.CalledProcessError) -> str:
    cmd_str = _cmd_str(exc.cmd)
    parts = [cmd_str]
    if exc.output:
        parts.append(exc.output)
    if exc.stderr:
        parts.append(exc.stderr)
    if len(parts) == 1:
        parts.append("compile failed")
    return "\n".join(parts)


class Compiler:
    """Incremental compiler.

    `num_threads` controls parallel object compilation. Use `1` to compile
    serially, or `-1` to use all detected CPU cores.
    """

    def __init__(
        self,
        *,
        working_dir: Path | str = ".",
        libs: dict[str, list[Path | str]],
        main_src: str | Path,
        app_extra_srcs: list[Path | str] | None = None,
        build_dir: str = "build",
        link_libs: list[str] | None = None,
        pkgconfig_libs: list[str] | None = None,
        force_rebuild: bool = False,
        include_dirs: list[Path | str] | None = None,
        use_relative_paths: bool = True,
        num_threads: int = -1,  # -1 means use all CPU cores
    ) -> None:
        self.workdir = Path(working_dir).resolve()
        self.libs = libs
        self.main_src = main_src
        self.app_extra_srcs = app_extra_srcs or []
        self.build_dir = build_dir
        self.link_libs = link_libs or []
        self.pkgconfig_libs = pkgconfig_libs or []
        self.force_rebuild = force_rebuild
        self.include_dirs = include_dirs or ["."]
        self.use_relative_paths = use_relative_paths
        self.num_threads = num_threads

        self.main_src_path = self.workdir / self.main_src
        self.app_name = self.main_src_path.stem
        self.build_dir_path = self.workdir / self.build_dir
        self.obj_dir = self.build_dir_path / "obj"
        self.state_path = self.build_dir_path / ".build_state.json"

        self.cxx = os.environ.get("CXX", "g++")
        self.repro_flags = [
            "-ffile-prefix-map=.=.",
            "-fdebug-prefix-map=.=.",
            "-fmacro-prefix-map=.=.",
            "-fno-record-gcc-switches",
        ]
        # Optional AddressSanitizer (etc.) profile. Must appear on both compile and link.
        self.sanitize_flags = sanitize_flags_from_env()
        self.include_flags = self._normalize_include_dirs(self.include_dirs)
        self.cxxflags = self._make_cxxflags([])
        # --no-undefined keeps each .so self-contained; sanitize flags pull libasan into
        # the .so link so the dlopen'd plugins match the ASan-instrumented main exe.
        self.ldflags = [
            "-shared",
            "-Wl,--build-id=sha1",
            "-Wl,--no-undefined",
            *self.sanitize_flags,
        ]
        self.pkg_cflags: list[str] = []
        self.pkg_libs: list[str] = []
        if self.pkgconfig_libs:
            try:
                self.pkg_cflags = _run(
                    ["pkg-config", "--cflags", *self.pkgconfig_libs]
                ).split()
                self.pkg_libs = _run(
                    ["pkg-config", "--libs", *self.pkgconfig_libs]
                ).split()
            except subprocess.CalledProcessError as exc:
                cmd_str = _cmd_str(exc.cmd)
                parts = [cmd_str]
                if exc.output:
                    parts.append(exc.output)
                if exc.stderr:
                    parts.append(exc.stderr)
                if len(parts) == 1:
                    parts.append("pkg-config failed")
                raise RuntimeError("\n".join(parts))

    def set_compile_options(self, optimize: bool = False, trace_mode: bool = False):
        # overwrite existing extra flags with the new ones
        cxx_flags = []
        if optimize:
            cxx_flags.extend(["-O3", "-flto"])
        if trace_mode:
            cxx_flags.append("-DTRACE")
        self._set_extra_cxxflags(cxx_flags)

    def _set_extra_cxxflags(self, flags: list[str]) -> None:
        self.extra_cxxflags = list(flags)
        self.cxxflags = self._make_cxxflags(self.extra_cxxflags)

    def set_include_dirs(self, dirs: list[Path | str]) -> None:
        self.include_dirs: list[Path | str] = list(dirs) if dirs else ["."]
        self.include_flags = self._normalize_include_dirs(self.include_dirs)
        self.cxxflags = self._make_cxxflags(self.extra_cxxflags)

    def _make_cxxflags(self, extra: list[str]) -> list[str]:
        return [
            "-g",
            "-std=c++20",
            "-fPIC",
            *self.repro_flags,
            *self.include_flags,
            *extra,
        ]

    def _run_cmd(self, cmd: list[str]) -> None:
        logger.debug(_cmd_str(cmd))
        proc = subprocess.run(
            cmd,
            text=True,
            capture_output=True,
            cwd=self.workdir,
        )
        if proc.returncode != 0:
            raise subprocess.CalledProcessError(
                proc.returncode, cmd, output=proc.stdout, stderr=proc.stderr
            )

    def _relpath(self, path: Path) -> str:
        if not self.use_relative_paths:
            return str(path)
        try:
            return str(path.relative_to(self.workdir))
        except ValueError:
            return os.path.relpath(path, self.workdir)

    def _normalize_include_dirs(self, dirs: list[Path | str]) -> list[str]:
        flags: list[str] = []
        for p in dirs:
            path = Path(p)
            if not path.is_absolute():
                path = self.workdir / path
            flags.append(f"-I{self._relpath(path)}")
        return flags

    def load_state(self) -> dict:
        if self.force_rebuild or not self.state_path.exists():
            return {}
        try:
            return json.loads(self.state_path.read_text())
        except Exception:
            return {}

    def save_state(self, state: dict) -> None:
        tmp_path = self.state_path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(state, indent=2, sort_keys=True))
        tmp_path.replace(self.state_path)

    def _dep_paths(self, dep_path: Path) -> list[Path] | None:
        if not dep_path.exists():
            return None
        text = dep_path.read_text()
        text = text.replace("\\\n", " ")
        if ":" not in text:
            return []
        _, deps = text.split(":", 1)
        paths: list[Path] = []
        for token in deps.split():
            if not token:
                continue
            if token == "\\":
                continue
            if token.endswith(":"):
                token = token[:-1]
                if not token:
                    continue
            p = Path(token)
            if p.is_absolute():
                paths.append(p)
                continue
            candidates: list[Path] = []
            if p.parts and p.parts[0] == self.workdir.name:
                candidates.append(Path.cwd() / p)
            candidates.append(self.workdir / p)
            candidates.append(Path.cwd() / p)
            chosen = None
            for cand in candidates:
                if cand.exists():
                    chosen = cand
                    break
            paths.append(chosen if chosen is not None else candidates[0])
        return paths

    def needs_rebuild_obj(
        self,
        obj_path: Path,
        src_path: Path,
        dep_path: Path,
        cmd: list[str],
        state: dict,
    ) -> bool:
        obj_rel = self._relpath(obj_path)
        if self.force_rebuild or not obj_path.exists():
            if self.force_rebuild:
                logger.debug("rebuild %s: force_rebuild", obj_rel)
            else:
                logger.debug(
                    "rebuild %s: missing object (parent exists=%s)",
                    obj_rel,
                    obj_path.parent.exists(),
                )
            return True
        try:
            obj_mtime = obj_path.stat().st_mtime
            src_mtime = src_path.stat().st_mtime
        except FileNotFoundError:
            logger.debug("rebuild %s: missing file during stat", obj_rel)
            return True
        if src_mtime > obj_mtime:
            logger.debug("rebuild %s: source newer", obj_rel)
            return True
        deps = self._dep_paths(dep_path)
        if deps is not None:
            if not deps:
                logger.debug("rebuild %s: dep parse failed", obj_rel)
                return True
            for dep in deps:
                try:
                    if dep.stat().st_mtime > obj_mtime:
                        logger.debug(
                            "rebuild %s: dep newer %s", obj_rel, self._relpath(dep)
                        )
                        return True
                except FileNotFoundError:
                    logger.debug(
                        "rebuild %s: dep missing %s", obj_rel, self._relpath(dep)
                    )
                    return True
        key = str(obj_path)
        prev = state.get("objects", {}).get(key)
        if not prev or prev.get("cmd") != _cmd_str(cmd):
            if not prev:
                logger.debug("rebuild %s: no state", obj_rel)
            else:
                logger.debug("rebuild %s: cmd changed", obj_rel)
            return True
        return False

    def mark_obj_state(
        self, obj_path: Path, src_path: Path, cmd: list[str], state: dict
    ) -> None:
        state.setdefault("objects", {})[str(obj_path)] = {
            "src": str(src_path),
            "cmd": _cmd_str(cmd),
        }

    def needs_relink(
        self, out_path: Path, cmd: list[str], inputs: list[Path], state: dict, key: str
    ) -> bool:
        out_rel = self._relpath(out_path)
        if self.force_rebuild or not out_path.exists():
            if self.force_rebuild:
                logger.debug("relink %s: force_rebuild", out_rel)
            else:
                logger.debug("relink %s: missing output", out_rel)
            return True
        try:
            out_mtime = out_path.stat().st_mtime
        except FileNotFoundError:
            logger.debug("relink %s: missing output on stat", out_rel)
            return True
        for inp in inputs:
            try:
                if inp.stat().st_mtime > out_mtime:
                    logger.debug(
                        "relink %s: input newer %s", out_rel, self._relpath(inp)
                    )
                    return True
            except FileNotFoundError:
                logger.debug("relink %s: input missing %s", out_rel, self._relpath(inp))
                return True
        prev = state.get("links", {}).get(key)
        if not prev or prev.get("cmd") != _cmd_str(cmd):
            if not prev:
                logger.debug("relink %s: no state", out_rel)
            else:
                logger.debug("relink %s: cmd changed", out_rel)
            return True
        return False

    def mark_link_state(
        self, out_path: Path, cmd: list[str], state: dict, key: str
    ) -> None:
        state.setdefault("links", {})[key] = {
            "out": str(out_path),
            "cmd": _cmd_str(cmd),
        }

    def _maybe_compile_objects(self, tasks: list[tuple[Path, Path, list[str]]]) -> None:
        if not tasks:
            return
        if self.num_threads == 1 or len(tasks) == 1:
            for _, _, cmd in tasks:
                self._run_cmd(cmd)
            return

        if self.num_threads == -1:
            configured_workers = os.cpu_count() or 1
        else:
            configured_workers = max(1, self.num_threads)
        max_workers = min(len(tasks), configured_workers)
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(self._run_cmd, cmd) for _, _, cmd in tasks]
            try:
                for future in concurrent.futures.as_completed(futures):
                    future.result()
            except subprocess.CalledProcessError:
                for future in futures:
                    future.cancel()
                raise

    def build(self, extra_include_dirs: list[Path | str] | None = None) -> str | None:
        logger.debug(
            "build dirs: workdir=%s build_dir=%s obj_dir=%s",
            os.path.relpath(self.workdir, Path.cwd()),
            self._relpath(self.build_dir_path),
            self._relpath(self.obj_dir),
        )
        self.build_dir_path.mkdir(parents=True, exist_ok=True)
        self.obj_dir.mkdir(parents=True, exist_ok=True)
        include_flags = self.include_flags + self._normalize_include_dirs(
            extra_include_dirs or []
        )
        cxxflags = [
            "-g",
            "-std=c++20",
            "-fPIC",
            *self.repro_flags,
            *self.sanitize_flags,
            *include_flags,
            *self.extra_cxxflags,
        ]
        state = self.load_state()
        try:
            lib_obj_info: dict[str, list[tuple[Path, Path, list[str]]]] = {}
            pending_compile_tasks: list[tuple[Path, Path, list[str]]] = []
            for lib, srcs in self.libs.items():
                lib_tasks: list[tuple[Path, Path, list[str]]] = []
                for src in srcs:
                    src_path = self.workdir / src
                    obj_path = self.obj_dir / f"{lib}_{src_path.stem}.o"
                    dep_path = self.obj_dir / f"{lib}_{src_path.stem}.d"
                    obj_cmd = [
                        self.cxx,
                        *cxxflags,
                        *self.pkg_cflags,
                        "-MMD",
                        "-MP",
                        "-MF",
                        self._relpath(dep_path),
                        "-c",
                        self._relpath(src_path),
                        "-o",
                        self._relpath(obj_path),
                    ]
                    if self.needs_rebuild_obj(
                        obj_path, src_path, dep_path, obj_cmd, state
                    ):
                        pending_compile_tasks.append((obj_path, src_path, obj_cmd))
                    lib_tasks.append((obj_path, src_path, obj_cmd))
                lib_obj_info[lib] = lib_tasks

            app_obj_info: list[tuple[Path, Path, list[str]]] = []
            for src in self.app_extra_srcs:
                src_path = self.workdir / src
                obj_path = self.obj_dir / f"app_{src_path.stem}.o"
                dep_path = self.obj_dir / f"app_{src_path.stem}.d"
                obj_cmd = [
                    self.cxx,
                    *cxxflags,
                    *self.pkg_cflags,
                    "-MMD",
                    "-MP",
                    "-MF",
                    self._relpath(dep_path),
                    "-c",
                    self._relpath(src_path),
                    "-o",
                    self._relpath(obj_path),
                ]
                if self.needs_rebuild_obj(obj_path, src_path, dep_path, obj_cmd, state):
                    pending_compile_tasks.append((obj_path, src_path, obj_cmd))
                app_obj_info.append((obj_path, src_path, obj_cmd))

            main_obj = self.obj_dir / f"app_{self.main_src_path.stem}.o"
            main_dep = self.obj_dir / f"app_{self.main_src_path.stem}.d"
            main_cmd = [
                self.cxx,
                *cxxflags,
                *self.pkg_cflags,
                "-MMD",
                "-MP",
                "-MF",
                self._relpath(main_dep),
                "-c",
                self._relpath(self.main_src_path),
                "-o",
                self._relpath(main_obj),
            ]
            if self.needs_rebuild_obj(
                main_obj, self.main_src_path, main_dep, main_cmd, state
            ):
                pending_compile_tasks.append((main_obj, self.main_src_path, main_cmd))

            self._maybe_compile_objects(pending_compile_tasks)

            for lib_tasks in lib_obj_info.values():
                for obj_path, src_path, obj_cmd in lib_tasks:
                    self.mark_obj_state(obj_path, src_path, obj_cmd, state)

            for obj_path, src_path, obj_cmd in app_obj_info:
                self.mark_obj_state(obj_path, src_path, obj_cmd, state)
            self.mark_obj_state(main_obj, self.main_src_path, main_cmd, state)

            for lib, lib_tasks in lib_obj_info.items():
                objs = [self._relpath(obj_path) for obj_path, _, _ in lib_tasks]
                so_name = self.build_dir_path / f"lib{lib}.so"
                link_cmd = [
                    self.cxx,
                    *self.ldflags,
                    "-o",
                    self._relpath(so_name),
                    *objs,
                    *self.pkg_libs,
                ]
                input_paths = [self.workdir / p for p in objs]
                if self.needs_relink(
                    so_name, link_cmd, input_paths, state, f"lib:{lib}"
                ):
                    self._run_cmd(link_cmd)
                self.mark_link_state(so_name, link_cmd, state, f"lib:{lib}")

            app_objs = [self._relpath(obj_path) for obj_path, _, _ in app_obj_info]

            app_cmd = [
                self.cxx,
                *cxxflags,
                # Export the executable's dynamic symbols so backtrace_symbols_fd (used by
                # crash_handler.hpp) can name frames in the main `db` binary, not just the
                # dlopen'd query .so. Cheap, and only affects the final link.
                "-rdynamic",
                # Stamp the executable with a build-id too (the plugins already get one above).
                # The publish gate uses these ids to prove the engine on disk is the one that was
                # validated; relying on the linker's default would leave the binary unidentified on
                # toolchains that default --build-id to none.
                "-Wl,--build-id=sha1",
                *self.pkg_libs,
                "-o",
                self._relpath(self.workdir / self.app_name),
                self._relpath(main_obj),
                *app_objs,
            ]
            for lib in self.link_libs:
                app_cmd.extend([f"-L{self._relpath(self.build_dir_path)}", f"-l{lib}"])
            app_cmd.extend([f"-Wl,-rpath,$ORIGIN/{self.build_dir}"])
            app_out = self.workdir / self.app_name
            link_inputs = [main_obj] + [self.workdir / p for p in app_objs]
            for lib in self.link_libs:
                link_inputs.append(self.build_dir_path / f"lib{lib}.so")
            if self.needs_relink(
                app_out, app_cmd, link_inputs, state, f"app:{self.app_name}"
            ):
                self._run_cmd(app_cmd)
            self.mark_link_state(app_out, app_cmd, state, f"app:{self.app_name}")
        except subprocess.CalledProcessError as exc:
            err = _format_cmd_error(exc)

            # cap error to 20 lines
            err_lines = err.splitlines()

            if len(err_lines) > 20:
                err_lines = err_lines[-20:]
                err_lines.insert(
                    0, f"... (truncated, total {len(err.splitlines())} lines) ..."
                )
            err_display = "\n".join(err_lines)

            logger.error(err_display)
            return err

        self.save_state(state)
        return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--num-threads",
        type=int,
        default=1,
        help="Number of parallel compile threads to use; -1 means all CPU cores.",
    )
    args = parser.parse_args()

    compiler = Compiler(
        working_dir=".",
        libs={
            "mylib": ["mylib.cpp"],
            "mylib2": ["mylib2.cpp"],
            "mylib3": ["mylib3.cpp"],
        },
        main_src="main.cpp",
        app_extra_srcs=["../utils/build_id.cpp"],
        build_dir="build",
        link_libs=[],
        pkgconfig_libs=[],
        num_threads=args.num_threads,
    )
    err = compiler.build()
    if err is not None:
        raise SystemExit(1)


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    main()
