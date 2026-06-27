"""Extract the memory budget (`memory_budget_mb`) that a past wandb run used.

The validate-tool cache key depends on `memory_budget_mb` (for non-IN_MEMORY
storage modes). When the CLI flag is not passed, the default is computed from
`SC_PHYS_PAGES * SC_PAGE_SIZE * 0.9`, which fluctuates between machines and even
across reboots. To reuse a prior run's validate cache, re-invoke with
`--memory_budget_mb=<value>` set to the exact value the prior run used.

This script resolves that value for a given wandb run id by:
  1. Reading `run.config["memory_budget_mb"]`. If non-null, return it.
  2. Otherwise, locating the run's logfile via `run.config["log_run_name"]`
     under `<artifacts_dir>/logs/` and parsing the `mem_limit=<N>` value from
     the first "Run with:" log line.

Usage:
    python -m demo_and_analysis.get_mem_budget_setting_from_run <run_id>
    python -m demo_and_analysis.get_mem_budget_setting_from_run <run_id> \\
        --entity learneddb --project bespoke-olap-internal
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent))

from observability.plots.utils.wandb_utils import get_wandb_run
from utils.cli_config import DEFAULT_ARTIFACTS_DIR

MEM_LIMIT_RE = re.compile(r"mem_limit=(\d+)")


def _extract_from_logfile(log_path: Path) -> int | None:
    if not log_path.exists():
        return None
    with log_path.open() as f:
        for line in f:
            if "Run with:" not in line:
                continue
            m = MEM_LIMIT_RE.search(line)
            if m:
                return int(m.group(1))
    return None


def get_mem_budget_from_wandb_run(
    run_id: str,
    entity: str = "learneddb",
    project: str = "bespoke-olap-internal",
    artifacts_dir: str = DEFAULT_ARTIFACTS_DIR,
) -> int:
    run = get_wandb_run(run_id=run_id, entity=entity, project=project)
    config = dict(run.config)

    explicit = config.get("memory_budget_mb")
    if explicit is not None:
        return int(explicit)

    # Fall back to scraping the logfile written alongside the run.
    log_run_name = config.get("log_run_name")
    if log_run_name is None:
        raise RuntimeError(
            f"Run {run_id} has memory_budget_mb=None and no log_run_name in config; "
            "cannot recover the value the run actually used."
        )

    log_path = Path(artifacts_dir) / "logs" / f"{log_run_name}.log"
    value = _extract_from_logfile(log_path)
    if value is None:
        raise RuntimeError(
            f"Run {run_id} has memory_budget_mb=None and no `mem_limit=<N>` "
            f"line was found in {log_path}. Cannot recover the value."
        )
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id", help="wandb run id (e.g. a8oim49a)")
    parser.add_argument("--entity", default="learneddb")
    parser.add_argument("--project", default="bespoke-olap-internal")
    parser.add_argument(
        "--artifacts_dir",
        default=DEFAULT_ARTIFACTS_DIR,
        help="Artifacts dir containing the logs/ subdir (default: %(default)s)",
    )
    args = parser.parse_args()

    value = get_mem_budget_from_wandb_run(
        run_id=args.run_id,
        entity=args.entity,
        project=args.project,
        artifacts_dir=args.artifacts_dir,
    )
    print(value)
    return 0


if __name__ == "__main__":
    sys.exit(main())
