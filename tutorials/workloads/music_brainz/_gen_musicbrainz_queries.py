"""One-shot script to (re)write the tutorial's self-describing ``musicbrainz_queries.json``.

The MusicBrainz queries are a set of templates in ``templates.json``: each is one SQL skeleton
with ``[PLACEHOLDER]`` holes plus ``generation_rules`` describing how to draw a value for every
hole. Two of those rule kinds have no faithful declarative equivalent in SynnoDB's typed param
specs:

  * ``int_offset`` binds an upper bound *correlated* to a lower one (``END = START + offset``),
    which independent scalar specs cannot express while preserving ``START <= END``;
  * ``choice_list`` renders a *variable-length* ``IN`` list into a single placeholder, whereas
    SynnoDB's ``sample`` group draws a fixed number of values across separate placeholders.

So rather than declaring value spaces, this script pre-generates concrete instances (drawing each
placeholder with the template's own rules) and binds all of a template's holes jointly as one
``tuples`` group - the exact shape ``sync_from_duckdb`` consumes, and the same bring-your-own shape
the Stack workload uses (see ``../stack/gen_stack_query.py``). Every sampled instantiation is
then a real, correlated binding: SynnoDB picks one recorded row per execution.

Regenerate ``musicbrainz_queries.json`` with::

    python tutorials/workloads/music_brainz/_gen_musicbrainz_queries.py

Generation is deterministic for a given ``--seed``.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import random
from pathlib import Path

from synnodb.workloads.query_params import hoist_literal_quotes

ROOT = Path(__file__).resolve().parent


def _load_generate_instances():
    """Import the sibling ``generate_instances.py`` by path (the directory is not a package on
    ``sys.path`` when this runs as a script, so a plain ``import`` would not resolve)."""
    spec = importlib.util.spec_from_file_location(
        "musicbrainz_generate_instances", ROOT / "generate_instances.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_musicbrainz_queries_json(
    templates_json: Path = ROOT / "templates.json",
    num_instances: int = 100,
    seed: int = 42,
) -> dict:
    """Build the templated bring-your-own ``queries.json`` mapping for the MusicBrainz workload.

    Each class becomes ``{"sql": <template>, "param_groups": [<tuples>]}``: the ``tuples`` group
    binds every ``[PLACEHOLDER]`` in the template jointly to ``num_instances`` pre-generated rows,
    so a whole correlated binding is sampled per execution. Insertion-ordered ``{qid: entry}``.
    """
    gi = _load_generate_instances()
    templates = json.loads(Path(templates_json).read_text())
    rng = random.Random(seed)

    out: dict = {}
    for cls, spec in templates.items():
        params = spec["parameters"]
        instances = gi.generate_class(cls, spec, rng, num_instances)
        rows = [[inst[p] for p in params] for inst in instances]
        # ``generate_class`` renders quoted values as SQL-ready literals (``'Person'``) filling
        # bare template holes. Convert to the framework convention - quotes in the template,
        # bare values - so the engine args line does not leak single quotes into the generated
        # C++ parser's string fields. Substituted SQL is unchanged.
        sql, rows = hoist_literal_quotes(spec["template"], params, rows)
        out[cls] = {
            "sql": sql,
            "param_groups": [
                {"type": "tuples", "placeholders": params, "values": rows},
            ],
        }
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--templates", default=ROOT / "templates.json", type=Path)
    parser.add_argument("--out", default=ROOT / "musicbrainz_queries.json", type=Path)
    parser.add_argument(
        "--num-instances",
        default=100,
        type=int,
        help="pre-generated bindings per template",
    )
    parser.add_argument("--seed", default=42, type=int)
    args = parser.parse_args()

    data = build_musicbrainz_queries_json(
        args.templates, num_instances=args.num_instances, seed=args.seed
    )
    args.out.write_text(json.dumps(data, indent=2))
    print(f"Wrote {args.out} ({len(data)} query classes)")


if __name__ == "__main__":
    main()
