"""Standalone live dashboard — browse run data without a running pipeline.

Usage examples:

  # Local DuckDB file
  python run_standalone_dashboard.py --db /mnt/labstore/bespoke_olap/logs/my_run.duckdb

  # W&B run (default entity/project)
  python run_standalone_dashboard.py --wandb_run_id abc123xy

  # W&B run with explicit entity/project
  python run_standalone_dashboard.py --wandb_run_id abc123xy \
      --wandb_entity myorg --wandb_project my-project

  # Remote live dashboard API on a job node
  python run_standalone_dashboard.py --api_url http://job-node:8765

  # Custom port
  python run_standalone_dashboard.py --db my_run.duckdb --port 9000
"""

import argparse
import logging
import sys
from urllib.parse import urlencode

from synnodb.observability.live_ui.live_dashboard import StandaloneDashboard

logging.basicConfig(level=logging.INFO, format="%(message)s")


def _dashboard_url(port: int, args: argparse.Namespace) -> str:
    params: dict[str, str] = {}
    if args.api_url:
        params["api_url"] = args.api_url
    elif args.db:
        params["db"] = args.db
    elif args.wandb_run_id:
        params["wandb_run_id"] = args.wandb_run_id
        # Only pin entity/project in the URL when the user overrode them;
        # otherwise let the dashboard resolve the single repo default lazily.
        if args.wandb_entity:
            params["wandb_entity"] = args.wandb_entity
        if args.wandb_project:
            params["wandb_project"] = args.wandb_project

    query = f"?{urlencode(params)}" if params else ""
    return f"http://localhost:{port}/{query}"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Start the Bespoke OLAP dashboard in standalone (read-only) mode."
    )

    source = parser.add_mutually_exclusive_group(required=False)
    source.add_argument(
        "--db",
        metavar="PATH",
        help="Path to a local .duckdb file written by DuckDBDrain.",
    )
    source.add_argument(
        "--wandb_run_id",
        metavar="RUN_ID",
        help="W&B run ID to load history from.",
    )
    source.add_argument(
        "--api_url",
        metavar="URL",
        help="Remote live dashboard URL, e.g. http://job-node:8765.",
    )

    parser.add_argument(
        "--wandb_entity",
        default=None,
        metavar="ENTITY",
        help="W&B entity (default: $WANDB_ENTITY or your W&B default entity).",
    )
    parser.add_argument(
        "--wandb_project",
        default=None,
        metavar="PROJECT",
        help="W&B project (default: $WANDB_PROJECT or the shared SynnoDB project).",
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Bind address (default: 0.0.0.0).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8765,
        help="Starting port (auto-increments if taken, default: 8765).",
    )

    args = parser.parse_args()

    dashboard = StandaloneDashboard(
        host=args.host,
        port=args.port,
        db_path=args.db,
        wandb_run_id=args.wandb_run_id,
        api_url=args.api_url,
        wandb_entity=args.wandb_entity,
        wandb_project=args.wandb_project,
    )

    print(
        f"Dashboard running at {_dashboard_url(dashboard.port, args)}  (Ctrl-C to stop)"
    )
    try:
        dashboard.serve_forever()
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
