from __future__ import annotations

import atexit
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pandas as pd

from pgrouter.capture_files import load_result_rows


REPO_ROOT = Path(__file__).resolve().parents[1]
TEST_CONTAINER_LABEL = "pgrouter.test-suite=true"
JDBC_CLIENT_SOURCE = REPO_ROOT / "tests" / "jdbc" / "JdbcDemoClient.java"


def reserve_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for_port(host: str, port: int, timeout_s: float) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.1)
    raise TimeoutError(f"router did not start listening on {host}:{port}")


def wait_for_process_exit(proc: subprocess.Popen[str], timeout_s: float) -> int:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        code = proc.poll()
        if code is not None:
            return code
        time.sleep(0.05)
    raise TimeoutError(f"process did not exit within {timeout_s} seconds")


def stop_process(proc: subprocess.Popen[str], timeout_s: float = 5.0) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=timeout_s)


def start_router_process(
    *,
    port: int,
    jsonl_path: Path,
    results_dir: Path,
    result_file_format: str = "json",
    upstream: str,
    extra_env: dict[str, str] | None = None,
    use_uv: bool = False,
) -> subprocess.Popen[str]:
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    python_command = ["uv", "run", "python"] if use_uv and shutil.which("uv") is not None else [sys.executable]
    return subprocess.Popen(
        [
            *python_command,
            "pg_router.py",
            "--listen",
            f"127.0.0.1:{port}",
            "--upstream",
            upstream,
            "--jsonl-path",
            str(jsonl_path),
            "--results-dir",
            str(results_dir),
            "--result-file-format",
            result_file_format,
        ],
        cwd=REPO_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )


def run_psql(
    conninfo: str,
    sql: str,
    *,
    timeout_s: float = 30.0,
    capture_output: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["psql", conninfo, "-v", "ON_ERROR_STOP=1", "-qAt", "-c", sql],
        cwd=REPO_ROOT,
        text=True,
        stdout=subprocess.PIPE if capture_output else subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=timeout_s,
        check=True,
    )


def run_psql_commands(conninfo: str, commands: list[str], *, timeout_s: float = 30.0) -> subprocess.CompletedProcess[str]:
    argv = ["psql", conninfo, "-v", "ON_ERROR_STOP=1", "-qAt"]
    for command in commands:
        argv.extend(["-c", command])
    return subprocess.run(
        argv,
        cwd=REPO_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout_s,
        check=True,
    )


def cleanup_test_postgres_containers() -> None:
    if shutil.which("docker") is None:
        return
    result = subprocess.run(
        ["docker", "ps", "-aq", "--filter", f"label={TEST_CONTAINER_LABEL}"],
        cwd=REPO_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    container_ids = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not container_ids:
        return
    subprocess.run(
        ["docker", "rm", "-f", *container_ids],
        cwd=REPO_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def register_test_postgres_cleanup() -> None:
    atexit.register(cleanup_test_postgres_containers)


def find_postgresql_jdbc_jar() -> Path | None:
    env_path = os.environ.get("PGJDBC_JAR")
    if env_path:
        path = Path(env_path).expanduser()
        if path.exists():
            return path.resolve()

    candidate_paths = [
        REPO_ROOT / "tests" / "jdbc" / "lib" / "postgresql.jar",
        REPO_ROOT / "tests" / "jdbc" / "lib" / "postgresql-jdbc.jar",
        Path("/usr/share/java/postgresql.jar"),
        Path("/usr/share/java/postgresql-jdbc.jar"),
    ]
    for path in candidate_paths:
        if path.exists():
            return path.resolve()

    glob_patterns = [
        REPO_ROOT / "tests" / "jdbc" / "lib" / "postgresql-*.jar",
        Path("/usr/share/java/postgresql-*.jar"),
        Path("/usr/share/maven-repo/org/postgresql/postgresql") / "*" / "postgresql-*.jar",
    ]
    for pattern in glob_patterns:
        matches = sorted(Path().glob(str(pattern)) if not pattern.is_absolute() else pattern.parent.glob(pattern.name))
        if matches:
            return matches[-1].resolve()
    return None


def compile_jdbc_test_client(build_dir: Path, jdbc_jar: Path) -> Path:
    class_output_dir = build_dir / "classes"
    class_output_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "javac",
            "-cp",
            str(jdbc_jar),
            "-d",
            str(class_output_dir),
            str(JDBC_CLIENT_SOURCE),
        ],
        cwd=REPO_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return class_output_dir


def run_jdbc_test_client(
    *,
    jdbc_url: str,
    jdbc_jar: Path,
    class_output_dir: Path,
    user: str = "app",
    password: str = "",
    timeout_s: float = 30.0,
) -> subprocess.CompletedProcess[str]:
    classpath = os.pathsep.join([str(class_output_dir), str(jdbc_jar)])
    return subprocess.run(
        ["java", "-cp", classpath, "JdbcDemoClient", jdbc_url, user, password],
        cwd=REPO_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout_s,
        check=True,
    )


class DockerPostgresInstance:
    def __init__(self, *, auth_mode: str) -> None:
        self.auth_mode = auth_mode
        self.port = reserve_tcp_port()
        self.password = "app"
        self.container_name = f"pgrouter-{auth_mode}-test-{int(time.time() * 1000)}-{self.port}"

    @property
    def conninfo(self) -> str:
        if self.auth_mode == "password":
            return f"postgresql://app:{self.password}@127.0.0.1:{self.port}/app?sslmode=disable"
        return f"postgresql://app@127.0.0.1:{self.port}/app?sslmode=disable"

    def start(self) -> None:
        argv = [
            "docker",
            "run",
            "--rm",
            "--detach",
            "--name",
            self.container_name,
            "--label",
            TEST_CONTAINER_LABEL,
            "-e",
            "POSTGRES_DB=app",
            "-e",
            "POSTGRES_USER=app",
            "-e",
            f"POSTGRES_PASSWORD={self.password}",
            "-p",
            f"{self.port}:5432",
        ]
        if self.auth_mode == "trust":
            argv.extend(["-e", "POSTGRES_HOST_AUTH_METHOD=trust"])
        argv.append("postgres:16-alpine")
        subprocess.run(
            argv,
            cwd=REPO_ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        deadline = time.time() + 30.0
        while time.time() < deadline:
            try:
                run_psql(self.conninfo, "select 1", timeout_s=2.0, capture_output=False)
                return
            except Exception:
                time.sleep(0.2)
        raise TimeoutError(f"docker postgres ({self.auth_mode}) did not become ready on 127.0.0.1:{self.port}")

    def stop(self) -> None:
        subprocess.run(
            ["docker", "rm", "-f", self.container_name],
            cwd=REPO_ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )


def load_jsonl_records(jsonl_path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in jsonl_path.read_text(encoding="utf-8").splitlines() if line.strip()]


def resolve_result_path(result_file: str | None) -> Path | None:
    if not result_file:
        return None
    path = Path(result_file)
    if path.is_absolute():
        return path
    repo_path = REPO_ROOT / result_file
    return repo_path if repo_path.exists() else path


def load_result_records(result_file: str | None, result_file_format: str) -> list[dict[str, object]]:
    result_path = resolve_result_path(result_file)
    if result_path is None:
        return []
    if result_file_format == "json":
        return load_result_rows(result_path)
    return pd.read_pickle(result_path).to_dict(orient="records")
