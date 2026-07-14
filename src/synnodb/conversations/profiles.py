"""Language profiles: the target-language half of a prompt.

The prompt corpus varies along two axes, and they are NOT the same kind of
variation:

* **Storage** (in-memory vs SSD) changes *what the model is told to build* - a
  struct-of-arrays populated through ``column_ingest.hpp``, versus a buffer pool
  with ``ColumnHandle<T>`` and pin/unpin page loops. Those are different
  documents, and they stay different files (``prompts/`` vs ``prompts/ssd/``).

* **Language** (C++ vs Rust) changes *how the same thing is expressed* - the
  helper API names, the exact-decimal accumulator type, the parallelism
  primitive, the filenames. That is the same document with different words in a
  handful of places.

So language is a set of *slots* substituted into whichever storage document was
selected, never a second copy of it. Adding a language adds one directory under
``prompts/lang/`` and one ``LanguageProfile`` here - and zero new prompt files.
That is what keeps the two targets from drifting: there is exactly one place
that says how the base implementation is structured, and it is language-neutral.

The blocks live as .txt files (``prompts/lang/<name>/``) rather than Python
strings so prompt prose stays editable prose, next to the rest of the corpus.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from string import Template

_LANG_DIR = Path(__file__).parent / "prompts" / "lang"


@dataclass(frozen=True)
class LanguageProfile:
    """The language-specific slots of the prompt corpus.

    ``name`` doubles as the directory under ``prompts/lang/`` holding this
    language's blocks.
    """

    name: str
    # How the language is named in prose ("C++", "Rust").
    display_name: str
    # The primitive the base impl is told to reduce with, referenced by name in
    # otherwise language-neutral prose.
    parallel_primitive: str
    # The exact (non-floating) accumulator for DECIMAL/HUGEINT aggregates. The
    # correctness gate compares decimals exactly, so this is load-bearing.
    decimal_accum_type: str
    # Hot-loop antipatterns named in the expert-knowledge prompt.
    hot_parse_antipatterns: str
    # How the language expresses a no-alias guarantee to the compiler.
    aliasing_hint: str

    def block(self, key: str, /, **subst: object) -> str:
        """Render one language block (``prompts/lang/<name>/<key>.txt``).

        Blocks may reference the same ``$``-placeholders the prompt files do
        (e.g. ``${query_id}``); they are rendered here, because a value injected
        into a Template is not itself re-scanned for placeholders.

        The trailing newline is stripped so a block can sit on its own line in
        the host prompt without introducing a blank line after it.
        """
        text = (_LANG_DIR / self.name / f"{key}.txt").read_text()
        if subst:
            text = Template(text).substitute(**subst)
        return text.rstrip("\n")


CPP_PROFILE = LanguageProfile(
    name="cpp",
    display_name="C++",
    parallel_primitive="parallel_reduce",
    decimal_accum_type="__int128",
    hot_parse_antipatterns="std::stoi, std::stod, substr",
    aliasing_hint="restrict semantics",
)

RUST_PROFILE = LanguageProfile(
    name="rust",
    display_name="Rust",
    parallel_primitive="parallel_reduce",
    decimal_accum_type="i128",
    hot_parse_antipatterns="str::parse, String allocation, slicing into a new String",
    # Rust has no `restrict`. The equivalent signal is to hand the optimizer a
    # slice whose bounds it can prove, so it can drop the per-element bounds
    # check and vectorize -- iterators and chunks_exact rather than indexing.
    aliasing_hint="iterating slices directly (or chunks_exact) so the bounds check is elided",
)


_PROFILES: dict[str, LanguageProfile] = {
    CPP_PROFILE.name: CPP_PROFILE,
    RUST_PROFILE.name: RUST_PROFILE,
}


def get_language_profile(name: str) -> LanguageProfile:
    try:
        return _PROFILES[name]
    except KeyError as exc:
        raise ValueError(
            f"Unsupported engine language: {name!r} (have: {sorted(_PROFILES)})"
        ) from exc
