#!/usr/bin/env bash
#
# Download the Stack cardinality-estimation benchmark data: the so_pg13
# PostgreSQL custom-format dump (pg_dump -Fc, PG 13.2, ~18.5 GiB).
#
# This is the data the so_queries/ workload (q1..q16) runs against - the Stack
# CE benchmark from the Flow-Loss / Cardinality Estimation Benchmark line of
# work. It is NOT the SQLStorm dataset.
#
# Dropbox drops the connection every ~130 MB, so we loop wget --continue until
# the file reaches its expected size.
#
set -euo pipefail

STORAGE_DIR="${STORAGE_DIR:-/mnt/labstore/learned_db/synno_data/workloads/stack}"
DEST="$STORAGE_DIR/so_pg13"
URL="https://www.dropbox.com/s/55bxfhilcu19i33/so_pg13?dl=1"
EXPECTED=19914075537

mkdir -p "$STORAGE_DIR"
echo "Downloading so_pg13 -> $DEST"
echo "Expected size: $EXPECTED bytes (~18.5 GiB)"

attempt=0
while true; do
  cur=$(stat -c%s "$DEST" 2>/dev/null || echo 0)
  if [[ "$cur" -ge "$EXPECTED" ]]; then
    echo "Reached expected size ($cur bytes)."
    break
  fi
  attempt=$((attempt + 1))
  echo "[attempt $attempt] have $cur / $EXPECTED bytes; resuming ..."
  # --continue resumes; Dropbox supports range requests. Follow the 302 redirect.
  wget --continue --tries=1 --timeout=60 -q "$URL" -O "$DEST" || true
done

sz=$(stat -c%s "$DEST")
echo "Done: $DEST ($sz bytes)."
[[ "$sz" -eq "$EXPECTED" ]] || { echo "WARNING: size != expected ($EXPECTED)."; }
