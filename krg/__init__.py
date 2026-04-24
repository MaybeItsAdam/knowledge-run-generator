"""
``krg`` — short alias for :mod:`knowledge_run_generator`.

Lets you write::

    import krg
    session = krg.Session()
    run = session.run("Ritz Hotel", "Gibson Square")

instead of the verbose package name. Everything is re-exported, so
``from krg import Session`` and ``from knowledge_run_generator import
Session`` refer to the same class.
"""

from knowledge_run_generator import (
    AliasIndex,
    Gazetteer,
    Run,
    Session,
    build_alias_index,
    cli,
    generate_run,
    normalise_street,
    preflight_run,
    semantic_snap,
)

__all__ = [
    "Session",
    "Run",
    "generate_run",
    "Gazetteer",
    "AliasIndex",
    "semantic_snap",
    "preflight_run",
    "build_alias_index",
    "normalise_street",
    "cli",
]
