#!/usr/bin/env bash
# Download the MusicBrainz full export and load it into a DuckDB file under STORAGE_DIR.
#
# Thin wrapper around load_musicbrainz.py so the whole flow (download + verify + extract + load)
# runs in the project venv. Override the destination by exporting STORAGE_DIR before calling, e.g.
#   STORAGE_DIR=/data/workloads ./download.sh
# Any extra arguments are forwarded to the Python loader (e.g. --archives mbdump --delete-archives).
set -euo pipefail

STORAGE_DIR="${STORAGE_DIR:-/mnt/labstore/learned_db/synno_data/workloads/}"
export STORAGE_DIR

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../../../.." && pwd)"

exec "${REPO_ROOT}/.venv/bin/python" "${SCRIPT_DIR}/load_musicbrainz.py" \
    --storage-dir "${STORAGE_DIR}" "$@"
