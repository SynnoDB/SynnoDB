#!/usr/bin/env bash
# List all `db` process trees with per-process RSS and thread count.
# Threads share their parent's RSS, so we hide them and just show a count.
set -euo pipefail

fmt_mem() {  # kB -> "X.XXGB" or "XXXMB"
  awk "BEGIN { kb=${1:-0}; gb=kb/1048576;
               if (gb>=0.005) printf \"%.2fGB\", gb;
               else printf \"%.0fMB\", kb/1024 }"
}

proc_info() {  # PID -> "name(PID) [MEM, N thr]"
  local pid="$1"
  local name rss threads
  name="$(ps -o comm= -p "$pid" 2>/dev/null | tr -d ' ')"
  rss="$(awk '/^VmRSS:/{print $2}' /proc/"$pid"/status 2>/dev/null || echo 0)"
  threads="$(awk '/^Threads:/{print $2}' /proc/"$pid"/status 2>/dev/null || echo 1)"
  printf "%s(%s) [%s, %s thr]" "$name" "$pid" "$(fmt_mem "${rss:-0}")" "$threads"
}

# Recursively render children of $1 with prefix $2.
render_children() {
  local parent="$1" prefix="$2"
  local kids=() k
  # Non-thread children only: ps --ppid does not list threads.
  while read -r k; do
    [[ -n "$k" ]] && kids+=("$k")
  done < <(ps --ppid "$parent" -o pid= 2>/dev/null | awk '{print $1}')

  local n=${#kids[@]} i=0
  for k in "${kids[@]}"; do
    i=$((i+1))
    local branch="├─ " next_prefix="${prefix}│  "
    if (( i == n )); then
      branch="└─ "
      next_prefix="${prefix}   "
    fi
    printf "%s%s%s\n" "$prefix" "$branch" "$(proc_info "$k")"
    render_children "$k" "$next_prefix"
  done
}

first=1
while read -r pid; do
  # Only roots: parent process is not itself a `db`.
  ppid="$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ')"
  parent_comm="$(ps -o comm= -p "${ppid:-0}" 2>/dev/null | tr -d ' ')"
  [[ "$parent_comm" == "db" ]] && continue

  args="$(ps -p "$pid" -o args= 2>/dev/null)"

  (( first )) || echo
  first=0
  printf "%s  %s\n" "$(proc_info "$pid")" "$args"
  render_children "$pid" ""
done < <(pgrep -x db)
