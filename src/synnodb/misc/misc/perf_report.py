#!/usr/bin/env python3
"""
perf_report_to_hotspots.py

Parse a *text* "perf report --stdio" output and emit a compact JSON hotspot
bundle that works well as LLM input.

Works best with:
  perf report --stdio --no-children --percent-limit 0.5 > perf_report.txt

Usage:
  python3 perf_report_to_hotspots.py perf_report.txt > hotspots.json
  python3 perf_report_to_hotspots.py perf_report.txt --top 25 --min-pct 0.5

Notes:
- perf's stdio format varies slightly by version/config; this parser aims to be
  robust for the common "Overhead  Command  Shared Object  Symbol" table.
- It extracts a flat hotspot list (self overhead). If you want callchains/inclusive
  time, feed it a children report separately and extend the schema.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Dict, List, Tuple


# Example lines commonly seen:
#  12.34%  db       db                [.] btree::find
#   3.21%  db       libc.so.6          [.] memcmp
#   0.87%  db       [kernel.kallsyms]  [k] __x64_sys_read
#
# Some variants include CPU column or extra spacing, but the key anchor is the
# leading percentage and the "[.]/[k]/[u]" marker before the symbol.
LINE_RE = re.compile(
    r"""^\s*
        (?P<pct>\d+(?:\.\d+)?)\%      # leading percent number (overhead)
        (?:\s+(?P<pct2>\d+(?:\.\d+)?)\%)?  # optional second percent (children)
        \s+
        (?P<comm>\S+)                 # command / process name
        \s+
        (?P<dso>\S+)                  # shared object / dso
        \s+
        (?:\[[^\]]*\]\s+)?            # optional annotation like [.] or [k]
        (?P<sym>.+?)                  # symbol (rest)
        \s*$
    """,
    re.VERBOSE,
)

CALLGRAPH_RE = re.compile(
    r"""^\s*
        (?:\|\s*)*                 # callgraph tree bars
        (?:\|--|--)                # branch marker
        \s*
        (?P<pct>\d+(?:\.\d+)?)\%--
        (?P<sym>.+?)\s*$
    """,
    re.VERBOSE,
)


@dataclass
class Hotspot:
    symbol: str
    self_pct: float
    samples_pct: float  # alias of self_pct (kept for clarity with future inclusive_pct)
    children_pct: float
    dso: str
    comm: str


def _is_table_header(line: str) -> bool:
    # Common header contains "Overhead" or "Children" and "Symbol"
    l = line.strip().lower()
    return (
        (("overhead" in l) or ("children" in l)) and "symbol" in l
    ) or l.startswith("overhead ") or l.startswith("children ")


def parse_perf_report_text(text: str) -> Tuple[List[Hotspot], Dict[str, int]]:
    hotspots: List[Hotspot] = []
    in_table = False
    children_first = False
    total_lines = 0
    table_lines = 0
    parsed_lines = 0
    skipped_lines = 0

    for raw in text.splitlines():
        total_lines += 1
        line = raw.rstrip("\n")

        if not in_table:
            if _is_table_header(line):
                in_table = True
                l = line.lower()
                if "children" in l and "self" in l:
                    children_first = l.find("children") < l.find("self")
            continue

        # After table begins, skip separators/blank/comment-ish lines
        if not line.strip():
            continue
        if line.strip().startswith("#"):
            continue
        if line.strip().startswith("---"):
            continue

        table_lines += 1
        m = LINE_RE.match(line)
        if not m:
            # Some perf versions put extra columns; try a fallback split:
            # Look for "<pct>% ... <dso> ... <symbol>"
            if "%" not in line:
                continue
            skipped_lines += 1
            continue

        pct = float(m.group("pct"))
        pct2 = m.group("pct2")
        if pct2 is None:
            self_pct = pct
            children_pct = pct
        else:
            if children_first:
                children_pct = pct
                self_pct = float(pct2)
            else:
                self_pct = pct
                children_pct = float(pct2)
        comm = m.group("comm")
        dso = m.group("dso")
        sym = m.group("sym").strip()

        # Normalize symbols that sometimes include leading markers:
        # e.g. "[.] foo" or "[k] bar" might appear if the optional group didn't catch it
        sym = re.sub(r"^\[[^\]]+\]\s+", "", sym)

        hotspots.append(
            Hotspot(
                symbol=sym,
                self_pct=self_pct,
                samples_pct=self_pct,
                children_pct=children_pct,
                dso=dso,
                comm=comm,
            )
        )
        parsed_lines += 1

    stats = {
        "lines_total": total_lines,
        "table_lines": table_lines,
        "parsed_lines": parsed_lines,
        "skipped_lines": skipped_lines,
    }
    return hotspots, stats


def extract_callgraph_for_top_hotspot(text: str) -> List[Tuple[float, str]]:
    in_table = False
    found_top = False
    callgraph: List[Tuple[float, str]] = []

    for raw in text.splitlines():
        line = raw.rstrip("\n")

        if not in_table:
            if _is_table_header(line):
                in_table = True
            continue

        if not found_top:
            m = LINE_RE.match(line)
            if m:
                found_top = True
            continue

        # stop when a new top-level hotspot line appears or table ends
        if LINE_RE.match(line):
            break
        if not line.strip():
            break

        m = CALLGRAPH_RE.match(line)
        if not m:
            continue
        pct = float(m.group("pct"))
        sym = m.group("sym").strip()
        sym = re.sub(r"^\[[^\]]+\]\s+", "", sym)
        callgraph.append((pct, sym))

    return callgraph


def load_perf_report_text(path: str, min_pct: float) -> str:
    with open(path, "rb") as f:
        head = f.read(4096)
        f.seek(0)
        is_binary = b"\x00" in head or path.endswith(".data")
        if not is_binary:
            return f.read().decode("utf-8", errors="replace")

    if shutil.which("perf") is None:
        raise RuntimeError(
            "Input looks like perf.data, but `perf` is not available on PATH."
        )

    cmd = [
        "perf",
        "report",
        "--stdio",
        "--percent-limit",
        str(min_pct),
        "-i",
        path,
    ]
    result = subprocess.run(cmd, check=False, text=True, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"`perf report` failed (exit {result.returncode}): {result.stderr.strip()}"
        )
    return result.stdout


def dedupe_and_sort(hotspots: List[Hotspot]) -> List[Hotspot]:
    # Combine entries with the same (symbol, dso, comm) by summing pct.
    # perf report typically already aggregates, but this makes the output robust.
    agg = {}
    for h in hotspots:
        key = (h.symbol, h.dso, h.comm)
        if key not in agg:
            agg[key] = h
        else:
            agg[key].self_pct += h.self_pct
            agg[key].samples_pct += h.samples_pct
            agg[key].children_pct += h.children_pct

    out = list(agg.values())
    out.sort(key=lambda x: max(x.self_pct, x.children_pct), reverse=True)
    return out


def hotspots_to_table(hotspots: List[Hotspot]) -> str:
    header = "Overhead  Shared Object     Symbol"
    lines = [header]
    for h in hotspots:
        pct = f"{h.self_pct:6.2f}%"
        lines.append(f"{pct}  {h.dso:<16} {h.symbol}")
    return "\n".join(lines)


def build_summary_text(hotspots: List[Hotspot], callgraph: List[Tuple[float, str]]) -> str:
    lines = []
    lines.append(hotspots_to_table(hotspots))
    if callgraph:
        lines.append("")
        lines.append("Top call paths (self overhead):")
        for pct, sym in callgraph[:8]:
            lines.append(f"  {pct:6.2f}%  {sym}")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "perf_report",
        help="Path to perf report text file produced by 'perf report --stdio'",
    )
    ap.add_argument(
        "--top", type=int, default=30, help="Max number of hotspots to emit"
    )
    ap.add_argument(
        "--min-pct",
        type=float,
        default=0.5,
        help="Drop hotspots below this self%% threshold",
    )
    args = ap.parse_args()

    text = load_perf_report_text(args.perf_report, args.min_pct)

    hs, _parse_stats = parse_perf_report_text(text)
    hs = dedupe_and_sort(hs)

    filtered = [h for h in hs if h.self_pct >= args.min_pct][: args.top]
    callgraph = extract_callgraph_for_top_hotspot(text)
    print(build_summary_text(filtered, callgraph))


if __name__ == "__main__":
    main()
