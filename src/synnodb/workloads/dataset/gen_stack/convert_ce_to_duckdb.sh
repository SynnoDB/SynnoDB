#!/usr/bin/env bash
#
# Convert the Stack CE benchmark dump (so_pg13, PostgreSQL custom format) into a
# DuckDB database.
#
# DuckDB cannot read a pg_dump custom archive directly, so we restore it into a
# throwaway Dockerized Postgres and then pull every table across with DuckDB's
# postgres scanner. We do NOT go via pg_restore --data-only | CSV: Postgres COPY
# escaping corrupts the multi-line `body` text.
#
# Everything (PGDATA + DuckDB) lives on labstore, which has the space; Postgres
# runs fine there. The container runs as the host uid/gid so PGDATA stays
# host-removable.
#
set -euo pipefail

STORAGE_DIR="${STORAGE_DIR:-/mnt/labstore/learned_db/synno_data/workloads/stack}"
DUMP="${DUMP:-$STORAGE_DIR/so_pg13}"
DB_PATH="${DB_PATH:-$STORAGE_DIR/stack_ce.duckdb}"

PGDATA_DIR="$STORAGE_DIR/pgdata"
CONTAINER="stack_ce_pg"
PGPORT="${PGPORT:-5439}"
PGDB="stack"
PGPW="stack"
CONN="dbname=$PGDB host=127.0.0.1 port=$PGPORT user=postgres password=$PGPW"
JOBS="${JOBS:-8}"
KEEP_PGDATA="${KEEP_PGDATA:-0}"

[[ -f "$DUMP" ]] || { echo "Error: dump not found: $DUMP" >&2; exit 1; }
command -v docker    >/dev/null || { echo "Error: docker missing" >&2; exit 1; }
command -v pg_restore>/dev/null || { echo "Error: pg_restore missing" >&2; exit 1; }
command -v duckdb    >/dev/null || { echo "Error: duckdb missing" >&2; exit 1; }

cleanup() {
  echo ">> Stopping Postgres container ..."
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  if [[ "$KEEP_PGDATA" != "1" ]]; then
    echo ">> Removing PGDATA ($PGDATA_DIR) ..."
    rm -rf "$PGDATA_DIR"
  fi
}
trap cleanup EXIT

# --- 1. Start throwaway Postgres -------------------------------------------
docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
rm -rf "$PGDATA_DIR"; mkdir -p "$PGDATA_DIR"
echo ">> Starting postgres:16 (PGDATA on labstore) ..."
# This Postgres is disposable and we only ever full-scan each table once, so
# durability is pointless: turn off fsync/WAL work to avoid fsync-to-NFS latency
# and make the bulk COPY as fast as possible.
docker run -d --name "$CONTAINER" \
  --user "$(id -u):$(id -g)" \
  --shm-size=1g \
  -e POSTGRES_PASSWORD="$PGPW" -e POSTGRES_DB="$PGDB" \
  -e PGDATA=/var/lib/postgresql/data/pgd \
  -v "$PGDATA_DIR:/var/lib/postgresql/data" \
  -p "$PGPORT:5432" postgres:16 \
  -c fsync=off -c synchronous_commit=off -c full_page_writes=off \
  -c wal_level=minimal -c max_wal_senders=0 -c archive_mode=off \
  -c max_wal_size=16GB -c checkpoint_timeout=60min \
  -c shared_buffers=8GB -c maintenance_work_mem=2GB >/dev/null

# Wait for the REAL server: the entrypoint runs a socket-only init phase first,
# so poll the host TCP port, not `docker exec pg_isready`.
echo ">> Waiting for Postgres TCP readiness ..."
for i in $(seq 1 90); do
  if PGPASSWORD="$PGPW" psql -h 127.0.0.1 -p "$PGPORT" -U postgres -d "$PGDB" \
       -tAc "select 1" >/dev/null 2>&1; then
    echo "   ready after ${i}s"; break
  fi
  sleep 1
  [[ "$i" == 90 ]] && { echo "Error: Postgres did not become ready" >&2; docker logs "$CONTAINER" | tail; exit 1; }
done

# --- 2. Restore the dump (data only) ----------------------------------------
# We copy each table into DuckDB with a single full scan, so the dump's indexes,
# primary keys and foreign keys are pure overhead - building/validating them is
# what makes a full restore slow. Restore pre-data (bare table definitions) then
# data (parallel COPY), and skip post-data (INDEX / CONSTRAINT / FK) entirely.
echo ">> Creating tables (pre-data) ..."
PGPASSWORD="$PGPW" pg_restore --no-owner --no-privileges --exit-on-error \
  --section=pre-data -h 127.0.0.1 -p "$PGPORT" -U postgres -d "$PGDB" "$DUMP"

echo ">> Loading data (parallel COPY, no indexes/keys) ..."
PGPASSWORD="$PGPW" pg_restore --no-owner --no-privileges --exit-on-error \
  --section=data -j "$JOBS" -h 127.0.0.1 -p "$PGPORT" -U postgres -d "$PGDB" "$DUMP"

echo ">> Restored. Postgres row counts:"
PGPASSWORD="$PGPW" psql -h 127.0.0.1 -p "$PGPORT" -U postgres -d "$PGDB" -c "
  SELECT relname, n_live_tup FROM pg_stat_user_tables ORDER BY n_live_tup DESC;"

tables=$(PGPASSWORD="$PGPW" psql -h 127.0.0.1 -p "$PGPORT" -U postgres -d "$PGDB" \
  -tAc "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename")
[[ -n "$tables" ]] || { echo "Error: no tables restored" >&2; exit 1; }

# --- 3. Copy every table into DuckDB via the postgres scanner ---------------
echo ">> Building DuckDB at $DB_PATH ..."
rm -f "$DB_PATH"
LOAD_SQL="$STORAGE_DIR/convert_ce.sql"
{
  echo "INSTALL postgres; LOAD postgres;"
  echo "ATTACH '$CONN' AS pg (TYPE postgres, READ_ONLY);"
  while read -r t; do
    [[ -z "$t" ]] && continue
    echo "CREATE OR REPLACE TABLE \"$t\" AS FROM pg.public.\"$t\";"
  done <<< "$tables"
} > "$LOAD_SQL"
duckdb "$DB_PATH" < "$LOAD_SQL"

# --- 4. Verify: row counts must match Postgres ------------------------------
echo ">> Verifying row counts (Postgres vs DuckDB):"
ok=1
while read -r t; do
  [[ -z "$t" ]] && continue
  pg=$(PGPASSWORD="$PGPW" psql -h 127.0.0.1 -p "$PGPORT" -U postgres -d "$PGDB" -tAc "SELECT count(*) FROM \"$t\"")
  dd=$(duckdb "$DB_PATH" -noheader -list -c "SELECT count(*) FROM \"$t\"")
  status="OK"; [[ "$pg" != "$dd" ]] && { status="MISMATCH"; ok=0; }
  printf "   %-16s pg=%-12s duckdb=%-12s %s\n" "$t" "$pg" "$dd" "$status"
done <<< "$tables"

echo ">> DuckDB: $DB_PATH ($(du -h "$DB_PATH" | cut -f1))"
[[ "$ok" == 1 ]] && echo "All tables match. Done." || { echo "ROW COUNT MISMATCH." >&2; exit 1; }
