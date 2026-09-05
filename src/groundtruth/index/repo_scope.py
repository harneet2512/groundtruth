"""Active-repo read scoping for a multi-repository graph.db (SM-9a consumer).

SM-9a (Go indexer, ``gt-index/internal/store/sqlite.go``) taught the indexer to
fold MULTIPLE repository roots into one ``graph.db``. Its schema partition is:

  * a ``repos(id, root, "commit")`` table — one row per indexed repo root;
  * a nullable ``repo_id`` column on every fact surface
    (nodes/edges/properties/assertions/closure/content_passages).

The load-bearing back-compat guarantee SM-9a designed for is *single-root
byte-identity*: a single-repo index NEVER writes a ``repos`` row and leaves every
``repo_id`` NULL, so the db is byte-identical to a pre-SM-9a one and every query
that omits ``repo_id`` behaves exactly as before.

Nothing on the Python READ side consumed that partition. On a MULTI-repo db a
bare ``WHERE name = ?`` (or an FTS ``MATCH``) returns candidates from the WRONG
repository — a cross-repo false positive delivered to the agent as fact. This
module is the missing consumer: it detects the multi-repo shape, resolves the
ACTIVE repo from the caller's ``repo_root``, and hands readers a SQL fragment
that scopes their node reads to that repo.

Contract (three states, exhaustive):

  * SINGLE-repo / legacy / no ``repos`` table  -> NO-OP.
    ``node_filter()`` returns ``("", ())`` so the query string is byte-identical
    and there is zero ranking or output change. This is the only shape production
    runs today, so the consumer adds nothing but one O(1) probe there.
  * MULTI-repo, active repo RESOLVED  ->  ``(" AND {a}repo_id = ?", (id,))``:
    reads are scoped to the active repo's partition.
  * MULTI-repo, active repo UNRESOLVED (repo_root absent / no match / ambiguous,
    or a malformed db missing ``nodes.repo_id``)  ->  FAIL-CLOSED
    ``(" AND 1=0", ())``: an empty, scoped-OUT result is strictly safer than
    leaking another repo's candidate as fact (correct-or-quiet).

The active repo is derived by matching the caller's ``repo_root`` against the
stored ``repos.root`` (normalized) — exactly the identity SM-9a persists. The
match is EXACT (normalized): a near-miss fails closed rather than guessing a
wrong repo. Pure sqlite; no model, no network; deterministic.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


def _normalize_root(root: str | None) -> str:
    """Canonical on-disk root for comparison: forward slashes, no trailing sep.

    Repo roots live on the same filesystem the graph was indexed on, so this is a
    separator/trailing-slash normalization only — it is NOT case-folded (POSIX
    paths are case-sensitive, and the graph + live checkout share one FS).
    """
    if not root:
        return ""
    return str(root).replace("\\", "/").rstrip("/")


def _repo_count(conn: sqlite3.Connection) -> int | None:
    """Row count of the ``repos`` table, or ``None`` when the table is ABSENT.

    ``None`` (table absent) is a legacy / pre-SM-9a db; ``0`` is a current
    single-root index (table present, empty). Both are single-repo no-ops, but
    the distinction is kept for clarity and testing.
    """
    try:
        row = conn.execute("SELECT COUNT(*) FROM repos").fetchone()
    except sqlite3.Error:
        return None
    return int(row[0]) if row and row[0] is not None else 0


def _column_present(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """True iff ``table`` carries ``column`` (PRAGMA table_info). Defensive: a
    multi-repo db is expected to have ``nodes.repo_id`` — if it does not (a
    malformed shape), the scope fails closed rather than pretending single-repo."""
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(" + table + ")").fetchall()}
    except sqlite3.Error:
        return False
    return column in cols


def _resolve_repo_id(conn: sqlite3.Connection, repo_root: str | None) -> int | None:
    """The ``repos.id`` whose normalized ``root`` EXACTLY equals ``repo_root``.

    Returns ``None`` when ``repo_root`` is empty, matches no stored root, or
    (defensively) matches more than one — every non-unique case is UNRESOLVED and
    handled fail-closed by the caller. Exact match only: a prefix/suffix guess
    could scope to the wrong repo, so a near-miss deliberately fails closed."""
    nr = _normalize_root(repo_root)
    if not nr:
        return None
    try:
        rows = conn.execute("SELECT id, root FROM repos").fetchall()
    except sqlite3.Error:
        return None
    matches = [
        int(r[0])
        for r in rows
        if r and r[0] is not None and r[1] is not None and _normalize_root(str(r[1])) == nr
    ]
    if len(matches) == 1:
        return matches[0]
    return None  # 0 matches (absent) or >1 (ambiguous) -> unresolved


@dataclass(frozen=True)
class RepoScope:
    """A resolved read scope for one graph.db + one active ``repo_root``.

    ``is_multi_repo``  — the db carries a populated ``repos`` partition.
    ``active_repo_id`` — the resolved active repo id, or ``None`` (unresolved).
    """

    is_multi_repo: bool
    active_repo_id: int | None

    @property
    def resolved(self) -> bool:
        """True when reads can be scoped to a concrete active repo. Always True on
        a single-repo db (nothing to scope); on multi-repo, iff an id resolved."""
        return (not self.is_multi_repo) or (self.active_repo_id is not None)

    def node_filter(self, alias: str = "") -> tuple[str, tuple]:
        """SQL fragment + params to AND into a query's WHERE, scoping ``nodes`` (or
        any fact surface with a ``repo_id`` column) to the active repo.

        ``alias`` qualifies the column (``"n"`` -> ``n.repo_id``); default is the
        bare ``repo_id``. Composition contract: the fragment is appended at the END
        of an existing WHERE clause and its params appended at the END of the param
        sequence (its ``?`` is the last placeholder).

          * single-repo -> ``("", ())``     : byte-identical, no-op.
          * multi + resolved -> ``(" AND {a}repo_id = ?", (id,))``.
          * multi + unresolved -> ``(" AND 1=0", ())`` : fail-closed, no rows.
        """
        if not self.is_multi_repo:
            return ("", ())
        a = (alias + ".") if alias else ""
        if self.active_repo_id is not None:
            return (" AND " + a + "repo_id = ?", (self.active_repo_id,))
        return (" AND 1=0", ())


# A shared single-repo NO-OP scope: readers with no db/repo context use this so
# every call site can unconditionally call node_filter() and get ("", ()).
NOOP_SCOPE = RepoScope(is_multi_repo=False, active_repo_id=None)


def for_read(conn: sqlite3.Connection, repo_root: str | None = "") -> RepoScope:
    """Build the active-repo read scope for ``conn`` and the caller's ``repo_root``.

    Runs a single O(1) probe of the ``repos`` table. On a single-root / legacy db
    (the only shape production runs today) it returns the no-op scope immediately —
    zero behavioral change. On a multi-repo db it resolves the active repo id from
    ``repo_root`` (fail-closed to unresolved when it cannot).
    """
    count = _repo_count(conn)
    if not count:  # None (legacy, no table) or 0 (current single-root) -> no-op
        return NOOP_SCOPE
    # Multi-repo. Require the partition column to scope; a malformed multi-repo db
    # missing nodes.repo_id cannot be scoped -> fail closed (unresolved).
    if not _column_present(conn, "nodes", "repo_id"):
        return RepoScope(is_multi_repo=True, active_repo_id=None)
    return RepoScope(is_multi_repo=True, active_repo_id=_resolve_repo_id(conn, repo_root))
