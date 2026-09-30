# FROZEN CONTRACT (KB0, D153): change it through a contract-change request in MEMORY/16-knowledge.md §8.
"""The keyword leg's languages, and the one Postgres expression that turns a chunk into a tsvector.

A knowledge base names a language (`KnowledgeBase.language`, default "simple");
each chunk stores it as plain text in `knowledge_chunks.lang`.

**Why plain text and a CASE, not a `regconfig` column cast.** The obvious
generated column, ``to_tsvector(lang::regconfig, …)``, is refused by
Postgres: casting text to regconfig looks the name up through `search_path`,
so it is only STABLE, and a generated column must be IMMUTABLE. Mapping the
text to regconfig *constants* in a CASE folds to fixed OIDs and is immutable.
The engine's ORM also stays portable: SQLite has no regconfig type.

`tsv_expression_sql()` is the single source of that expression. Cloud's
migration and OSS-on-Postgres's raw DDL both use it, and a cloud test holds the
migration's text equal to it.
"""
from __future__ import annotations

#: Text-search configurations present in every Postgres from 13 on. "simple" is
#: no stemming and no stop words: the safe default for mixed or unknown text.
TS_LANGUAGES: tuple[str, ...] = (
    "simple", "arabic", "danish", "dutch", "english", "finnish", "french", "german", "greek",
    "hungarian", "indonesian", "irish", "italian", "lithuanian", "nepali", "norwegian",
    "portuguese", "romanian", "russian", "spanish", "swedish", "tamil", "turkish",
)
DEFAULT_LANGUAGE = "simple"


def normalize_language(value: str | None) -> str:
    """A language name as stored: one of `TS_LANGUAGES`, else "simple"."""
    name = (value or "").strip().lower()
    return name if name in TS_LANGUAGES else DEFAULT_LANGUAGE


def regconfig_case_sql(lang_column: str = "lang") -> str:
    """`lang_column` mapped to a regconfig constant; anything unknown is 'simple'."""
    arms = " ".join(
        f"WHEN '{name}' THEN '{name}'::regconfig" for name in TS_LANGUAGES if name != DEFAULT_LANGUAGE
    )
    return f"CASE {lang_column} {arms} ELSE '{DEFAULT_LANGUAGE}'::regconfig END"


def tsv_expression_sql(lang_column: str = "lang", context_column: str = "context",
                       content_column: str = "content") -> str:
    """The Postgres expression for a chunk's tsvector: its context prefix and its content."""
    return (f"to_tsvector({regconfig_case_sql(lang_column)}, "
            f"coalesce({context_column}, '') || ' ' || coalesce({content_column}, ''))")
