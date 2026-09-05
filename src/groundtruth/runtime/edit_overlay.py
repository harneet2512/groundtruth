"""Transactional edit overlay — after an edit, downstream queries must never see a
STALE pre-edit graph presented as CURRENT truth (W6).

GroundTruth already SELECTS/RUNS covering tests (``covering_runner``), checks edit
SYNTAX (``edit_check``), and analyzes CONTRACT delta (``patch_delta``). But those
engines each read ``graph.db`` as-is. The moment an agent writes a file, ``graph.db``
is a snapshot of the PRE-EDIT world: a caller lookup, a contract, a covering-test
selection computed against it is a fact about code that no longer exists. This module
is the TRANSACTION that closes that window: it snapshots, applies the candidate edit,
REINDEXES (the repo's own ``gt-index`` binary, executor-routed so it runs where the
task tree lives), attributes ONLY the NEW diagnostics the edit introduced, stamps a
new ``graph_revision`` so every fact tagged with the old one is invalidated, and can
either COMMIT (retain) or ROLL BACK byte-exact.

PIPELINE (deliverable 1)::

    snapshot(files + graph_revision)
      -> apply candidate (write files)
      -> incremental reindex (gt-index -file per changed file; -root full fallback;
         missing binary => reindex UNAVAILABLE, graph STALE-FLAGGED, overlay still runs)
      -> diagnostic delta (edit_check before/after; DIFFERENTIAL: only NEW errors)
      -> contract delta (patch_delta at the NEW revision)
      -> commit (retain)  OR  rollback (byte-exact restoration, proven)

REVISIONS (deliverable 2). The overlay stamps a new ``graph_revision`` = the
content-hash of the reindexed ``graph.db``. :class:`OverlayResult` carries
``{pre_revision, post_revision, diagnostics_delta, contract_delta, capability_states}``,
every field provenance'd. When the graph could NOT be refreshed (reindex unavailable),
the edit signature is folded into ``post_revision`` so it necessarily differs from
``pre_revision`` — old graph facts are then correctly treated as STALE even though the
graph itself is frozen (fail toward freshness, never present stale as current).
:func:`mark_stale_envelopes` is the pure invalidation helper (envelope
``valid_until != post_revision`` => stale).

LANGUAGE MATRIX HONESTY (deliverable 3) — syntax checking mirrors ``edit_check``'s
own dispatch; it is NEVER guessed clean for a language we cannot soundly parse:

    ext          syntax path              capability
    -----------  -----------------------  -------------------------------------------
    .py          ast.parse (in-proc/exe)  SUPPORTED   (Python 3 always present)
    .go          gofmt -e                 SUPPORTED   (available iff gofmt on PATH)
    .js/.mjs/.cjs node --check             SUPPORTED   (available iff node on PATH)
    .rb          ruby -c                  SUPPORTED   (available iff ruby on PATH)
    .ts/.tsx     --                       UNAVAILABLE (tsc conflates syntax+type)
    .jsx         --                       UNAVAILABLE (JSX is not valid JS)
    .rs          --                       UNAVAILABLE (no fast parse-only rustc)
    .java        --                       UNAVAILABLE (javac needs classpath)
    other        --                       UNAVAILABLE (unknown -> never guess)

``capability_states["syntax"]`` reports the RUNTIME state per extension:
``"available"`` (a real ok/syntax_error verdict was obtained), ``"toolchain_missing"``
(a SUPPORTED language whose tool is absent — honest, never "clean"), or
``"unsupported"`` (a language edit_check cannot soundly check).

ROLLBACK COMPLETENESS (deliverable 4). Snapshot records raw BYTES of every touched
file (``None`` == the file did not exist) plus a byte copy of ``graph.db``; rollback
restores every file (deleting ones the edit created, pruning EMPTY directories the
apply created — a leftover empty dir is an importable namespace package; a created
dir holding FOREIGN content is preserved) and the db, byte-for-byte. Completeness is
VERIFIED by re-reading the disk, never assumed: any path whose bytes differ from the
snapshot is surfaced in ``capability_states["rollback_failed"]`` with
``capability_states["rollback"] = "incomplete"`` (``"<graph.db>"`` stands for the
graph itself), and ``post_revision`` then ADVANCES (edit signature folded under a
``rollback_incomplete`` domain tag) — a failed restoration is never presented as a
restored world. A committed transaction drops the backup; a VERIFIED-complete
rollback restores ``post_revision`` to ``pre_revision`` (the world is unchanged, so
nothing downstream is stale).

THE LAWS (identical to the rest of the enrichment stack):
- **Deterministic / LLM-free.** The :class:`OverlayResult` is a pure function of the
  inputs + the resulting db bytes: sorted lists, no wallclock, no randomness in any
  reported field (the internal db-backup path uses a token, never surfaced).
- **Correct-or-quiet + fail-open.** Any reindex/check fault degrades to a flagged
  capability, never a crash and never a fabricated verdict. The DEFAULT decision is
  COMMIT (retain the agent's edit) — the overlay REPORTS, it does not enforce.
- **DIFFERENTIAL law (B1/E1).** Only NEW, edit-attributed diagnostics enter
  ``diagnostics_delta``; a pre-existing (baseline) syntax error is NEVER blamed on
  the edit.
- **Unmeasured => advisory.** ``diagnostics_delta`` (the NEW-syntax-error class) is
  the ONE documented differential-blocking class — it is ``blocking_eligible`` but
  STILL ships ``measured=False`` (advisory in effect). Everything else is advisory.
- **Leak law.** No test identity ever enters a reported field — diagnostics carry the
  toolchain's own error text (``<gt-*>``-scrubbed by edit_check); contract-delta rows
  are already leak-filtered by patch_delta.

EXECUTOR CONTRACT (frozen, shared with test_runner/covering_runner/edit_check)::

    executor(cmd: list[str], cwd: str, timeout: int) -> tuple[int | None, str, str]
                                                        (exit_code, stdout, stderr)

``executor=None`` runs external PROCESS work (``gt-index``) via the host subprocess
(byte-identical to ``test_runner._run_subprocess``); an injected executor runs it
inside the task's own container. FILE snapshot/rollback operate on the local
filesystem view of ``repo_root`` — the same host-fs discipline ``patch_delta`` /
``edit_check`` use for source reads; the seam is responsible for running the overlay
where the task files live.

Not wired to any seam yet (ship-list / seam registration is OWED — a delivery-layer
follow-up, out of this module's ownership). The top-level convenience entry
:func:`apply_edit_transaction` is gated behind ``GT_EDIT_OVERLAY`` (unset/0 => a no-op
disabled result that touches NOTHING); the :class:`EditOverlay` primitive is always
usable/testable.

Deterministic, LLM-free, stdlib + the two GT engines only.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import struct
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Callable

from groundtruth.runtime.covering_runner import _connect_ro
from groundtruth.runtime.edit_check import _build_check_command, check_edit_syntax
from groundtruth.runtime.patch_delta import PatchDeltaResult, analyze_patch_delta

# The injectable execution boundary — the SAME frozen triple every enrichment engine
# builds to. ``None`` selects the host subprocess path for PROCESS work (gt-index).
Executor = Callable[[list[str], str, int], "tuple[int | None, str, str]"]

# --- reindex capability states (honest, never a guess) ---------------------
REINDEX_INCREMENTAL = "incremental"  # gt-index -file succeeded for every changed file
REINDEX_FULL = "full"  # a full gt-index -root reindex succeeded
REINDEX_UNAVAILABLE = "unavailable"  # no binary / all reindex attempts failed

# --- graph freshness states ------------------------------------------------
GRAPH_FRESH = "fresh"  # graph.db reflects the applied edit
GRAPH_STALE = "stale"  # reindex could not refresh the graph (FLAGGED)

# --- per-extension syntax capability ---------------------------------------
SYNTAX_AVAILABLE = "available"  # a real ok/syntax_error verdict was obtained
SYNTAX_TOOLCHAIN_MISSING = "toolchain_missing"  # SUPPORTED lang, tool absent (honest)
SYNTAX_UNSUPPORTED = "unsupported"  # a language edit_check cannot soundly check

# --- contract-delta capability ---------------------------------------------
CONTRACT_OK = "ok"  # ran against a FRESH graph
CONTRACT_STALE_GRAPH = "stale_graph"  # ran against a graph the edit did not refresh
CONTRACT_UNAVAILABLE = "unavailable"  # no graph.db to analyze

# Default timeouts (seconds). Reindex is bounded so a hung indexer cannot wedge the
# transaction; the diagnostic check inherits edit_check's own default.
_DEFAULT_REINDEX_TIMEOUT = 180
_DEFAULT_DIAG_TIMEOUT = 10

# Candidate host-side gt-index binary names, probed under <cwd>/gt-index/ when the
# tool is not on PATH and no executor is injected (host-run development / CI).
_GT_INDEX_NAMES = ("gt-index.exe", "gt-index")

# A verdict proves a CLEAN baseline (so an after-edit syntax_error is genuinely NEW)
# ONLY when we positively know the before state was fine. "unavailable" does NOT
# qualify (we could not prove the baseline clean) -> correct-or-quiet, never blame.
_CLEAN_BASELINE_VERDICTS = frozenset({"ok", "absent"})


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
def _norm(path: str) -> str:
    """Repo-relative canonical form: backslashes -> '/', a single leading './'
    stripped. Conservative (does NOT strip a leading '.' from dotfiles)."""
    p = (path or "").replace("\\", "/").strip()
    while p.startswith("./"):
        p = p[2:]
    return p


def _read_bytes(abs_path: str) -> bytes | None:
    """Raw bytes of ``abs_path`` for a byte-exact snapshot, or None if unreadable."""
    try:
        with open(abs_path, "rb") as fh:
            return fh.read()
    except OSError:
        return None


def _hash_file(path: str) -> str:
    """Streaming sha256 hex of a file's bytes, or ``""`` when it is not a file.

    The content-hash IS the ``graph_revision`` — deterministic given the db bytes,
    wallclock-free. (gt-index may embed non-reproducible metadata, so two logically
    identical reindexes can hash differently; that is conservative — it errs toward
    freshness, never toward presenting a stale graph as current.)"""
    if not path or not os.path.isfile(path):
        return ""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


def _edit_signature(applied: "Mapping[str, bytes | None]") -> str:
    """A deterministic content-hash of what the edit WROTE — sorted
    ``(path, sha256(after))`` records ('deleted' for a removed file). Folded into
    ``post_revision`` when the graph could not be refreshed (or an incomplete
    rollback left the world changed), so the revision advances on the CODE change.

    FRAMING (F3, Fable review): every field is prefixed by its 8-byte little-endian
    byte-length — the mirror of gt-index ``revEncodeRecord`` (revision.go) — so no
    field content (a ``:`` or newline in a path) can shift a field boundary. The
    old ``"\\n".join(f"{path}:{digest}")`` collided ``{"a": None, "b": X}`` with
    ``{"a:deleted\\nb": X}``; length-framing is injection-proof by construction."""
    h = hashlib.sha256()
    for path in sorted(applied):
        content = applied[path]
        digest = "deleted" if content is None else hashlib.sha256(content).hexdigest()
        for fld in (path, digest):
            raw = fld.encode("utf-8")
            h.update(struct.pack("<Q", len(raw)))
            h.update(raw)
    return h.hexdigest()


def _lang_supported(ext: str) -> bool:
    """True iff ``edit_check`` can SOUNDLY syntax-check this extension — derived from
    its OWN dispatch (``_build_check_command``) so the matrix can never drift from the
    checker. ``.py`` is supported (in-process ast.parse) even though its command form
    also exists."""
    return _build_check_command(ext, "_probe_") is not None


def _syntax_state(ext: str, verdict: str) -> str:
    """Honest per-extension capability from an actual check verdict."""
    if not _lang_supported(ext):
        return SYNTAX_UNSUPPORTED
    if verdict in ("ok", "syntax_error"):
        return SYNTAX_AVAILABLE
    return SYNTAX_TOOLCHAIN_MISSING


# precedence for aggregating multiple files of the same extension: a real verdict
# beats a missing tool beats unsupported (deterministic regardless of iteration order).
_SYNTAX_RANK = {SYNTAX_AVAILABLE: 2, SYNTAX_TOOLCHAIN_MISSING: 1, SYNTAX_UNSUPPORTED: 0}


def capability_matrix() -> dict[str, str]:
    """The STATIC language matrix (deliverable 3): extension -> ``"supported"`` /
    ``"unsupported"`` for whether ``edit_check`` can soundly syntax-check it, decoupled
    from whether the toolchain happens to be installed. Sorted for determinism."""
    exts = (
        ".py",
        ".go",
        ".js",
        ".mjs",
        ".cjs",
        ".rb",
        ".ts",
        ".tsx",
        ".jsx",
        ".rs",
        ".java",
    )
    return {e: ("supported" if _lang_supported(e) else "unsupported") for e in sorted(exts)}


def mark_stale_envelopes(
    envelopes: "list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...] | None",
    post_revision: str,
) -> list[dict[str, Any]]:
    """Pure stale-fact invalidation (deliverable 2): return COPIES of each envelope
    dict with a ``"stale"`` bool set.

    An envelope is STALE iff its freshness token (``valid_until``, falling back to
    ``graph_revision``) is known AND differs from ``post_revision``. Unprovable
    staleness — either side empty — is NEVER marked stale (fail-open, parity with
    ``submit_gate._is_fresh``: correct-or-quiet never fabricates a stale). Inputs are
    not mutated; output order matches input order. Deterministic, no I/O."""
    out: list[dict[str, Any]] = []
    pr = str(post_revision or "")
    for env in envelopes or []:
        d = dict(env) if isinstance(env, Mapping) else {}
        token = str(d.get("valid_until") or d.get("graph_revision") or "")
        d["stale"] = bool(token) and bool(pr) and token != pr
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# SM-4 — the LIGHTWEIGHT episode overlay (freshness against the CURRENT edit state).
#
# The certified base ``graph.db`` is a SNAPSHOT of the pre-edit world and is ro-mounted
# (immutable) in the real run. After the agent edits a file, a fact READ from that base
# about the file describes structure that may no longer exist — but the base's revision
# never moves, so the class-level freshness token (``valid_until``) can't stale it. This
# overlay closes that window WITHOUT rebuilding the base: it re-derives, per edited file,
# whether the base's recorded def structure still matches the file's CURRENT content
# (a read-only query of ONLY that file's nodes + a scan of the current bytes — O(changed
# file), NEVER O(repo)). A structurally-changed file is marked ``changed`` — the SAME
# :func:`mark_stale_envelopes` verdict, so a base-read fact about it is dropped downstream
# (Gateway ``route_delivery``). A fact about an UNEDITED file is untouched; an UNPROVABLE
# case (no base nodes / db fault) fails QUIET (``changed=False`` — never a fabricated stale).
# ---------------------------------------------------------------------------
# The graph node labels that carry a definition SIGNATURE (mirror of
# ``gateway._DEF_LABELS`` — the classes whose recorded shape a fact can be ABOUT).
_OVERLAY_DEF_LABELS = (
    "Function",
    "Method",
    "Class",
    "Interface",
    "Struct",
    "Enum",
    "Trait",
    "Constructor",
)


def _overlay_norm_path(path: str) -> str:
    """Overlay key normalization: repo-relative, ``\\`` -> ``/``, no leading ``./`` or
    ``/`` (parity with ``gateway._norm_fp`` so a key the seam STORES and the Gateway's
    ``env.target`` LOOKUP agree for the same file)."""
    return _norm(path).lstrip("/")


def _collapse_ws(s: str) -> str:
    """Collapse every run of whitespace to a single space + strip. A signature's PRESENCE
    in the current file becomes insensitive to reindent/reformat, while a genuine token
    change (an added/removed/renamed parameter, a changed return type) still fails the
    substring match — so a body-only or comment-only edit stays FRESH but a signature
    change stales."""
    return " ".join((s or "").split())


def _graph_def_signatures(graph_db: str, rel_file: str) -> tuple[str, ...]:
    """The definition SIGNATURES (fallback: the def NAME) the certified base ``graph.db``
    recorded for ``rel_file`` — NON-test def nodes only. Read-only (``_connect_ro`` — the
    ro-mounted base is NEVER written), bounded to ONE file's nodes; correct-or-quiet -> ()
    on any db/schema fault or no matching node.

    O(changed file), NEVER O(repo): the predicate is an INDEXED EQUALITY on ``file_path``
    (``AND file_path = ?``), so the SQLite planner uses the base graph.db's own baked index
    (``idx_nodes_file``, present on every gt-index db) to return ONLY the target file's rows —
    it never materializes the whole ``nodes`` table into Python. The equality is exact because
    gt-index stores ``file_path`` in ONE deterministic form — repo-relative, forward-slashed
    (``walker.go``: ``filepath.Rel(root, path)`` -> ``filepath.ToSlash``) — the SAME form the
    seam's ``_to_repo_rel``/``_overlay_norm_path`` produce (the indexer itself queries the
    identical ``WHERE file_path = ?``, resolver/api_edges.go). A stored form that ever differs
    simply returns () -> the overlay is UNKNOWN for that file -> fail-quiet (never a false stale
    drop; degrades to pre-SM-4). The per-row normalized-equality check is retained as an exactness
    backstop over the already-narrow result set."""
    db = graph_db or ""
    if not db or not os.path.isfile(db):
        return ()
    target = _overlay_norm_path(rel_file)
    if not target:
        return ()
    con = _connect_ro(db)
    if con is None:
        return ()
    labels_sql = ",".join("?" * len(_OVERLAY_DEF_LABELS))
    try:
        # INDEXED EQUALITY on file_path (idx_nodes_file) — bounds the scan to this ONE file's
        # nodes, not the whole table. `target` is the exact repo-relative forward-slashed form
        # gt-index stores (walker.go). label/is_test further narrow within the file.
        rows = con.execute(
            f"SELECT file_path, name, signature FROM nodes "
            f"WHERE file_path = ? AND COALESCE(is_test,0)=0 AND label IN ({labels_sql})",
            (target, *_OVERLAY_DEF_LABELS),
        ).fetchall()
    except sqlite3.Error:
        return ()
    finally:
        con.close()
    out: list[str] = []
    for fp, name, sig in rows:
        # backstop: every returned row already has file_path == target (indexed equality); the
        # re-normalized equality guards against any stored-form surprise (defensive, cheap on a
        # handful of rows — never a full scan).
        if _overlay_norm_path(str(fp or "")) != target:
            continue
        text = _collapse_ws(str(sig or "")) or _collapse_ws(str(name or ""))
        if text:
            out.append(text)
    return tuple(sorted(set(out)))


def build_episode_overlay_entry(
    graph_db: str,
    rel_file: str,
    current_content: str,
) -> dict[str, Any]:
    """SM-4 deliverable 1 — the per-file episode overlay entry for ONE edited file.

    Re-derives whether the base ``graph.db``'s recorded structure for ``rel_file`` still
    matches the file's CURRENT content, WITHOUT rebuilding the base (a read-only query of
    ONLY this file's def nodes + a scan of ``current_content``; O(changed file), never a
    write). Returns::

        {"file", "state", "graph_sig", "current_sig", "changed", "overlay_rev", "n_defs"}

    ``changed`` (deliverable 2) IS the :func:`mark_stale_envelopes` verdict: a synthetic
    envelope stamped with the BASE structural signature (``graph_sig``) is marked stale
    against the CURRENT structural signature (``current_sig``) — they differ iff a recorded
    def signature no longer appears in the current file. ``state`` is ``"available"`` when
    the base held ≥1 signature for the file (a real comparison happened) or ``"unknown"``
    (no db / no nodes / unreadable) — in the UNKNOWN case ``changed`` is False (deliverable
    4: fail QUIET, keep current behavior, never drop on an unprovable staleness). NEVER
    raises."""
    file_key = _overlay_norm_path(rel_file)
    content = current_content if isinstance(current_content, str) else ""
    overlay_rev = hashlib.sha256(content.encode("utf-8", "replace")).hexdigest()[:16]
    try:
        graph_defs = _graph_def_signatures(graph_db, rel_file)
    except Exception:  # noqa: BLE001 -- correct-or-quiet
        graph_defs = ()
    if not graph_defs:
        return {
            "file": file_key,
            "state": "unknown",
            "graph_sig": "",
            "current_sig": "",
            "changed": False,
            "overlay_rev": overlay_rev,
            "n_defs": 0,
        }
    norm_content = _collapse_ws(content)
    present = tuple(sorted(g for g in graph_defs if g and g in norm_content))
    graph_sig = hashlib.sha256("\x00".join(graph_defs).encode("utf-8")).hexdigest()[:16]
    current_sig = hashlib.sha256("\x00".join(present).encode("utf-8")).hexdigest()[:16]
    # deliverable-2 wiring: mark_stale_envelopes IS the verdict. A base-stamped synthetic
    # envelope is stale iff the current structural signature differs from the base's.
    verdict = mark_stale_envelopes([{"valid_until": graph_sig}], current_sig)
    changed = bool(verdict and verdict[0].get("stale"))
    return {
        "file": file_key,
        "state": "available",
        "graph_sig": graph_sig,
        "current_sig": current_sig,
        "changed": changed,
        "overlay_rev": overlay_rev,
        "n_defs": len(graph_defs),
    }


def compose_episode_revision(
    base_revision: str,
    overlay: "Mapping[str, Any] | None",
) -> str:
    """SM-4 deliverable 1 — ONE current episode revision = the certified base composite
    revision folded with each overlaid file's per-file revision (``overlay_rev``), in
    sorted-path order. Empty overlay -> the base revision UNCHANGED (byte-identical to the
    no-overlay world). Deterministic; the base is never mutated; no I/O."""
    base = str(base_revision or "")
    if not overlay:
        return base
    h = hashlib.sha256()
    h.update(base.encode("utf-8"))
    for path in sorted(overlay):
        entry = overlay[path]
        rev = str(entry.get("overlay_rev", "")) if isinstance(entry, Mapping) else ""
        h.update(b"\x00")
        h.update(path.encode("utf-8"))
        h.update(b"\x00")
        h.update(rev.encode("utf-8"))
    return h.hexdigest()[:16]


def overlay_target_stale(
    overlay: "Mapping[str, Any] | None",
    target_file: str,
) -> bool:
    """True iff a fact whose target is ``target_file`` is STALE against the episode overlay
    — the file was edited this episode AND its recorded structure changed (``entry['changed']``,
    the mark_stale_envelopes verdict). False when the file is not overlaid (UNEDITED ->
    unaffected) or its entry is UNKNOWN (fail quiet). Pure — the Gateway's drop decision reads
    exactly this predicate's per-file verdict."""
    if not overlay or not target_file:
        return False
    entry = overlay.get(_overlay_norm_path(target_file))
    return bool(isinstance(entry, Mapping) and entry.get("changed"))


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class DiagnosticDelta:
    """One NEW, edit-attributed syntax error (the DIFFERENTIAL result).

    This is the ONE documented differential-blocking class: ``blocking_eligible`` is
    True, but ``measured`` STILL ships False (unmeasured => advisory-in-effect). The
    ``diagnostic`` is the toolchain's own error text (bounded + ``<gt-*>``-scrubbed by
    edit_check); it carries no test identity (it checks a SOURCE edit)."""

    file: str
    language: str
    verdict: str  # always "syntax_error" (NEW-error entries only)
    diagnostic: str
    blocking_eligible: bool = True
    measured: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "language": self.language,
            "verdict": self.verdict,
            "diagnostic": self.diagnostic,
            "blocking_eligible": self.blocking_eligible,
            "measured": self.measured,
        }


@dataclass(frozen=True)
class OverlayResult:
    """The transaction outcome — a pure, provenance'd function of the inputs.

    ``pre_revision`` / ``post_revision`` are content-hashes of ``graph.db`` at snapshot
    and at end-of-transaction; on a rolled-back transaction ``post_revision ==
    pre_revision`` (the world is restored). ``capability_states`` is the honest
    availability of each capability (reindex / graph / contract / per-ext syntax).
    ``disabled`` marks the flag-off no-op path."""

    committed: bool
    rolled_back: bool
    pre_revision: str
    post_revision: str
    edited_files: tuple[str, ...]
    diagnostics_delta: tuple[DiagnosticDelta, ...]
    contract_delta: PatchDeltaResult
    capability_states: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    disabled: bool = False

    @property
    def graph_stale(self) -> bool:
        """True when the graph could not be refreshed to reflect the edit (FLAGGED)."""
        return self.capability_states.get("graph") == GRAPH_STALE

    def to_dict(self) -> dict[str, Any]:
        return {
            "committed": self.committed,
            "rolled_back": self.rolled_back,
            "pre_revision": self.pre_revision,
            "post_revision": self.post_revision,
            "edited_files": list(self.edited_files),
            "diagnostics_delta": [d.to_dict() for d in self.diagnostics_delta],
            "contract_delta": {
                "signature_mismatches": len(self.contract_delta.signature_mismatches),
                "companion_surfaces": len(self.contract_delta.companion_surfaces),
                "cochange_partners": len(self.contract_delta.cochange_partners),
            },
            "capability_states": dict(self.capability_states),
            "reason": self.reason,
            "disabled": self.disabled,
        }


# ---------------------------------------------------------------------------
# The transaction primitive
# ---------------------------------------------------------------------------
class EditOverlay:
    """A single edit transaction over a repo tree + its ``graph.db``.

    Lifecycle: ``snapshot(candidate)`` -> ``apply()`` -> ``reindex()`` ->
    ``diagnostics_delta()`` -> ``contract_delta()`` -> ``commit()`` | ``rollback()`` ->
    ``result(...)``. Each method is deterministic and never raises for expected
    failures (they degrade to flagged capabilities).

    ``candidate`` maps a repo-relative path to its NEW content (``str``/``bytes``), or
    ``None`` to DELETE the file. Before-content is read from disk during ``snapshot``.
    """

    def __init__(
        self,
        repo_root: str,
        graph_db: str,
        *,
        executor: Executor | None = None,
        gt_index_bin: str | None = None,
        reindex_timeout: int = _DEFAULT_REINDEX_TIMEOUT,
        diag_timeout: int = _DEFAULT_DIAG_TIMEOUT,
    ) -> None:
        self._repo_root = repo_root
        self._graph_db = graph_db
        self._executor = executor
        self._gt_index_bin = gt_index_bin
        self._reindex_timeout = int(reindex_timeout)
        self._diag_timeout = int(diag_timeout)

        self._candidate: dict[str, "str | bytes | None"] = {}
        self._orig: dict[str, bytes | None] = {}
        self._applied: dict[str, bytes | None] = {}
        self._before_verdicts: dict[str, str] = {}
        self._after_verdicts: dict[str, str] = {}
        # Directories apply() CREATED (absolute, inside repo_root) — pruned (when
        # empty) on rollback so no namespace-package residue survives (F2).
        self._created_dirs: list[str] = []
        # Rollback attestation (F1): VERIFIED after the restore pass, never assumed.
        self._rollback_state = "not_attempted"  # not_attempted | complete | incomplete
        self._rollback_failed: tuple[str, ...] = ()

        self._db_existed = False
        self._db_backup: str | None = None
        self._pre_revision = ""

        self._reindex_state = REINDEX_UNAVAILABLE
        self._graph_state = GRAPH_STALE
        self._contract_state = CONTRACT_UNAVAILABLE

        self._snapshot_done = False
        self._applied_done = False
        self._committed = False
        self._rolled_back = False

    # --- process execution (executor-routed) -------------------------------
    def _run(self, cmd: list[str], cwd: str, timeout: int) -> "tuple[int | None, str, str]":
        """Run external PROCESS work through the executor (container) or the host
        subprocess. Any fault -> ``(None, "", "")`` (treated as failure). Never
        raises — a transaction must never die on a spawn error."""
        if self._executor is None:
            from groundtruth.runtime.test_runner import _run_subprocess

            try:
                rc, out, err = _run_subprocess(cmd, cwd, timeout)
            except Exception:  # noqa: BLE001 -- timeout / OSError / spawn failure
                return None, "", ""
            return int(rc), out or "", err or ""
        try:
            ret = self._executor(list(cmd), cwd, timeout)
        except Exception:  # noqa: BLE001 -- a buggy executor is a spawn failure
            return None, "", ""
        if not isinstance(ret, (tuple, list)) or len(ret) != 3:
            return None, "", ""
        rc_raw, out, err = ret
        rc = rc_raw if isinstance(rc_raw, int) and not isinstance(rc_raw, bool) else None
        return rc, ("" if out is None else str(out)), ("" if err is None else str(err))

    def _resolve_bin(self) -> str | None:
        """Locate the ``gt-index`` binary. Explicit arg wins; then ``GT_INDEX_BIN``;
        an injected executor TRUSTS the container PATH ("gt-index"); else the host
        PATH; else a ``<cwd>/gt-index/`` probe. None => host binary absent."""
        if self._gt_index_bin:
            return self._gt_index_bin
        env = os.environ.get("GT_INDEX_BIN")
        if env and os.path.isfile(env):
            return env
        if self._executor is not None:
            return "gt-index"  # runs in the container; availability is the exit code
        found = shutil.which("gt-index")
        if found:
            return found
        for name in _GT_INDEX_NAMES:
            cand = os.path.join(os.getcwd(), "gt-index", name)
            if os.path.isfile(cand):
                return cand
        return None

    # --- stage 1: snapshot -------------------------------------------------
    def snapshot(self, candidate: "Mapping[str, str | bytes | None]") -> None:
        """Record byte-exact originals + baseline syntax verdicts + ``pre_revision``
        + a byte copy of ``graph.db``. Computes the BEFORE diagnostics on the on-disk
        (pre-edit) state — must run before :meth:`apply`."""
        self._candidate = {_norm(k): v for k, v in (candidate or {}).items()}
        for path in sorted(self._candidate):
            abs_p = os.path.join(self._repo_root, path)
            if os.path.isfile(abs_p):
                self._orig[path] = _read_bytes(abs_p)
                res = check_edit_syntax(
                    path,
                    self._repo_root,
                    executor=self._executor,
                    timeout=self._diag_timeout,
                )
                self._before_verdicts[path] = str(res.get("verdict") or "unavailable")
            else:
                self._orig[path] = None  # new file: no prior content
                self._before_verdicts[path] = "absent"  # a new file has no baseline error

        self._db_existed = os.path.isfile(self._graph_db)
        self._pre_revision = _hash_file(self._graph_db)
        if self._db_existed:
            backup = f"{self._graph_db}.gtoverlay.{uuid.uuid4().hex[:12]}.bak"
            try:
                shutil.copy2(self._graph_db, backup)
                self._db_backup = backup
            except OSError:
                self._db_backup = None  # cannot back up -> rollback of db is best-effort
        self._snapshot_done = True

    def _missing_parents(self, abs_p: str) -> list[str]:
        """The ancestor directories of ``abs_p`` that do NOT yet exist, bounded
        STRICTLY inside ``repo_root`` (never a path at/above the root — rollback
        must never prune outside the tree it owns). Deepest-first order."""
        root_abs = os.path.normcase(os.path.abspath(self._repo_root))
        out: list[str] = []
        cur = os.path.dirname(os.path.abspath(abs_p))
        while cur and not os.path.isdir(cur):
            norm = os.path.normcase(cur)
            if norm == root_abs or not norm.startswith(root_abs + os.sep):
                break  # at/above/outside the repo root -> never tracked
            out.append(cur)
            nxt = os.path.dirname(cur)
            if nxt == cur:
                break
            cur = nxt
        return out

    # --- stage 2: apply ----------------------------------------------------
    def apply(self) -> None:
        """Write the candidate to disk (create/overwrite, or delete on ``None``).
        Directories that must be CREATED for a new file are recorded so rollback
        can prune them (F2: an empty leftover dir is an importable namespace
        package — residue is a behavioral difference)."""
        if not self._snapshot_done:
            raise RuntimeError("EditOverlay.apply() called before snapshot()")
        for path in sorted(self._candidate):
            after = self._candidate[path]
            abs_p = os.path.join(self._repo_root, path)
            if after is None:
                if os.path.isfile(abs_p):
                    os.remove(abs_p)
                self._applied[path] = None
                continue
            parent = os.path.dirname(abs_p)
            if parent:
                for created in self._missing_parents(abs_p):
                    if created not in self._created_dirs:
                        self._created_dirs.append(created)
                os.makedirs(parent, exist_ok=True)
            data = after.encode("utf-8") if isinstance(after, str) else bytes(after)
            with open(abs_p, "wb") as fh:
                fh.write(data)
            self._applied[path] = data
        self._applied_done = True

    # --- stage 3: reindex --------------------------------------------------
    def reindex(self) -> tuple[str, str]:
        """Refresh ``graph.db`` for the applied edit. Returns ``(reindex_state,
        graph_state)``. Incremental (``-file`` per changed file) is preferred; a full
        (``-root``) reindex is the fallback when the db is absent, a file was deleted,
        or any incremental step failed. Missing binary / total failure => the graph is
        left STALE and FLAGGED (the overlay keeps functioning)."""
        if not self._applied_done:
            raise RuntimeError("EditOverlay.reindex() called before apply()")
        binary = self._resolve_bin()
        if binary is None:
            self._reindex_state, self._graph_state = REINDEX_UNAVAILABLE, GRAPH_STALE
            return self._reindex_state, self._graph_state

        changed = sorted(self._candidate)
        deleted = any(self._applied.get(p) is None for p in changed)
        db_present = os.path.isfile(self._graph_db)
        need_full = deleted or not db_present or not changed

        used: str | None = None
        if not need_full:
            all_ok = True
            for rel in changed:
                rc, _out, _err = self._run_reindex(binary, rel)
                if rc != 0:
                    all_ok = False
                    break
            used = REINDEX_INCREMENTAL if all_ok else None

        if used is None:  # full reindex (needed, or an incremental step failed)
            rc, _out, _err = self._run_reindex(binary, None)
            if rc == 0 and os.path.isfile(self._graph_db):
                used = REINDEX_FULL
            else:
                self._reindex_state, self._graph_state = REINDEX_UNAVAILABLE, GRAPH_STALE
                return self._reindex_state, self._graph_state

        self._reindex_state, self._graph_state = used, GRAPH_FRESH
        return self._reindex_state, self._graph_state

    def _run_reindex(self, binary: str, rel_file: str | None) -> "tuple[int | None, str, str]":
        """One ``gt-index`` invocation, built to the frozen CLI contract
        ``gt-index -root R -output G.db [-file F]``."""
        cmd = [binary, "-root", self._repo_root, "-output", self._graph_db]
        if rel_file is not None:
            cmd += ["-file", rel_file]
        return self._run(cmd, self._repo_root, self._reindex_timeout)

    # --- stage 4: diagnostic delta (DIFFERENTIAL) --------------------------
    def diagnostics_delta(self) -> list[DiagnosticDelta]:
        """NEW, edit-attributed syntax errors only (the differential). A file enters
        the delta iff its AFTER verdict is ``syntax_error`` AND its BEFORE verdict
        proved a clean baseline (``ok``/``absent``). A pre-existing error (before was
        also ``syntax_error``) or an unprovable baseline (before ``unavailable``) is
        NEVER attributed — correct-or-quiet, never baseline-blame."""
        if not self._applied_done:
            raise RuntimeError("EditOverlay.diagnostics_delta() called before apply()")
        out: list[DiagnosticDelta] = []
        for path in sorted(self._candidate):
            if self._applied.get(path) is None:  # deleted -> no after content to check
                self._after_verdicts[path] = "absent"
                continue
            res = check_edit_syntax(
                path,
                self._repo_root,
                executor=self._executor,
                timeout=self._diag_timeout,
            )
            verdict = str(res.get("verdict") or "unavailable")
            self._after_verdicts[path] = verdict
            before = self._before_verdicts.get(path, "absent")
            if verdict == "syntax_error" and before in _CLEAN_BASELINE_VERDICTS:
                out.append(
                    DiagnosticDelta(
                        file=path,
                        language=str(res.get("language") or ""),
                        verdict="syntax_error",
                        diagnostic=str(res.get("diagnostic") or ""),
                    )
                )
        out.sort(key=lambda d: (d.file, d.language))
        return out

    # --- stage 5: contract delta (at the NEW revision) ---------------------
    def contract_delta(self) -> PatchDeltaResult:
        """Static contract-delta at the reindexed revision (``patch_delta``). Runs
        against ``graph.db`` as it stands post-reindex; the ``contract`` capability
        records whether that graph was FRESH or STALE. Self-gated by ``GT_PATCH_DELTA``
        (off => empty). Never raises."""
        if not self._applied_done:
            raise RuntimeError("EditOverlay.contract_delta() called before apply()")
        edited_files: dict[str, tuple[str | None, str]] = {}
        for path in self._candidate:
            before_b = self._orig.get(path)
            before_s = None if before_b is None else before_b.decode("utf-8", "replace")
            after_b = self._applied.get(path)
            after_s = "" if after_b is None else after_b.decode("utf-8", "replace")
            edited_files[path] = (before_s, after_s)
        if not os.path.isfile(self._graph_db):
            self._contract_state = CONTRACT_UNAVAILABLE
            return PatchDeltaResult()
        self._contract_state = (
            CONTRACT_OK if self._graph_state == GRAPH_FRESH else CONTRACT_STALE_GRAPH
        )
        try:
            return analyze_patch_delta(edited_files, self._repo_root, self._graph_db)
        except Exception:  # noqa: BLE001 -- correct-or-quiet
            return PatchDeltaResult()

    # --- stage 6: commit / rollback ---------------------------------------
    def commit(self) -> None:
        """Retain the applied edit; drop the db backup."""
        self._cleanup_backup()
        self._committed = True

    def rollback(self) -> None:
        """Byte-exact restoration of every touched file AND ``graph.db``, with a
        VERIFIED attestation (F1, Fable review).

        Each snapshot original is rewritten verbatim; a file the edit CREATED
        (original ``None``) is deleted; directories apply() created are pruned when
        empty (F2 — a created dir that gained FOREIGN content is preserved). The db
        is restored from its byte backup, or deleted if it did not exist at snapshot.

        Restore faults are swallowed per-path (never raises), but completeness is
        then PROVEN by re-reading the disk: any path whose bytes do not match the
        snapshot lands in ``rollback_failed`` and the transaction is attested
        ``incomplete`` — a failed restoration is NEVER presented as a restored
        world (post_revision advances; see :meth:`_post_revision`)."""
        for path in sorted(self._orig):
            abs_p = os.path.join(self._repo_root, path)
            orig = self._orig[path]
            if orig is None:
                if os.path.isfile(abs_p):
                    try:
                        os.remove(abs_p)
                    except OSError:
                        pass
                continue
            parent = os.path.dirname(abs_p)
            if parent:
                try:
                    os.makedirs(parent, exist_ok=True)
                except OSError:
                    pass
            try:
                with open(abs_p, "wb") as fh:
                    fh.write(orig)
            except OSError:
                pass
        # F2: prune directories apply() created, deepest-first, EMPTY ones only —
        # os.rmdir refuses a non-empty dir, so foreign content is never destroyed.
        for created in sorted(
            set(self._created_dirs),
            key=lambda d: (d.count(os.sep) + d.count("/"), len(d)),
            reverse=True,
        ):
            try:
                os.rmdir(created)
            except OSError:
                pass  # non-empty (foreign content) or already gone -> keep quiet
        # restore the db
        if self._db_backup is not None and os.path.isfile(self._db_backup):
            try:
                shutil.copy2(self._db_backup, self._graph_db)
            except OSError:
                pass
        elif not self._db_existed and os.path.isfile(self._graph_db):
            try:
                os.remove(self._graph_db)
            except OSError:
                pass
        self._cleanup_backup()
        # F1: VERIFY the restoration — the attestation is read back from disk,
        # never assumed from the absence of exceptions.
        failed: list[str] = []
        for path in sorted(self._orig):
            abs_p = os.path.join(self._repo_root, path)
            orig = self._orig[path]
            if orig is None:
                if os.path.exists(abs_p):
                    failed.append(path)
            elif _read_bytes(abs_p) != orig:
                failed.append(path)
        if self._db_existed:
            if _hash_file(self._graph_db) != self._pre_revision:
                failed.append("<graph.db>")
        elif os.path.isfile(self._graph_db):
            failed.append("<graph.db>")
        self._rollback_failed = tuple(sorted(failed))
        self._rollback_state = "incomplete" if failed else "complete"
        self._rolled_back = True

    def _cleanup_backup(self) -> None:
        if self._db_backup and os.path.isfile(self._db_backup):
            try:
                os.remove(self._db_backup)
            except OSError:
                pass
        self._db_backup = None

    # --- result assembly ---------------------------------------------------
    def _syntax_capabilities(self) -> dict[str, str]:
        """Per-extension syntax capability, aggregated deterministically over the
        touched files (a real verdict beats a missing tool beats unsupported)."""
        agg: dict[str, str] = {}
        for path in sorted(self._candidate):
            ext = os.path.splitext(path)[1].lower()
            # A deleted file yields after="absent" (no content to check) — fall back
            # to the BEFORE verdict (the tool DID run on that extension at snapshot).
            # Neither side checked (candidate-delete of a nonexistent file) -> skip:
            # "absent" must never masquerade as a toolchain state.
            verdict = self._after_verdicts.get(path, "")
            if verdict in ("", "absent"):
                verdict = self._before_verdicts.get(path, "")
            if verdict in ("", "absent"):
                continue
            state = _syntax_state(ext, verdict)
            if ext not in agg or _SYNTAX_RANK[state] > _SYNTAX_RANK[agg[ext]]:
                agg[ext] = state
        return dict(sorted(agg.items()))

    def _post_revision(self) -> str:
        """End-of-transaction revision. Rolled back COMPLETE (verified) => equals
        ``pre_revision`` (world restored). Rolled back INCOMPLETE => the world still
        differs from the snapshot, so the revision MUST advance (F1: returning
        ``pre_revision`` would be a false "world restored" attestation) — the edit
        signature is folded under a dedicated domain tag. Committed + fresh graph =>
        the db content-hash. Committed + STALE graph => the db hash folded with the
        edit signature, so the revision still advances on the code change and old
        graph facts are invalidated."""
        if self._rolled_back:
            if not self._rollback_failed:
                return self._pre_revision
            db_hash = _hash_file(self._graph_db)
            edit_sig = _edit_signature(self._applied)
            return hashlib.sha256(
                (db_hash + "\x00rollback_incomplete\x00" + edit_sig).encode("utf-8")
            ).hexdigest()
        db_hash = _hash_file(self._graph_db)
        if self._graph_state == GRAPH_FRESH:
            return db_hash
        edit_sig = _edit_signature(self._applied)
        return hashlib.sha256((db_hash + "\x00stale\x00" + edit_sig).encode("utf-8")).hexdigest()

    def result(
        self,
        diagnostics: "list[DiagnosticDelta] | None" = None,
        contract: PatchDeltaResult | None = None,
    ) -> OverlayResult:
        """Assemble the deterministic :class:`OverlayResult` from the current state."""
        capability_states: dict[str, Any] = {
            "reindex": self._reindex_state,
            "graph": self._graph_state,
            "contract": self._contract_state,
            "syntax": self._syntax_capabilities(),
            # F1: the VERIFIED rollback attestation + the paths that failed to
            # restore ("<graph.db>" stands for the graph itself).
            "rollback": self._rollback_state,
            "rollback_failed": list(self._rollback_failed),
        }
        if self._rolled_back:
            reason = "rollback_incomplete" if self._rollback_failed else "rolled_back"
        else:
            reason = "committed" if self._committed else "open"
        return OverlayResult(
            committed=self._committed,
            rolled_back=self._rolled_back,
            pre_revision=self._pre_revision,
            post_revision=self._post_revision(),
            edited_files=tuple(sorted(self._candidate)),
            diagnostics_delta=tuple(diagnostics or ()),
            contract_delta=contract if contract is not None else PatchDeltaResult(),
            capability_states=capability_states,
            reason=reason,
        )


# ---------------------------------------------------------------------------
# Flag-gated convenience entry point
# ---------------------------------------------------------------------------
def _flag_on() -> bool:
    return os.environ.get("GT_EDIT_OVERLAY", "").strip().lower() in {"1", "true", "yes", "on"}


def _default_decide(
    diagnostics: list[DiagnosticDelta], contract: PatchDeltaResult, overlay: EditOverlay
) -> bool:
    """Default decision: COMMIT (fail-open — retain the agent's edit; the overlay
    REPORTS, it does not enforce). A caller wanting transactional
    rollback-on-new-syntax-error passes an explicit ``decide`` (e.g.
    ``lambda diags, *_: not diags``)."""
    return True


def apply_edit_transaction(
    repo_root: str,
    graph_db: str,
    candidate: "Mapping[str, str | bytes | None]",
    *,
    executor: Executor | None = None,
    gt_index_bin: str | None = None,
    decide: "Callable[[list[DiagnosticDelta], PatchDeltaResult, EditOverlay], bool] | None" = None,
    reindex_timeout: int = _DEFAULT_REINDEX_TIMEOUT,
    diag_timeout: int = _DEFAULT_DIAG_TIMEOUT,
) -> OverlayResult:
    """Run the full transaction pipeline and commit (default) or roll back per
    ``decide``. Behind ``GT_EDIT_OVERLAY`` (unset/0 => a no-op ``disabled`` result that
    touches NOTHING — no snapshot, no write, no reindex). Never raises."""
    if not _flag_on():
        return OverlayResult(
            committed=False,
            rolled_back=False,
            pre_revision="",
            post_revision="",
            edited_files=tuple(sorted(_norm(k) for k in (candidate or {}))),
            diagnostics_delta=(),
            contract_delta=PatchDeltaResult(),
            capability_states={"overlay": "disabled"},
            reason="flag_off",
            disabled=True,
        )
    overlay = EditOverlay(
        repo_root,
        graph_db,
        executor=executor,
        gt_index_bin=gt_index_bin,
        reindex_timeout=reindex_timeout,
        diag_timeout=diag_timeout,
    )
    overlay.snapshot(candidate)
    overlay.apply()
    overlay.reindex()
    diagnostics = overlay.diagnostics_delta()
    contract = overlay.contract_delta()
    keep = (decide or _default_decide)(diagnostics, contract, overlay)
    if keep:
        overlay.commit()
    else:
        overlay.rollback()
    return overlay.result(diagnostics, contract)
