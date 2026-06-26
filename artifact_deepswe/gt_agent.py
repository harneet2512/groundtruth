"""GT-augmented mini-swe-agent for Pier -- full 3-phase GroundTruth integration.

Extends Pier's MiniSweAgent to inject GroundTruth codebase intelligence into the
container at three points:

  Phase 1 -- graph.db at install time:
    Injects gt_hook.py + gt_mini_patch.py into the container.  If Go is available,
    builds gt-index from source and indexes the repo into /tmp/graph.db.  Falls
    back gracefully: gt_hook.py's AST-based self-index works without graph.db.

  Phase 2 -- L1 brief in instruction:
    If GT_GRAPH_DB + GT_REPO_ROOT env vars point to a pre-indexed graph.db on the
    HOST, generates a v1r brief and prepends it to the instruction in a
    <gt-task-brief> block.

  Phase 3 -- observation interception (post-edit / post-view):
    gt_mini_patch.py is loaded at mini-swe-agent interpreter startup via a .pth
    file in site-packages.  It monkey-patches the environment's execute() to
    classify commands as edit/view and append <gt-evidence> from gt_hook.py.

SUBSTRATE-CONSUME / PROOF mode (GT_PROOF_MODE=1, the leaderboard `deepswe_full.yml`
run) is FAIL-CLOSED, NO-FALLBACK on every path: the pinned substrate's
/gt_artifacts/graph.db is THE ONLY graph (no second in-container build), the §H
consumption witness RAISES (does not warn) on a hook != post-LSP hash mismatch, and
the brief is CONSUMED read-only from $GT_CERT_DIR/brief.txt — never regenerated host-
side. A missing/divergent substrate artifact HARD-STOPS the run (DeepSweAdapterError /
DEEPSWE_ADAPTER_FAIL), it never silently degrades. Outside proof mode the legacy
preindex/trial path stays best-effort / correct-or-quiet.

Control arm: set GT_BASELINE=1 to disable all GT injection.

Usage with pier:
    pier run -p deep-swe/tasks/<task> \\
        --agent-import-path artifact_deepswe.gt_agent:GTMiniSweAgent \\
        --model deepseek/deepseek-v4-flash \\
        --env docker -y

    # With custom config:
    pier run -p deep-swe/tasks/<task> \\
        --agent-import-path artifact_deepswe.gt_agent:GTMiniSweAgent \\
        --model deepseek/deepseek-v4-flash \\
        --env docker -y \\
        --ak config_file=artifact_deepswe/gt_integration/deepswe_gt_pier.yaml

    # With pre-indexed graph.db (host-side brief):
    GT_GRAPH_DB=/path/to/graph.db GT_REPO_ROOT=/path/to/repo \\
    pier run ...

    # Baseline (control arm, no GT):
    GT_BASELINE=1 pier run ...
"""
from __future__ import annotations

import base64
import logging
import os
import re
import textwrap
from pathlib import Path
try:
    from pier.agents.installed.mini_swe_agent import MiniSweAgent
    from pier.environments.base import BaseEnvironment
    from pier.models.agent.context import AgentContext
    from pier.models.agent.install import AgentInstallSpec, InstallStep
    from pier.models.trial.paths import EnvironmentPaths
except ImportError:
    MiniSweAgent = None  # type: ignore[assignment,misc]
    BaseEnvironment = None  # type: ignore[assignment,misc]
    AgentContext = None  # type: ignore[assignment,misc]
    AgentInstallSpec = None  # type: ignore[assignment,misc]
    InstallStep = None  # type: ignore[assignment,misc]
    EnvironmentPaths = None  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)

# Strict flag parse (bug #6): bool(env) made GT_BASELINE=0 enable the baseline
# arm. Strict == "1" like every other GT flag (GT_PROOF_MODE, GT_PORTABLE_SUBSTRATE).
_GT_BASELINE = os.environ.get("GT_BASELINE") == "1"
_THIS_DIR = Path(__file__).resolve().parent


def _proof_mode() -> bool:
    """True when the run is the substrate-consume PROOF (GHA `deepswe_full.yml`):
    GT_PROOF_MODE=1. In proof mode every GT path is consume-or-fail-closed — no
    in-container rebuild, no host-side brief, no warn-and-continue."""
    return os.environ.get("GT_PROOF_MODE") == "1"


def _substrate_active() -> bool:
    """True when the pinned portable substrate already produced the authoritative
    graph + certs and the harness handed them to the adapter READ-ONLY via the
    canonical handoff (GT_HOST_GRAPH_DB / GT_CERT_DIR; proof.py HOST_HANDOFF). In
    this mode the substrate graph is THE ONLY graph — the adapter never builds a
    second one and never falls back to one. Detected purely from the handoff env so
    it is harness-agnostic and mirrors gt_mini_patch._substrate_active exactly."""
    return bool(
        os.environ.get("GT_PORTABLE_SUBSTRATE") == "1"
        or os.environ.get("GT_HOST_GRAPH_DB")
        or os.environ.get("GT_CERT_DIR")
    )


class DeepSweAdapterError(RuntimeError):
    """Fail-closed adapter error (§E DEEPSWE_ADAPTER_FAIL). Raised — never warned —
    when a substrate-consume invariant is violated under proof/substrate mode (a
    second graph would be built, the consumed graph diverges from the substrate's
    LSP-resolved graph, or the substrate brief is absent). A divergent or missing
    substrate artifact must HARD-STOP the run, not silently degrade."""


def _adapter_fail(detail: str, message: str, cause: BaseException | None = None):
    """Print the classified ``[GT_META] ... error=DEEPSWE_ADAPTER_FAIL`` line and
    THEN raise DeepSweAdapterError (bug #7, P0-support). pier can swallow the
    exception, so the printed line is what the workflow's grep and the outcome
    classifier actually see — a bare raise was invisible. Every adapter raise
    site routes through here (the witness's ``_fail`` prints its own richer line)."""
    print(
        f"[GT_META] gt_artifacts={os.environ.get('GT_CERT_DIR', '')}; "
        f"error=DEEPSWE_ADAPTER_FAIL detail={detail}",
        flush=True,
    )
    if cause is not None:
        raise DeepSweAdapterError(message) from cause
    raise DeepSweAdapterError(message)


def _assert_substrate_handoff() -> None:
    """D1 — EARLY fail-closed STARTUP guard (Priority C, 2026-06-13).

    In proof/benchmark mode the run MUST consume the pinned substrate's
    authoritative graph + certs + brief, handed off READ-ONLY. If
    ``GT_PROOF_MODE=1`` but the handoff is absent (``_substrate_active()`` false,
    or the required handoff env vars unset), the run would otherwise drift toward
    a non-handoff graph and only fail LATE on a downstream ``GT_ARTIFACT_MISSING``.
    Assert it up front — before any witness/brief/agent work — with a single
    named error, so a missing handoff can never silently reach the agent and the
    integration can never re-index/re-brief in proof mode.

    Outside proof mode this is a no-op (legacy dev/CI may host-build). It does NOT
    replace the downstream artifact/hash checks (witness graph-hash,
    ``_substrate_brief`` existence/non-empty) — it front-loads the env contract."""
    if not _proof_mode():
        return
    if not _substrate_active():
        _adapter_fail(
            "PROOF_WITHOUT_SUBSTRATE_HANDOFF",
            "DEEPSWE_ADAPTER_FAIL: GT_PROOF_MODE=1 but the substrate handoff is "
            "absent (none of GT_PORTABLE_SUBSTRATE / GT_HOST_GRAPH_DB / GT_CERT_DIR "
            "is set). A proof/benchmark run must CONSUME the pinned substrate's "
            "authoritative graph + certs + brief READ-ONLY; refusing to run without "
            "the handoff (no in-container rebuild, no host-side brief).",
        )
    missing = [v for v in ("GT_HOST_GRAPH_DB", "GT_CERT_DIR") if not os.environ.get(v)]
    if missing:
        _adapter_fail(
            "PROOF_HANDOFF_ENV_MISSING",
            "DEEPSWE_ADAPTER_FAIL: GT_PROOF_MODE=1 but required substrate-handoff "
            f"env var(s) {missing} are unset. The harness must pass the authoritative "
            "graph (GT_HOST_GRAPH_DB) and cert dir (GT_CERT_DIR) — refusing to run.",
        )

# ---------------------------------------------------------------------------
# Locate the two payloads we inject into the container
# ---------------------------------------------------------------------------
_GT_HOOK_CANDIDATES = [
    _THIS_DIR / "benchmarks" / "swebench" / "gt_hook.py",
    _THIS_DIR.parent / "benchmarks" / "swebench" / "gt_hook.py",
    _THIS_DIR / "gt_hook.py",
]
_PATCH_PATH = _THIS_DIR / "gt_mini_patch.py"
_PHASE_POLICY_PATH = _THIS_DIR / "phase_policy.py"
_PRODUCT_SRC_DIR = _THIS_DIR.parent / "src" / "groundtruth"
_PRODUCT_RUNTIME_DIR = _PRODUCT_SRC_DIR / "runtime"
# Oracle siblings (LIPI 2026-06-10): gt_mini_patch._load_gt_oracle() loads
# gt_oracle.py (which requires gt_oracle_sense.py) from ITS OWN directory to
# produce the live SPEC/obligation candidates. Without shipping both siblings
# to /opt/gt the live obligation fire stays dead in-container (the exact gap
# this wiring closes). Best-effort: absent files -> correct-or-quiet skip.
_ORACLE_PATH = _THIS_DIR / "gt_oracle.py"
_SENSE_PATH = _THIS_DIR / "gt_oracle_sense.py"


def _load(path_candidates: list[Path]) -> str | None:
    """Load the first file found from a list of candidate paths."""
    for p in path_candidates:
        if p.is_file():
            logger.info("GT: loaded %s (%d bytes)", p, p.stat().st_size)
            return p.read_text(encoding="utf-8", errors="replace")
    logger.warning("GT: payload not found: %s", [str(p) for p in path_candidates])
    return None


_GT_HOOK_CONTENT = _load(_GT_HOOK_CANDIDATES)
_PATCH_CONTENT = _load([_PATCH_PATH])
_PHASE_POLICY_CONTENT = _load([_PHASE_POLICY_PATH])
_ORACLE_CONTENT = _load([_ORACLE_PATH])
_SENSE_CONTENT = _load([_SENSE_PATH])
# ---------------------------------------------------------------------------
# PRODUCT-PACKAGE INJECTION ALLOW-LIST (F2+F5, 2026-06-13) — the SINGLE source of
# truth for which `groundtruth.*` modules are shipped into the task container so
# gt_mini_patch.py imports the REAL Product policy in-container (NOT a divergent
# inline fallback copy). Every module-scope `from groundtruth.<pkg>.<mod> import`
# in gt_mini_patch.py MUST be covered by this map, or the import fails in-container
# and the inline fallback (which historically drifted from Product — e.g. omitted
# `/static//assets/` in the fact-filter) runs instead. The drift is what kept
# Product != agent-time. `_assert_gt_mini_patch_imports_covered()` (and the pytest
# import-coverage test) fail-closed if a future import is added without shipping it.
#
# Maps package-relative subdir -> the module filenames to inject under
# /opt/gt/groundtruth/<subdir>/. The injection creates each subdir's __init__.py
# (empty, like runtime) so the submodules import directly; the modules below import
# only stdlib so no transitive groundtruth.* import is needed. `runtime` historically
# carried CP011-015 control-plane modules; `delivery`+`pretask` carry the B1 single-
# source fact-filter policy (path_policy/name_policy/curation_map).
_PRODUCT_PACKAGE_MODULES: dict[str, tuple[str, ...]] = {
    "runtime": (
        "context_policy.py",
        "trajectory_state.py",
        "context_budget.py",
        "action_translation.py",
        "verification_horizon.py",
        "obligations.py",
        "ledger.py",
        "patterns.py",
        "edit_risk.py",
    ),
    # B1 delivery fact-filter — the single source for path-class + name-class
    # exclusion. Shipping these makes gt_mini_patch.py's `from groundtruth.delivery
    # .{path_policy,name_policy} import` succeed in-container so the inline
    # `delivery_policy_import_fallback` copy never runs (it omitted /static//assets/).
    "delivery": (
        "path_policy.py",
        "name_policy.py",
    ),
    # FACT gate + cross-language disqualifier — gt_mini_patch.py imports
    # `from groundtruth.pretask.curation_map import (...)`. curation_map imports
    # only stdlib (os/re/sqlite3/dataclasses) so it ships standalone.
    "pretask": (
        "curation_map.py",
    ),
}

# Per-package {filename: content}. _load() emits a warning + leaves the value None
# if a source file is absent (correct-or-quiet: a missing module is skipped at
# injection time, exactly as before for runtime).
_PRODUCT_PACKAGE_FILES: dict[str, dict[str, str | None]] = {
    subdir: {name: _load([_PRODUCT_SRC_DIR / subdir / name]) for name in names}
    for subdir, names in _PRODUCT_PACKAGE_MODULES.items()
}
# Back-compat alias (runtime-only view; some callers/tests reference this name).
_PRODUCT_RUNTIME_FILES = _PRODUCT_PACKAGE_FILES["runtime"]

# Flat set of the dotted module names this adapter SHIPS into the container
# (groundtruth.<subdir>.<module>) — the canonical allow-list the import-coverage
# guard checks gt_mini_patch.py's module-scope imports against.
_INJECTED_GT_MODULES: frozenset[str] = frozenset(
    f"groundtruth.{subdir}.{name[:-3]}"
    for subdir, names in _PRODUCT_PACKAGE_MODULES.items()
    for name in names
)


# Module-scope `from groundtruth... import` statements at the top level of a file
# (indent 0). Submodule imports inside a try/except are still indented under the
# try, so we match `from groundtruth.<a>.<b> import` regardless of leading
# whitespace but only `from ... import` (not `import groundtruth` bare).
_GT_IMPORT_RE = re.compile(
    r"^\s*from\s+(groundtruth(?:\.[A-Za-z_][\w]*)+)\s+import\b", re.MULTILINE
)


def gt_mini_patch_required_modules(patch_source: str | None = None) -> set[str]:
    """Every `groundtruth.*` module gt_mini_patch.py imports at module scope.

    Returns the set of dotted module names (``groundtruth.delivery.path_policy``,
    ``groundtruth.pretask.curation_map``, ...) that MUST be injected for the
    in-container import to resolve. Used by ``_assert_gt_mini_patch_imports_covered``
    and the pytest import-coverage test so a future import can't silently
    re-introduce the inline-fallback drift."""
    src = patch_source
    if src is None:
        src = _PATCH_CONTENT if "_PATCH_CONTENT" in globals() else None
    if src is None:
        try:
            src = _PATCH_PATH.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return set()
    return {m.group(1) for m in _GT_IMPORT_RE.finditer(src)}


def _assert_gt_mini_patch_imports_covered(patch_source: str | None = None) -> None:
    """Fail-closed coverage guard (F2+F5): every module-scope `from groundtruth.*
    import` in gt_mini_patch.py must be in the injection allow-list
    (``_INJECTED_GT_MODULES``). If one is NOT shipped, the in-container import
    fails and the DIVERGENT inline fallback runs (the root cause this fix closes) —
    so we raise at import time rather than silently drift. Generalized: derived from
    the actual import statements + the actual allow-list, no hardcoded module list."""
    required = gt_mini_patch_required_modules(patch_source)
    uncovered = sorted(required - set(_INJECTED_GT_MODULES))
    if uncovered:
        raise RuntimeError(
            "gt_mini_patch.py imports groundtruth modules NOT covered by the "
            f"injection allow-list: {uncovered}. Add them to "
            "_PRODUCT_PACKAGE_MODULES in gt_agent.py (and confirm the source files "
            "import only stdlib / shipped modules), or the in-container import will "
            "fail and the divergent inline fallback will run (Product != agent-time)."
        )

# ---------------------------------------------------------------------------
# Base64 encoding for heredoc injection (gt_hook.py is ~115KB+)
# ---------------------------------------------------------------------------
# Docker caps a single RUN line at 65535 bytes, so we chunk the base64 into
# pieces that each fit in one echo command.
_B64_CHUNK_SIZE = 45_000


def _b64_chunks(content: str | None) -> list[str]:
    if not content:
        return []
    enc = base64.b64encode(content.encode("utf-8")).decode("ascii")
    return [enc[i : i + _B64_CHUNK_SIZE] for i in range(0, len(enc), _B64_CHUNK_SIZE)]


def _b64_chunks_bytes(data: bytes | None) -> list[str]:
    """Binary-safe sibling of _b64_chunks — for injecting graph.db (a SQLite
    blob, not utf-8 text) into the container via chunked echo|base64 -d."""
    if not data:
        return []
    enc = base64.b64encode(data).decode("ascii")
    return [enc[i : i + _B64_CHUNK_SIZE] for i in range(0, len(enc), _B64_CHUNK_SIZE)]


# GT files go to /opt/gt -- a persistent, non-volume location.
_GT_DIR = "/opt/gt"

# ---------------------------------------------------------------------------
# Shell snippets for install steps
# ---------------------------------------------------------------------------

# Repo-root detection, written to /opt/gt/gt_root.txt.
_ROOT_DETECT = (
    f"mkdir -p {_GT_DIR}; chmod 755 {_GT_DIR}; "
    'REPO_ROOT=""; '
    'for d in /home/user /testbed /workspace /app /repo; do '
    '[ -d "$d/.git" ] && REPO_ROOT="$d" && break; done; '
    '[ -z "$REPO_ROOT" ] && REPO_ROOT=$(find / -maxdepth 3 -name .git -type d '
    '2>/dev/null | head -1 | sed "s|/.git||"); '
    '[ -z "$REPO_ROOT" ] && REPO_ROOT="/home/user"; '
    f'echo "$REPO_ROOT" > {_GT_DIR}/gt_root.txt; '
    'echo "GT: repo root=$REPO_ROOT" >&2 || true'
)

# Phase 1: build gt-index from Go source and run indexing.
# Best-effort: if Go is not available or build fails, skip gracefully.
# The gt-index Go source is NOT copied into the container (too large).
# Instead, we try to download a pre-built binary from GitHub releases.
_GT_INDEX_URL = os.environ.get(
    "GT_INDEX_URL",
    "https://github.com/harneet2512/groundtruth/releases/latest/download"
    "/gt-index-linux-amd64",
)
_BUILD_GRAPH_DB = (
    "set +e; "
    f'REPO_ROOT=$(cat {_GT_DIR}/gt_root.txt 2>/dev/null || echo "/home/user"); '
    # Try 1: download pre-built binary from GitHub releases
    f'if command -v curl >/dev/null 2>&1; then '
    f'  curl -fsSL --connect-timeout 10 --max-time 60 -o /tmp/gt-index "{_GT_INDEX_URL}" 2>/dev/null '
    f'  && chmod +x /tmp/gt-index '
    f'  && echo "GT: downloaded gt-index binary" >&2; '
    f'fi; '
    # Try 2: build from source if Go is available and source is present
    'if [ ! -x /tmp/gt-index ] && command -v go >/dev/null 2>&1; then '
    '  for d in /opt/gt/gt-index /tmp/gt-index-src; do '
    '    if [ -f "$d/cmd/gt-index/main.go" ]; then '
    # -tags sqlite_fts5 is MANDATORY (CLAUDE.md): without it the nodes_fts vtable is
    # silently compiled out. Low runtime impact here (the in-container localizer is not
    # used; the brief runs host-side) but correctness + future-proofing demand it.
    '      cd "$d" && CGO_ENABLED=1 go build -tags sqlite_fts5 -ldflags="-s -w" -o /tmp/gt-index '
    '        ./cmd/gt-index/ 2>/dev/null '
    '      && echo "GT: built gt-index from source at $d" >&2 && break; '
    '    fi; '
    '  done; '
    'fi; '
    # Run indexing if binary is available
    'if [ -x /tmp/gt-index ]; then '
    '  /tmp/gt-index -root="$REPO_ROOT" -output=/tmp/graph.db 2>&1 '
    '  && echo "GT: graph.db built at /tmp/graph.db" >&2 '
    '  || echo "GT: gt-index failed (non-fatal)" >&2; '
    'else '
    '  echo "GT: gt-index not available, using AST-only mode" >&2; '
    'fi; '
    "true"  # ensure exit 0
)

# Tiny bootstrap snippet appended to mini-swe-agent's default.py so the
# patch loads whenever the module imports.  Base64'd to dodge shell quoting.
_BOOTSTRAP_SNIPPET = (
    "\ntry:\n"
    f'    import sys as _gts; _gts.path.insert(0, "{_GT_DIR}"); import gt_mini_patch  # GroundTruth\n'
    "except Exception:\n"
    "    pass\n"
)
_BOOTSTRAP_B64 = base64.b64encode(_BOOTSTRAP_SNIPPET.encode("utf-8")).decode("ascii")

# Primary load mechanism: a .pth file in site-packages.  site.py executes any
# .pth line beginning with `import` at interpreter startup -- before user code
# and immune to .pyc caching.
_PTH_LINE = f'import sys; sys.path.insert(0, "{_GT_DIR}"); import gt_mini_patch\n'
_PTH_B64 = base64.b64encode(_PTH_LINE.encode("utf-8")).decode("ascii")

# Locate mini-swe-agent's installed default.py and append the bootstrap.
# Also write a .pth file as the primary mechanism.
_APPEND_TO_MINI = (
    "set +e; "
    'export PATH="/root/.local/bin:$HOME/.local/bin:$PATH"; '
    '. "$HOME/.local/bin/env" 2>/dev/null; . /root/.local/bin/env 2>/dev/null; '
    'BIN="$(command -v mini-swe-agent '
    "|| command -v mini "
    "|| ls /root/.local/bin/mini-swe-agent "
    '/home/*/.local/bin/mini-swe-agent 2>/dev/null | head -1)"; '
    'if [ -z "$BIN" ]; then '
    '  echo "GT: mini bin not found; patch-load skipped" >&2; exit 0; fi; '
    'MPY="$(head -n1 "$BIN" | sed "s/^#!//")"; '
    # tail -1: importing minisweagent prints a banner; without tail we
    # capture the banner instead of the path.
    'SP="$("$MPY" -c '
    '"import minisweagent,os;print(os.path.dirname(os.path.dirname('
    'minisweagent.__file__)))" 2>/dev/null | tail -1)"; '
    'DEF="$("$MPY" -c '
    '"import minisweagent.agents.default as m;print(m.__file__)" '
    '2>/dev/null | tail -1)"; '
    # (1) PRIMARY: .pth in site-packages
    f'if [ -n "$SP" ]; then echo "{_PTH_B64}" | base64 -d '
    f'  > "$SP/zz_gt_bootstrap.pth" '
    f'  && echo "GT: wrote .pth to $SP" >&2; fi; '
    # (2) BACKUP: append to default.py AND purge stale .pyc
    'if [ -n "$DEF" ]; then '
    f'  echo "{_BOOTSTRAP_B64}" | base64 -d >> "$DEF"; '
    '  find "$(dirname "$DEF")/.." -name "*.pyc" -delete 2>/dev/null; '
    '  echo "GT: appended+pyc-purged $DEF" >&2; fi'
)

# Build-time self-test: verify the patch loaded correctly.
_SELFTEST_PY = (
    "import os, sys\n"
    "try:\n"
    "    import minisweagent.environments.local as L\n"
    "    ok = bool(getattr(L.LocalEnvironment, '_gt_patched', False))\n"
    "except Exception as e:\n"
    "    print('GT_SELFTEST import_error=%r' % (e,)); sys.exit(7)\n"
    "print('GT_SELFTEST patched=%s gt_mini=%s hook=%s root=%s db=%s' % ("
    f"ok, os.path.exists('{_GT_DIR}/gt_mini_patch.py'), "
    f"os.path.exists('{_GT_DIR}/gt_hook.py'), "
    f"os.path.exists('{_GT_DIR}/gt_root.txt'), "
    # G14: probe the SAME path the per-turn producers read (substrate mode keeps the
    # authoritative graph at GT_HOST_GRAPH_DB=/gt_artifacts/graph.db, NOT /tmp/graph.db),
    # so db=True reflects the real graph instead of lying db=False on a correct run.
    "os.path.exists(os.environ.get('GT_HOST_GRAPH_DB') or '/tmp/graph.db')))\n"
    "sys.exit(0 if ok else 7)\n"
)
_SELFTEST_B64 = base64.b64encode(_SELFTEST_PY.encode("utf-8")).decode("ascii")
_SELFTEST_STEP = (
    "set +e; "
    'export PATH="/root/.local/bin:$HOME/.local/bin:$PATH"; '
    '. "$HOME/.local/bin/env" 2>/dev/null; . /root/.local/bin/env 2>/dev/null; '
    'BIN="$(command -v mini-swe-agent '
    "|| ls /root/.local/bin/mini-swe-agent "
    '/home/*/.local/bin/mini-swe-agent 2>/dev/null | head -1)"; '
    'if [ -z "$BIN" ]; then '
    '  echo "GT_SELFTEST mini bin not found" >&2; exit 1; fi; '
    'MPY="$(head -n1 "$BIN" | sed "s/^#!//")"; '
    f'echo "{_SELFTEST_B64}" | base64 -d > {_GT_DIR}/selftest.py; '
    f'"$MPY" {_GT_DIR}/selftest.py 1>&2; RC=$?; '
    'if [ "$RC" -ne 0 ]; then '
    '  echo "GT_SELFTEST_FAILED rc=$RC" >&2; exit 1; fi; '
    'echo "GT_SELFTEST_OK" >&2'
)

# ---------------------------------------------------------------------------
# GT preamble injected into the agent's instruction
# ---------------------------------------------------------------------------
_GT_PREAMBLE = textwrap.dedent("""\

    ## GroundTruth codebase intelligence (automatic)

    As you read and edit files, GroundTruth automatically appends evidence to the
    command output inside <gt-evidence> tags: who calls a function and how, the
    tests that cover it, behavioral contracts (signature/return), and sibling
    patterns you must match. Read those tags -- they are cross-file facts you
    cannot get from the file alone. They appear on their own; you do not call
    anything. When GT shows callers, do not break them; when it shows a contract,
    preserve it; when it names a test, run it to verify.
""")

# Fallback preamble for when observation interception is not available
# (baseline mode or patch injection failed)
_GT_MANUAL_PREAMBLE = textwrap.dedent("""\

    ## Codebase Intelligence Tool

    You have a codebase intelligence tool at /opt/gt/gt_hook.py that provides \
    cross-file analysis. Before editing a file, run:

        python3 /opt/gt/gt_hook.py understand <filepath> \\
            --root=$(cat /opt/gt/gt_root.txt) --quiet --max-lines=10

    This shows you information you CANNOT get by reading the file alone:
    - Which OTHER files call functions in this file and how they use the results
    - Which TEST files cover this module (so you know where to verify)
    - Rules that hold across ALL sibling methods (patterns you must follow)
    - Behavioral contracts (what functions read/write/return)

    Use the understand command on 1-2 key files before editing. Don't over-use it.
""")


def _inject_steps_mount_mode() -> list[InstallStep]:
    """Fast-path inject when GT_MOUNT_MODE=1 (GHA host-mount optimisation).

    The workflow pre-stages all GT files under /tmp/gt_inject/opt/gt on the
    host and bind-mounts that dir into the container as /opt/gt:ro.  All we
    need to do inside the container is:
      1. Wire the already-present gt_mini_patch.py into mini-swe-agent's
         import chain (writes into site-packages and default.py — container-
         internal paths that the :ro mount cannot reach).
      2. Run the self-test to verify the patch loaded.

    This replaces ~30 base64-echo RUN layers (12+ min) with 2 fast steps
    (~10 s total).  Backward-compat: GT_MOUNT_MODE unset falls back to the
    full base64-injection path via _inject_steps_b64().
    """
    # Import-coverage guard still runs — the allow-list must cover gt_mini_patch
    # even though we skip the injection (the files arrive via the host mount, but
    # we still need to verify they'll import correctly).
    _assert_gt_mini_patch_imports_covered()

    steps: list[InstallStep] = [
        # Ensure /opt/gt exists and is accessible.  In mount-mode the directory
        # is created by the workflow before pier starts, but the mkdir is
        # idempotent and costs nothing.
        InstallStep(
            user="root",
            run=(
                f"mkdir -p {_GT_DIR} && chmod 755 {_GT_DIR} && "
                f'echo "GT mount-mode: files pre-staged by host at {_GT_DIR}" >&2'
            ),
        ),
        # Wire the import chain (writes to container-internal site-packages).
        InstallStep(user="root", run=_APPEND_TO_MINI),
        # Verify the patch loaded correctly.
        InstallStep(user="root", run=_SELFTEST_STEP),
    ]
    return steps


def _inject_steps_b64() -> list[InstallStep]:
    """Full base64-injection path (legacy / non-mount-mode runners).

    Each InstallStep maps to one Dockerfile RUN line.  Docker caps a line at
    65535 bytes, so the ~115KB gt_hook.py is chunked into multiple echo lines.
    """
    hook_chunks = _b64_chunks(_GT_HOOK_CONTENT)
    if not hook_chunks:
        return [
            InstallStep(
                user="root",
                run='echo "GT WARNING: gt_hook.py missing -- GT skipped" >&2 || true',
            )
        ]

    steps: list[InstallStep] = [
        InstallStep(user="root", run=f"mkdir -p {_GT_DIR} && chmod 755 {_GT_DIR}")
    ]

    # --- gt_hook.py: the container-native evidence engine ---
    for i, chunk in enumerate(hook_chunks):
        op = ">" if i == 0 else ">>"
        steps.append(
            InstallStep(user="root", run=f'echo "{chunk}" {op} {_GT_DIR}/gt_hook.b64')
        )
    steps.append(
        InstallStep(
            user="root",
            run=(
                f"base64 -d {_GT_DIR}/gt_hook.b64 > {_GT_DIR}/gt_hook.py "
                f"&& chmod 755 {_GT_DIR}/gt_hook.py "
                f"&& rm -f {_GT_DIR}/gt_hook.b64"
            ),
        )
    )

    # --- gt_mini_patch.py: the observation-interception patch ---
    patch_chunks = _b64_chunks(_PATCH_CONTENT)
    if patch_chunks:
        for i, chunk in enumerate(patch_chunks):
            op = ">" if i == 0 else ">>"
            steps.append(
                InstallStep(
                    user="root", run=f'echo "{chunk}" {op} {_GT_DIR}/gt_patch.b64'
                )
            )
        steps.append(
            InstallStep(
                user="root",
                run=(
                    f"base64 -d {_GT_DIR}/gt_patch.b64 > {_GT_DIR}/gt_mini_patch.py "
                    f"&& chmod 644 {_GT_DIR}/gt_mini_patch.py "
                    f"&& rm -f {_GT_DIR}/gt_patch.b64"
                ),
            )
        )
        # Wire the patch into mini-swe-agent's import chain
        steps.append(InstallStep(user="root", run=_APPEND_TO_MINI))

    # --- gt_oracle.py + gt_oracle_sense.py: the SPEC/obligation producer ---
    # gt_mini_patch._load_gt_oracle() resolves these as same-directory siblings
    # at the review transition; both must land in /opt/gt for the live
    # obligation fire (load_obligations) to exist in-container.
    for fname, content in (("phase_policy.py", _PHASE_POLICY_CONTENT),
                           ("gt_oracle.py", _ORACLE_CONTENT),
                           ("gt_oracle_sense.py", _SENSE_CONTENT)):
        chunks = _b64_chunks(content)
        if not chunks:
            continue
        b64name = fname.replace(".py", ".b64")
        for i, chunk in enumerate(chunks):
            op = ">" if i == 0 else ">>"
            steps.append(
                InstallStep(user="root", run=f'echo "{chunk}" {op} {_GT_DIR}/{b64name}')
            )
        steps.append(
            InstallStep(
                user="root",
                run=(
                    f"base64 -d {_GT_DIR}/{b64name} > {_GT_DIR}/{fname} "
                    f"&& chmod 644 {_GT_DIR}/{fname} "
                    f"&& rm -f {_GT_DIR}/{b64name}"
                ),
            )
        )

    # --- product packages: GT identity lives under groundtruth.{runtime,delivery,
    #     pretask} (F2+F5, 2026-06-13) ---
    # The task container receives ONLY the files injected by this adapter. Ship the
    # product-owned modules gt_mini_patch.py imports at module scope so the REAL
    # Product policy imports in-container and the inline fallback copies NEVER run
    # (the divergent inline fact-filter omitted /static//assets/, so Product !=
    # agent-time until these ship). `runtime` = the CP011-015 control plane;
    # `delivery`+`pretask` = the B1 single-source fact-filter (path_policy /
    # name_policy / curation_map). Each package gets an empty __init__.py (like
    # runtime) so `from groundtruth.<pkg>.<mod> import` resolves the submodule
    # directly. The injected modules import only stdlib — no transitive
    # groundtruth.* import to satisfy.
    #
    # FAIL-CLOSED build guard: every `from groundtruth.* import` in gt_mini_patch.py
    # MUST be in this allow-list or the in-container import fails and the inline
    # fallback runs. Assert before emitting steps so the drift can't silently ship.
    _assert_gt_mini_patch_imports_covered()
    mkdir_inits = (
        f"touch {_GT_DIR}/groundtruth/__init__.py"
    )
    for subdir in _PRODUCT_PACKAGE_FILES:
        mkdir_inits = (
            f"mkdir -p {_GT_DIR}/groundtruth/{subdir} && "
            + mkdir_inits
            + f" && touch {_GT_DIR}/groundtruth/{subdir}/__init__.py"
        )
    steps.append(InstallStep(user="root", run=mkdir_inits))
    for subdir, files in _PRODUCT_PACKAGE_FILES.items():
        for fname, content in files.items():
            chunks = _b64_chunks(content)
            if not chunks:
                continue
            # Prefix the temp b64 name with the package so two packages can carry
            # the same filename without clobbering each other.
            b64name = f"{subdir}__{fname.replace('.py', '.b64')}"
            for i, chunk in enumerate(chunks):
                op = ">" if i == 0 else ">>"
                steps.append(
                    InstallStep(user="root",
                                run=f'echo "{chunk}" {op} {_GT_DIR}/{b64name}')
                )
            steps.append(
                InstallStep(
                    user="root",
                    run=(
                        f"base64 -d {_GT_DIR}/{b64name} > "
                        f"{_GT_DIR}/groundtruth/{subdir}/{fname} "
                        f"&& chmod 644 {_GT_DIR}/groundtruth/{subdir}/{fname} "
                        f"&& rm -f {_GT_DIR}/{b64name}"
                    ),
                )
            )

    # --- Repo root detection ---
    steps.append(InstallStep(user="root", run=_ROOT_DETECT))

    # --- Phase 1: graph.db ---
    # SUBSTRATE-CONSUME (hole #1, FAIL-CLOSED, NO DUAL GRAPH): when the pinned
    # substrate already produced the authoritative graph (GT_PORTABLE_SUBSTRATE /
    # GT_HOST_GRAPH_DB / GT_CERT_DIR set), the substrate's /gt_artifacts/graph.db is
    # THE ONLY graph. Building a SECOND in-container /tmp/graph.db would (a) diverge
    # from the substrate's LSP-resolved + gate-certified graph and (b) break the
    # hook==post-LSP-hash witness. So the build step is REMOVED ENTIRELY in substrate
    # mode — never "fall back" to it. The consume path is verified by the witness
    # (which fails closed if the consumed graph is absent or diverges). Outside
    # substrate mode (the legacy preindex/trial path) the in-container build remains.
    if _substrate_active():
        steps.append(
            InstallStep(
                user="root",
                run=(
                    'echo "GT: substrate-consume mode (GT_HOST_GRAPH_DB/GT_CERT_DIR '
                    'set) — the substrate graph is authoritative; NOT building a '
                    'second in-container graph.db (no dual graph, no fallback)" >&2 '
                    "|| true"
                ),
            )
        )
    else:
        # NON-SUBSTRATE (local / trial) path. The in-container _BUILD_GRAPH_DB cannot
        # acquire gt-index (the release download 404s on a repo with no asset; no Go in
        # a task image to build from source), so it produces NO /tmp/graph.db and EVERY
        # per-turn producer falls silent (the 0-evidence regression). When the host
        # already built a graph (GT_GRAPH_DB -> a readable file), inject THAT graph
        # directly into the container at /tmp/graph.db — byte-identical to the graph the
        # host brief used, so brief and per-turn evidence agree by construction. Falls
        # back to the (best-effort) build only when no host graph is available.
        _host_db = os.environ.get("GT_GRAPH_DB", "")
        _gdb_chunks: list[str] = []
        if _host_db and os.path.isfile(_host_db):
            try:
                # gzip BEFORE base64: a raw 4.2MB graph.db base64s to 5.6MB and bakes
                # a 33.6MB Dockerfile, over BuildKit's 16MB gRPC cap (the build dies
                # with ResourceExhausted). SQLite compresses ~4x, so gzip -> ~1.4MB
                # base64 -> 33 chunks, comfortably under the cap.
                import gzip as _gzip
                _gdb_chunks = _b64_chunks_bytes(
                    _gzip.compress(open(_host_db, "rb").read(), 9))
            except OSError:
                _gdb_chunks = []
        if _gdb_chunks:
            # Size ceiling (fail loud, not a cryptic build death): the gzipped+b64 graph
            # bakes into Dockerfile RUN layers; the whole build definition must stay under
            # BuildKit's 16MB gRPC cap. Empirically the build def is ~1.5x the b64 payload
            # (gzipped), so a b64 over ~12MB blows the cap and `docker build` dies with an
            # opaque ResourceExhausted — and since this is the ONLY source of /tmp/graph.db
            # on the inject path, a large repo SILENTLY loses all per-turn evidence
            # ("works on any repo size" violated). Reject at spec time with the diagnostic
            # + the durable fix (mount the graph; no size limit, no bake).
            _b64_total = sum(len(_c) for _c in _gdb_chunks)
            if _b64_total > 12_000_000:
                raise RuntimeError(
                    "GT_GRAPH_DB_TOO_LARGE_TO_BAKE: gzipped graph.db base64 is "
                    f"{_b64_total / 1048576:.1f}MB (host db "
                    f"{os.path.getsize(_host_db) / 1048576:.1f}MB) — over the ~12MB bake "
                    "ceiling (BuildKit's 16MB gRPC cap). MOUNT the graph instead of baking "
                    f"it: pier run --mounts-json '[{{\"type\":\"bind\",\"source\":\"{_host_db}\""
                    ",\"target\":\"/tmp/graph.db\",\"read_only\":true}]' (no size limit). The "
                    "trial/codespace launchers already mount /gt_out — add the graph mount "
                    "there for large repos."
                )
            _b64name = f"{_GT_DIR}/graph_db.b64"
            for _i, _chunk in enumerate(_gdb_chunks):
                _op = ">" if _i == 0 else ">>"
                steps.append(InstallStep(user="root",
                                         run=f'echo "{_chunk}" {_op} {_b64name}'))
            # Fail-closed on the ARTIFACT: decode to a temp, assert non-empty, then
            # atomically mv into place. A corrupt/partial chunk (or gunzip error) leaves
            # NO /tmp/graph.db at all — never a 0-byte/torn file that _db_path()'s
            # os.path.isfile() would return as if it were a real db. The `||` still keeps
            # the build green (the agent degrades to no-graph = correct-or-quiet), but the
            # torn-db window is closed by construction (atomic mv), not by discipline.
            steps.append(InstallStep(user="root", run=(
                f"base64 -d {_b64name} | gunzip > /tmp/graph.db.tmp "
                f"&& [ -s /tmp/graph.db.tmp ] "
                f"&& mv /tmp/graph.db.tmp /tmp/graph.db && chmod 644 /tmp/graph.db && rm -f {_b64name} "
                f'&& echo "GT: injected host graph.db -> /tmp/graph.db '
                f'($(wc -c </tmp/graph.db) bytes)" >&2 '
                f"|| {{ rm -f /tmp/graph.db.tmp /tmp/graph.db {_b64name}; "
                f"echo 'GT: graph.db injection FAILED (decode/gunzip/empty) — agent degrades to no-graph' >&2; }}")))
        else:
            steps.append(InstallStep(user="root", run=_BUILD_GRAPH_DB))

    # --- Self-test: verify patch loaded (fails build with diagnostic if not) ---
    if patch_chunks:
        steps.append(InstallStep(user="root", run=_SELFTEST_STEP))

    return steps


def _inject_steps() -> list[InstallStep]:
    """Dispatcher: return mount-mode steps (fast) or full b64 steps (legacy).

    GT_MOUNT_MODE=1 (set by deepswe_full.yml / swebench_pro_full.yml) means the
    workflow pre-staged all GT files on the host and bind-mounted /opt/gt:ro into
    the container — the 30 base64-echo RUN layers are unnecessary.  Any other
    value (including unset) falls back to the full base64-injection path so older
    runners and local usage continue to work without change.
    """
    if os.environ.get("GT_MOUNT_MODE") == "1":
        return _inject_steps_mount_mode()
    return _inject_steps_b64()


def _cert_dir() -> str:
    """The substrate artifact dir the adapter consumes. GT_CERT_DIR is canonical;
    GT_HOST_GRAPH_DB=/gt_artifacts/graph.db implies the dir even if GT_CERT_DIR was
    not exported separately."""
    cert_dir = os.environ.get("GT_CERT_DIR", "")
    if not cert_dir:
        hg = os.environ.get("GT_HOST_GRAPH_DB", "")
        if hg:
            cert_dir = os.path.dirname(hg)
    return cert_dir


def _substrate_brief() -> str:
    """SUBSTRATE-CONSUME (handoff §A/§D/§G, hole #3): the pinned substrate emitted the
    CURATED brief IN-CONTAINER to ``$GT_CERT_DIR/brief.txt`` (gt_run_proof.py:380-385)
    over the SAME LSP-enriched graph the gates measured. Consume it READ-ONLY — the
    host NEVER re-runs the brief pipeline in this mode.

    FAIL-CLOSED in proof/substrate mode (NO host fallback): if the substrate brief is
    absent/empty while the substrate is active, that is a SUBSTRATE_MISSING_CERTS / GT_
    ARTIFACT_MISSING violation — host-side GT scoring is forbidden (assert_container_
    boundary). We raise DeepSweAdapterError rather than silently falling back to host
    ``generate_v1r_brief``. Outside proof/substrate mode, '' (caller may host-gen)."""
    cert_dir = _cert_dir()
    proof = _proof_mode()
    substrate = _substrate_active()
    if not cert_dir:
        if proof or substrate:
            _adapter_fail(
                "GT_ARTIFACT_MISSING_CERT_DIR",
                "GT_ARTIFACT_MISSING / DEEPSWE_ADAPTER_FAIL: substrate-consume mode is "
                "active (proof_mode=%s substrate=%s) but neither GT_CERT_DIR nor "
                "GT_HOST_GRAPH_DB is set — cannot locate the substrate brief, and host "
                "GT scoring is forbidden in proof mode (no fallback)."
                % (proof, substrate),
            )
        return ""
    brief_path = os.path.join(cert_dir, "brief.txt")
    if not os.path.isfile(brief_path):
        if proof or substrate:
            _adapter_fail(
                "GT_ARTIFACT_MISSING_BRIEF",
                "GT_ARTIFACT_MISSING / DEEPSWE_ADAPTER_FAIL: substrate brief absent at "
                f"{brief_path!r} in substrate-consume mode (proof_mode={proof} "
                f"substrate={substrate}). Failing closed — host-side generate_v1r_brief "
                "is forbidden in proof mode (no divergent host GT scoring).",
            )
        return ""
    try:
        with open(brief_path, encoding="utf-8", errors="replace") as fh:
            txt = (fh.read() or "").strip()
    except OSError as e:
        if proof or substrate:
            _adapter_fail(
                "BRIEF_UNREADABLE",
                f"DEEPSWE_ADAPTER_FAIL: substrate brief unreadable at {brief_path!r} "
                f"in substrate-consume mode: {e} (no fallback).",
                cause=e,
            )
        logger.warning("GT: substrate brief unreadable (%s) -- skipping", e)
        return ""
    if not txt and (proof or substrate):
        _adapter_fail(
            "BRIEF_EMPTY",
            f"DEEPSWE_ADAPTER_FAIL: substrate brief at {brief_path!r} is EMPTY in "
            f"substrate-consume mode (proof_mode={proof} substrate={substrate}). "
            "Failing closed — a paid proof run must not ship a brief-less trajectory "
            "(no host fallback).",
        )
    if txt:
        logger.info("GT: consumed substrate brief %s (%d chars, READ-ONLY)",
                    brief_path, len(txt))
    return txt


def _generate_brief(instruction: str) -> str:
    """Phase 2: the L1 brief.

    SUBSTRATE-CONSUME / PROOF (hole #3): if the pinned substrate is active, the brief
    is consumed READ-ONLY from ``$GT_CERT_DIR/brief.txt`` (``_substrate_brief``) — which
    FAILS CLOSED (raises) if the substrate brief is absent/empty. Host-side generation
    over a divergent graph is FORBIDDEN in proof mode (no fallback).

    LEGACY (non-proof, non-substrate ONLY): host-side generation from a preindexed
    graph.db via GT_GRAPH_DB + GT_REPO_ROOT (the pre-substrate deepswe_preindex/trial
    path). This branch is unreachable in the leaderboard `deepswe_full.yml` run.
    """
    substrate = _substrate_brief()  # raises DeepSweAdapterError on a missing proof brief
    if substrate:
        return substrate

    # Past this point the substrate is NOT active and we are NOT in proof mode
    # (_substrate_brief would have raised otherwise). Host generation is allowed only
    # on the legacy non-proof preindex path — and we still refuse it under proof mode
    # as a defence in depth.
    if _proof_mode() or _substrate_active():
        _adapter_fail(
            "HOST_BRIEF_FORBIDDEN",
            "DEEPSWE_ADAPTER_FAIL: reached host brief generation while proof/substrate "
            "mode is active — host GT scoring is forbidden (no fallback). The substrate "
            "brief consume must have produced a brief or failed closed before this point.",
        )

    db = os.environ.get("GT_GRAPH_DB", "") or os.environ.get("GT_HOST_GRAPH_DB", "")
    root = os.environ.get("GT_REPO_ROOT", "") or os.environ.get("GT_HOST_SRC_ROOT", "")
    if not db or not root or not os.path.isfile(db):
        logger.info("GT: no substrate brief and no GT_GRAPH_DB/GT_REPO_ROOT -- "
                    "skipping brief (Phase 2, legacy non-proof path)")
        return ""
    try:
        from groundtruth.pretask.v1r_brief import generate_v1r_brief

        res = generate_v1r_brief(instruction, root, db)
        return (getattr(res, "brief_text", "") or "").strip()
    except Exception as e:  # noqa: BLE001
        # Legacy non-proof path: a brief failure under the strict full-stack flags is a
        # degraded dimension — re-raise so the run fails closed instead of shipping a
        # degraded trajectory. Otherwise correct-or-quiet skip.
        if (
            os.environ.get("GT_REQUIRE_EMBEDDER") == "1"
            or os.environ.get("GT_REQUIRE_FULL_STACK") == "1"
        ):
            logger.error(
                "GT: brief generation failed under strict full-stack (%s) -- "
                "refusing a degraded run", e,
            )
            raise
        logger.warning("GT: brief generation failed (%s) -- skipping", e)
        return ""


def _prepend_brief(brief: str, instruction: str) -> str:
    """Assemble the agent instruction with EXACTLY ONE ``<gt-task-brief>`` block (G2).

    brief.txt from the substrate (gt_run_proof.emit_brief -> generate_v1r_brief
    .brief_text, v1r_brief.py:1417) already STARTS with the ``<gt-task-brief>`` tag;
    the old unconditional wrap nested duplicate tags in the agent's prompt. Consume a
    pre-tagged brief as-is; wrap only when the tag is absent (legacy host-generated
    text). Empty brief -> instruction untouched (correct-or-quiet). Same single-tag
    invariant the OH wrapper pins (tests/preflight/test_brief_delivery_invariants.py).
    """
    if not brief:
        return instruction
    if re.search(r"<gt-task-brief\b", brief, re.IGNORECASE):
        return f"{brief}\n\n{instruction}"
    return f"<gt-task-brief>\n{brief}\n</gt-task-brief>\n\n{instruction}"


def _emit_gt_meta_witness() -> None:
    """Handoff §H — the DeepSWE adapter CONSUMPTION WITNESS.

    Prove (from the agent's own host, NOT telemetry) that this adapter read the
    SAME resolved graph + certs the pinned substrate produced — never a rebuild.

    The witness is meaningful only if the graph the adapter consumed fingerprints
    IDENTICALLY to the LSP certificate's post-LSP hash (graph_certificate.py:192-198
    enforces hook==post-LSP as GRAPH_FAIL_HASH_MISMATCH). So:
      * resolve the consumed graph via GTRuntimeContext.from_env (the NON-PROOF
        host-handoff branch, context.py:111-112 -> GT_HOST_GRAPH_DB / GT_CERT_DIR),
        READ-ONLY — we never index, never write, never rebuild;
      * fingerprint it with proof.graph_edges_hash (proof.py:225-245), the SAME
        canonical edge hash resolve/graph_certificate use;
      * read the LSP cert's graph_hash_after_lsp from $GT_CERT_DIR and compare;
      * format the canonical [GT_META] graph_witness line via
        graph_certificate.format_graph_witness (layer-1 GT code — we MAY call it),
        then append cert paths + substrate_digest (§H format).

    FAIL-CLOSED (hole #2, §E DEEPSWE_ADAPTER_FAIL / GRAPH_FAIL_HASH_MISMATCH): under
    PROOF mode (GT_PROOF_MODE=1) OR substrate-consume mode (_substrate_active) any
    failure to wire, fingerprint, or MATCH the substrate's post-LSP hash emits
    `[GT_META] ... error=DEEPSWE_ADAPTER_FAIL ...` AND RAISES ``DeepSweAdapterError``
    — a divergent or unconsumable graph HARD-STOPS the run, it does NOT
    warn-and-continue. The raise scope is CONSISTENT with the brief consume
    (``_substrate_brief`` raises under proof OR substrate) and with the
    DeepSweAdapterError contract ("under proof/substrate mode") — the witness was
    proof-only, leaving a substrate-active non-proof run to warn on a divergent
    graph (bug #7). Outside BOTH proof and substrate the same conditions print the
    classified line and return (warn), so legacy dev/CI is non-fatal.
    """
    import json as _json

    proof = _proof_mode()
    substrate = _substrate_active()

    def _fail(detail: str, *, prebuilt: str = "unknown") -> None:
        """Print the classified [GT_META] DEEPSWE_ADAPTER_FAIL line, then RAISE in
        proof/substrate mode (fail-closed) or return (warn) outside both."""
        print(f"[GT_META] gt_artifacts={os.environ.get('GT_CERT_DIR', '')}; "
              f"gt_prebuilt_active={prebuilt}; error=DEEPSWE_ADAPTER_FAIL "
              f"detail={detail}", flush=True)
        if proof or substrate:
            raise DeepSweAdapterError(f"DEEPSWE_ADAPTER_FAIL: {detail}")

    try:
        from groundtruth.runtime import proof as _proof
        from groundtruth.runtime.context import GTRuntimeContext
    except Exception as _ie:  # noqa: BLE001 -- layer-1 GT must be importable host-side
        _fail(f"import_failed:{_ie}", prebuilt="false")
        return

    # The canonical [GT_META] formatter is layer-1 GT code (graph_certificate.py) —
    # the handoff (§H) says the adapter MAY call it directly. scripts/metrics has no
    # __init__.py, so import the module by adding its dir to sys.path (the same shape
    # gt_run_proof.py:357-361 uses). Optional: an inline fallback formats the line if
    # the import is unavailable, so the witness is never blocked on it.
    format_graph_witness = None  # type: ignore[assignment]
    try:
        import importlib as _il
        import sys as _sys
        # _THIS_DIR is artifact_deepswe/; the repo root is its parent.
        _md = str((_THIS_DIR.parent / "scripts" / "metrics"))
        if os.path.isdir(_md) and _md not in _sys.path:
            _sys.path.insert(0, _md)
        format_graph_witness = getattr(
            _il.import_module("graph_certificate"), "format_graph_witness", None)
    except Exception:  # noqa: BLE001 -- inline fallback below
        format_graph_witness = None  # type: ignore[assignment]

    cert_dir = os.environ.get("GT_CERT_DIR", "")
    host_graph = os.environ.get("GT_HOST_GRAPH_DB", "")
    if not cert_dir and host_graph:
        cert_dir = os.path.dirname(host_graph)

    try:
        # Non-proof host-handoff resolution (context.py:111-112): resolves
        # GT_GRAPH_DB or GT_HOST_GRAPH_DB without entering proof-mode reject paths.
        ctx = GTRuntimeContext.from_env()
        resolved_db = ctx.graph_db or host_graph
        # A2 (2026-06-14): a PRESENT-but-0-BYTE handoff must NOT pass as prebuilt-active.
        # os.path.exists() is True on an empty copy; require a non-empty file so the
        # empty-but-present handoff is classified as a hard GT_ARTIFACT_MISSING here
        # (the host mirror of the in-container _guard_handoff_db fail-close) rather than
        # slipping to the graph_edges_hash_empty branch with a misleading prebuilt=true.
        try:
            _db_size = os.path.getsize(resolved_db) if (resolved_db and os.path.exists(resolved_db)) else -1
        except OSError:
            _db_size = -1
        prebuilt_active = _db_size > 0
        _present_but_empty = bool(resolved_db and os.path.exists(resolved_db) and _db_size <= 0)

        if not prebuilt_active:
            # No CONSUMABLE substrate graph (absent, or PRESENT-but-0-byte). In proof/
            # substrate mode this is a GT_ARTIFACT_MISSING fail-closed (the substrate graph
            # MUST be consumable — never rebuild, never fall back). Outside proof mode it is
            # the legacy host-fallback path (warn + return).
            _why = (f"present_but_empty (0-byte handoff copy at {resolved_db!r}; the REAL "
                    f"graph lives at <task>/graph.db — the copy was assembled empty)"
                    if _present_but_empty else
                    "substrate graph is absent and rebuild/fallback is forbidden")
            if proof or _substrate_active():
                _fail(
                    f"no_resolved_substrate_graph graph_db={resolved_db or '(unset)'} "
                    f"db_size={_db_size}B cert_dir={cert_dir or '(unset)'} "
                    f"(GT_ARTIFACT_MISSING — {_why})",
                    prebuilt="false",
                )
                return
            print(f"[GT_META] gt_artifacts={cert_dir or '(unset)'}; "
                  f"graph_db={resolved_db or '(unset)'}; gt_prebuilt_active=false; "
                  f"runtime_strategy={os.environ.get('GT_RUNTIME_STRATEGY', '')}; "
                  f"note=no_host_resolved_graph (host-fallback path, not substrate-consume)",
                  flush=True)
            return

        # READ-ONLY canonical fingerprint of the consumed graph (never writes).
        hook_hash = _proof.graph_edges_hash(resolved_db)
        if not hook_hash:
            _fail(f"graph_edges_hash_empty for {resolved_db!r}", prebuilt="true")
            return

        # Compare against the LSP cert's post-LSP hash (the substrate's authority).
        lsp_cert_path = (os.environ.get("GT_LSP_CERT", "")
                         or (os.path.join(cert_dir, "lsp_certificate.json") if cert_dir else ""))
        post_lsp_hash = ""
        if lsp_cert_path and os.path.isfile(lsp_cert_path):
            try:
                with open(lsp_cert_path, encoding="utf-8") as fh:
                    _lc = _json.load(fh)
                post_lsp_hash = (_lc.get("graph_hash_after_lsp")
                                 or _lc.get("graph_hash") or "")
            except (OSError, ValueError):
                post_lsp_hash = ""
        # Also accept the graph_certificate's post-LSP hash if the LSP cert lacked it.
        if not post_lsp_hash and cert_dir:
            _gc_path = os.path.join(cert_dir, "graph_certificate.json")
            if os.path.isfile(_gc_path):
                try:
                    with open(_gc_path, encoding="utf-8") as fh:
                        _gc = _json.load(fh)
                    post_lsp_hash = (_gc.get("graph_hash_after_lsp")
                                     or _gc.get("graph_hash") or "")
                except (OSError, ValueError):
                    post_lsp_hash = ""

        hash_match = bool(post_lsp_hash) and (hook_hash == post_lsp_hash)

        # Canonical witness line (layer-1 formatter) + the §H cert/digest suffix.
        if format_graph_witness is not None:
            base = format_graph_witness(host_resolved_graph_db=resolved_db,
                                        hook_graph_db=resolved_db,
                                        hook_graph_hash=hook_hash,
                                        prebuilt_active=prebuilt_active)
        else:
            base = (f"[GT_META] graph_witness host_resolved_graph_db={resolved_db} "
                    f"hook_graph_db={resolved_db} hook_graph_hash={hook_hash} "
                    f"_gt_prebuilt_active={prebuilt_active}")

        def _cert(name: str) -> str:
            return os.path.join(cert_dir, name) if cert_dir else ""

        digest = os.environ.get("GT_SUBSTRATE_DIGEST", "")
        print(
            f"{base} | gt_artifacts={cert_dir}; graph_db={resolved_db}; "
            f"graph_hash={hook_hash}; graph_hash_after_lsp={post_lsp_hash or '(absent)'}; "
            f"hook_graph_hash_matches_post_lsp={hash_match}; "
            f"lsp_certificate={_cert('lsp_certificate.json')}; "
            f"graph_certificate={_cert('graph_certificate.json')}; "
            f"embedder_certificate={_cert('embedder_certificate.json')}; "
            f"foundational_gate_report={_cert('foundational_gate_report.json')}; "
            f"gt_prebuilt_active=true; "
            f"runtime_strategy={os.environ.get('GT_RUNTIME_STRATEGY', 'unified_substrate')}; "
            f"substrate_digest={digest or '(unset)'}",
            flush=True,
        )
        # FAIL-CLOSED (hole #2): a divergent consumed graph must HARD-STOP under proof
        # mode, NOT warn. graph_certificate.classify_graph treats hook!=post-LSP as
        # GRAPH_FAIL_HASH_MISMATCH; the adapter is the in-agent enforcement of that.
        if post_lsp_hash and not hash_match:
            # The graph the adapter consumed is NOT the one the substrate certified.
            _fail(
                f"GRAPH_FAIL_HASH_MISMATCH hook_graph_hash={hook_hash} != "
                f"graph_hash_after_lsp={post_lsp_hash} — the consumed graph diverges "
                f"from the substrate's LSP-resolved graph",
                prebuilt="true",
            )
            return
        # In proof mode the substrate's authority hash MUST be present + matched —
        # an absent post-LSP hash means we cannot PROVE the consume is correct, which
        # is itself a fail-closed condition (no unprovable consume).
        if proof and not post_lsp_hash:
            _fail(
                f"post_lsp_hash_absent cert_dir={cert_dir or '(unset)'} — cannot prove "
                f"the consumed graph == the substrate's LSP-resolved graph "
                f"(GRAPH_FAIL_MISSING_HANDOFF analogue)",
                prebuilt="true",
            )
            return
    except DeepSweAdapterError:
        # _fail already printed + raised under proof; propagate the hard stop.
        raise
    except Exception as e:  # noqa: BLE001 -- surface, never swallow (§E)
        _fail(f"witness_exception:{e}", prebuilt="unknown")


# ---------------------------------------------------------------------------
# Write->test->fix retry loop (PIER_INTEGRATION.md §d Option 2 — the in-agent
# retry; gt_gt §15.6: "the verification loop" is the dominant measured lever:
# Reflexion NeurIPS 2023 +11pp; Self-Debug ICLR 2024; ORACLE-SWE Reproduction
# Test the largest single signal).
#
# CONFIRMED from pier 0.2.0 source (the "confirm which" the plan demanded):
#   * pier's Verifier is TRIAL-level (trial.py:318 `Verifier(task=self._task, …)`);
#     the agent receives only (instruction, environment, context) — there is NO
#     `self._task` on the agent, so pier's verifier is NOT callable from run().
#   * the verifier's test script (tests/test.sh) lives HOST-side in the task dir
#     and is uploaded to /tests only AT VERIFY TIME (verifier.py:134-143) — it
#     does not exist in the container while the agent runs.
#   * test.sh applies the HIDDEN test patch (/tests/test.patch) and captures
#     model.patch from the worktree — running it mid-run would LEAK the hidden
#     test suite into the agent's context (leakage MUST be 0; no FAIL_TO_PASS /
#     hidden tests in product logic) and contaminate the model.patch artifact.
# => the retry verifier calls the test command DIRECTLY: the repository's OWN
#    visible test suite, executed in the container via environment.exec().
#    Command source: GT_RETRY_TEST_CMD (explicit per-run config) or the
#    language-dispatched auto-detection below (ONE surface, not per-task logic).
#
# Flag: GT_RETRY_ON_VERIFIER_FAIL = number of retries (0 = off, the default —
# without the flag behavior is byte-identical to today). Cap: 2 retries
# (3 total attempts) to bound cost. Applied to BOTH arms (GT-on and
# GT_BASELINE) so a paired measurement compares the same harness.
#
# Oracle statelessness across retries (verified by construction): every
# attempt shells a NEW `mini-swe-agent` process in the container
# (mini_swe_agent.py:891-901 exec_as_agent), so gt_mini_patch's module-global
# latches (loaded per-interpreter via the .pth bootstrap) start FRESH each
# attempt, and the oracle recomputes its state from that attempt's own
# trajectory — exactly the right behavior for a retry.
# ---------------------------------------------------------------------------
_RETRY_FLAG = "GT_RETRY_ON_VERIFIER_FAIL"
_RETRY_MAX = 2                     # hard cap: 3 total attempts
_RETRY_TIMEOUT_DEFAULT = 600       # seconds per in-container test run
_FEEDBACK_TAIL_CHARS = 6000        # bounded failure-output dose

_RC_NO_ROOT = 96                   # repo root not found -> unverifiable
_RC_NO_RUNNER = 97                 # no recognizable test runner -> unverifiable

# Language-dispatched, repo-native test command (ONE surface — mirrors the LSP
# dispatch-by-extension rule; no per-task or per-benchmark logic). The repo
# root comes from /opt/gt/gt_root.txt (the GT install step) with a generic
# fallback scan.
_RETRY_TEST_AUTODETECT = (
    'R="$(cat /opt/gt/gt_root.txt 2>/dev/null)"; '
    'if [ -z "$R" ] || [ ! -d "$R" ]; then '
    'for d in /app /testbed /workspace /home/user /repo; do '
    '[ -d "$d/.git" ] && R="$d" && break; done; fi; '
    f'[ -z "$R" ] && exit {_RC_NO_ROOT}; cd "$R" || exit {_RC_NO_ROOT}; '
    "if [ -f go.mod ]; then go test ./... 2>&1; "
    "elif [ -f Cargo.toml ]; then cargo test 2>&1; "
    "elif [ -f package.json ] && grep -q '\"test\"' package.json; then "
    "npm test --silent 2>&1; "
    "elif [ -f pyproject.toml ] || [ -f setup.py ] || [ -f setup.cfg ] "
    "|| [ -f pytest.ini ] || [ -d tests ]; then python3 -m pytest -x -q 2>&1; "
    f"else exit {_RC_NO_RUNNER}; fi"
)

# D6 fix: split env detection into HARD (always env, never agent) and SOFT
# (could be the agent's own broken patch). Only HARD patterns classify as
# "unverifiable" — SOFT patterns are real test failures that the retry loop
# should give the agent feedback on.
#
# HARD: toolchain missing, network, syscall/wasm, corepack — the agent's
# patch cannot cause these.
_ENV_UNVERIFIABLE_RE = re.compile(
    r"(command not found|not recognized as"
    r"|error: could not find `Cargo\.toml`"
    r"|cannot find package|npm ERR! missing script"
    r"|Unable to locate package|no such file or directory: .*(?:go|cargo|npm|pytest)"
    r"|Connection refused|Network is unreachable|Temporary failure in name resolution"
    r"|CERTIFICATE_VERIFY_FAILED|ReadTimeoutError|ProxyError"
    r"|error while loading shared libraries|cannot open shared object"
    r"|syscall/js|GOOS=js|js/wasm"
    r"|corepack|ENOENT.*yarn|ENOENT.*pnpm"
    r"|Could not build wheels|subprocess-exited-with-error|metadata-generation-failed"
    r"|ERROR: Could not find a version|No matching distribution found)",
    re.I,
)
# SOFT: patterns that CAN be agent-caused (broken import, compile error,
# missing symbol). These are now classified as "fail" → retry gets feedback.
# Previously they were in _ENV_UNVERIFIABLE_RE and ate 5/9 tasks.


def _retry_count() -> int:
    """Retries from GT_SELF_VERIFY_ATTEMPTS (product name) or the legacy
    GT_RETRY_ON_VERIFIER_FAIL, capped at _RETRY_MAX.
    Unset/0/garbage -> 0 (off: exact single-attempt legacy behavior).
    GT_SELF_VERIFY_ATTEMPTS takes precedence (the product name for the outer
    safety ring — verification that fires AFTER the in-run horizon)."""
    raw = os.environ.get("GT_SELF_VERIFY_ATTEMPTS", "").strip()
    if not raw:
        raw = os.environ.get(_RETRY_FLAG, "").strip()
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return 0
    return max(0, min(n, _RETRY_MAX))


def _retry_test_command() -> tuple[str, str]:
    """(command, display_name) for the in-loop verifier check. GT_RETRY_TEST_CMD
    wins (explicit per-run config); otherwise the auto-detected repo-native
    runner."""
    explicit = os.environ.get("GT_RETRY_TEST_CMD", "").strip()
    if explicit:
        return explicit, explicit
    return _RETRY_TEST_AUTODETECT, "repository test suite (auto-detected runner)"


_FAILURE_CLASSIFIERS: list[tuple[str, re.Pattern[str]]] = [
    # D6: compile_error catches agent-caused breakage that was previously eaten
    # by _ENV_UNVERIFIABLE_RE. These patterns now reach the classifier.
    ("compile_error", re.compile(
        r"ModuleNotFoundError|ImportError|No module named"
        r"|fatal error: |compilation terminated|undefined reference to"
        r"|ld returned \d+ exit status|collect2: error"
        r"|TS2307|Cannot find module|TS1005|TS2345"
        r"|AttributeError: module '[\w.]+' has no attribute"
        r"|errors? during collection|ERROR collecting",
        re.I)),
    ("assertion_error", re.compile(
        r"AssertionError|assert\s+.*failed|FAILED.*assert"
        r"|expected.*got|test result: FAILED",
        re.I)),
    ("missing_feature", re.compile(
        r"not implemented|NotImplementedError|TODO|FIXME.*not.*implement",
        re.I)),
]


def _classify_verifier_failure(output: str) -> str:
    """P6: bucket test/verifier output for targeted retry feedback."""
    text = output or ""
    for label, pat in _FAILURE_CLASSIFIERS:
        if pat.search(text):
            return label
    return "test_failure"


def _format_test_feedback(attempt: int, display_cmd: str, rc: int,
                          output: str) -> str:
    """The structured failure message prepended to the retry attempt's
    instruction. Arm-neutral tag (<test-feedback>, not <gt-…>): this is
    harness execution feedback, identical across GT-on and baseline arms."""
    tail = (output or "").strip()
    if len(tail) > _FEEDBACK_TAIL_CHARS:
        tail = "…(truncated)…\n" + tail[-_FEEDBACK_TAIL_CHARS:]
    failure_kind = _classify_verifier_failure(tail)
    repair_hint = {
        "compile_error": (
            "Your change broke compilation or imports. Check for: missing "
            "imports, wrong module paths, undefined symbols, type errors. "
            "Fix the compile/import error before re-running tests."),
        "assertion_error": (
            "Logic bug: a failing assertion disagrees with your change. "
            "Re-read the assertion and fix the implementation."),
        "missing_feature": (
            "Incomplete implementation: new behavior is missing or partial. "
            "Cover the unmet requirement before finishing."),
        "test_failure": (
            "Fix the failures: re-read the failing assertions, correct your "
            "changes in the repository (your previous edits are still present), "
            "and re-run the tests to confirm they pass before you finish."),
    }[failure_kind]
    return (
        f'<test-feedback attempt="{attempt}" failure="{failure_kind}">\n'
        "Tests failed: your previous attempt did not pass the test suite.\n"
        f"Command: {display_cmd}\n"
        f"Exit code: {rc}\n"
        f"Failure class: {failure_kind}\n"
        "Failing output (tail):\n"
        f"{tail}\n"
        f"{repair_hint}\n"
        "</test-feedback>"
    )


_BASE_AGENT = MiniSweAgent if MiniSweAgent is not None else object


class GTMiniSweAgent(_BASE_AGENT):  # type: ignore[misc]
    """MiniSweAgent with full 3-phase GroundTruth integration.

    Phase 1: graph.db indexing in the container (install_spec)
    Phase 2: L1 brief prepended to instruction (run)
    Phase 3: observation interception via gt_mini_patch.py (install_spec)
    Phase 4: optional write->test->fix retry loop (GT_RETRY_ON_VERIFIER_FAIL)

    Set GT_BASELINE=1 to disable all GT injection (control arm).
    """

    @staticmethod
    def name() -> str:
        return "gt-mini-swe-agent"

    @staticmethod
    def gt_host_mounts(host_gt_inject_dir: str = "/tmp/gt_inject/opt/gt") -> list[dict]:
        """Return the pier --mounts-json entry that pre-stages GT files into the container.

        Used by the GHA workflows (deepswe_full.yml / swebench_pro_full.yml) when
        GT_MOUNT_MODE=1: the caller stages all GT files under ``host_gt_inject_dir`` on
        the host, then passes this mount spec to ``pier run --mounts-json``.

        The mount is READ-ONLY (/opt/gt:ro).  The import-chain patch (_APPEND_TO_MINI)
        writes to container-internal site-packages, not to /opt/gt, so :ro is correct.

        Args:
            host_gt_inject_dir: Absolute host path where the workflow staged GT files.
                Defaults to ``/tmp/gt_inject/opt/gt`` (the GHA convention).

        Returns:
            A list with one ServiceVolumeConfig-compatible dict (pier --mounts-json).
        """
        return [
            {
                "type": "bind",
                "source": host_gt_inject_dir,
                "target": _GT_DIR,
                "read_only": True,
            }
        ]

    def install_spec(self) -> AgentInstallSpec:
        """Extend parent install_spec with GT injection steps."""
        spec = super().install_spec()
        if not _GT_BASELINE:
            spec.steps.extend(_inject_steps())
        return spec

    async def _retry_verifier_check(
        self, environment: BaseEnvironment, cmd: str
    ) -> tuple[str, int, str]:
        """Run the in-loop verifier command in the container and classify the
        result: ('pass'|'fail'|'unverifiable', return_code, output).
        Correct-or-quiet: any shape that is not a REAL test failure (missing
        root/runner, environment failure, exec error, timeout) is
        'unverifiable' — never injected as feedback."""
        timeout_raw = os.environ.get("GT_RETRY_TEST_TIMEOUT_SEC", "").strip()
        try:
            timeout = int(timeout_raw) if timeout_raw else _RETRY_TIMEOUT_DEFAULT
        except (TypeError, ValueError):
            timeout = _RETRY_TIMEOUT_DEFAULT
        try:
            res = await environment.exec(command=cmd, timeout_sec=timeout)
        except Exception as e:  # noqa: BLE001 -- exec/timeout failure != test failure
            return "unverifiable", -1, f"verifier exec failed: {e}"
        out = (res.stdout or "")
        if res.stderr:
            out = f"{out}\n{res.stderr}" if out else res.stderr
        rc = res.return_code
        if rc == 0:
            return "pass", 0, out
        if rc in (_RC_NO_ROOT, _RC_NO_RUNNER):
            return "unverifiable", rc, out
        if _ENV_UNVERIFIABLE_RE.search(out or ""):
            return "unverifiable", rc, out
        return "fail", rc, out

    async def _archive_attempt_artifacts(
        self, environment: BaseEnvironment, attempt: int
    ) -> None:
        """Preserve the finished attempt's trajectory/log before the retry
        overwrites them (best-effort; /logs is the mounted trial dir)."""
        try:
            ad = str(EnvironmentPaths.agent_dir)
            await environment.exec(command=(
                f"mv {ad}/mini-swe-agent.trajectory.json "
                f"{ad}/mini-swe-agent.trajectory.attempt{attempt}.json 2>/dev/null; "
                f"cp {ad}/mini-swe-agent.txt "
                f"{ad}/mini-swe-agent.attempt{attempt}.txt 2>/dev/null; true"
            ))
        except Exception:  # noqa: BLE001 -- archival must never break the loop
            pass

    async def _run_with_test_retry(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None:
        """The write->test->fix loop (Option 2): run the agent; run the
        repo-native test command in the SAME container (edits persist); on a
        REAL failure, prepend the structured failure output to the original
        instruction and re-enter the agent loop. Capped at _RETRY_MAX retries.
        GT_RETRY_ON_VERIFIER_FAIL unset/0 -> exactly one super().run(), no
        verifier exec — byte-identical to the pre-retry behavior."""
        retries = _retry_count()
        if retries <= 0:
            await super().run(instruction, environment, context)
            return
        cmd, display_cmd = _retry_test_command()
        attempt_instruction = instruction
        total_attempts = retries + 1
        for attempt in range(1, total_attempts + 1):
            # D2 note: oracle state reset is NOT needed here — each super().run()
            # spawns a NEW mini-swe-agent process in the container (exec_as_agent),
            # so gt_mini_patch state is fresh by construction per attempt. A host-side
            # reset would import a DIFFERENT module instance and have no effect.
            await super().run(attempt_instruction, environment, context)
            if attempt >= total_attempts:
                break
            status, rc, output = await self._retry_verifier_check(environment, cmd)
            print(f"[GT_RETRY] attempt={attempt}/{total_attempts} "
                  f"status={status} rc={rc}", flush=True)
            if status != "fail":
                # pass -> done; unverifiable -> stop (never inject non-test noise).
                break
            await self._archive_attempt_artifacts(environment, attempt)
            # Latest feedback + the ORIGINAL instruction (no accumulation —
            # one bounded feedback block per attempt, dose-disciplined).
            gate_note = ""
            if not _GT_BASELINE:
                # CP012 option 2: patch-intercept path — retry loop blocks
                # another submit until tests pass; remind about unverified reqs.
                gate_note = (
                    "GT pre_submit_intervention (not a hard block): your patch "
                    "is still in the repo. Run targeted tests for every edited "
                    "requirement before you finish — unverified obligations are "
                    "the top hidden verifier failure mode.\n\n"
                )
            attempt_instruction = (
                gate_note
                + _format_test_feedback(attempt + 1, display_cmd, rc, output)
                + "\n\n" + instruction
            )

    async def run(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None:
        """Run mini-swe-agent with GT brief and preamble prepended."""
        if _GT_BASELINE:
            # Control arm: pure mini-swe-agent, no GT content at all.
            # The retry loop is HARNESS mechanics, not GT content — it applies
            # to both arms so paired measurement compares the same harness.
            await self._run_with_test_retry(instruction, environment, context)
            return

        augmented = instruction

        # D1 (Priority C) — EARLY fail-closed: in proof mode the substrate handoff
        # (authoritative graph + certs + brief) MUST be wired before any GT work.
        # Aborts up front if GT_PROOF_MODE=1 without _substrate_active()/handoff env,
        # so a missing handoff can never silently reach the agent (the integration
        # never re-indexes/re-briefs in proof mode).
        _assert_substrate_handoff()

        # Handoff §H — emit the consumption WITNESS before the agent runs: prove this
        # adapter read the SAME resolved graph the substrate produced (hook hash ==
        # post-LSP hash), READ-ONLY, never rebuilt. FAIL-CLOSED (hole #2): under proof
        # mode this RAISES DeepSweAdapterError on a hash mismatch / unconsumable graph,
        # hard-stopping the run before any model spend (no warn-and-continue).
        _emit_gt_meta_witness()

        # Phase 2: L1 brief — substrate brief consumed READ-ONLY (hole #3). FAIL-CLOSED
        # under proof mode: raises DeepSweAdapterError if the substrate brief is
        # absent/empty (NO host-side generate_v1r_brief fallback in proof mode).
        # G2: _prepend_brief guarantees exactly ONE <gt-task-brief> block — the
        # substrate brief is already tagged (v1r_brief.py:1417); never wrap it twice.
        brief = _generate_brief(instruction)
        augmented = _prepend_brief(brief, augmented)

        # Phase 3 preamble: tell the agent about automatic evidence injection.
        # If the patch is available, use the automatic preamble; otherwise
        # fall back to the manual gt_hook.py usage instructions.
        if _PATCH_CONTENT:
            augmented = augmented.rstrip() + "\n" + _GT_PREAMBLE
        else:
            augmented = augmented.rstrip() + "\n" + _GT_MANUAL_PREAMBLE

        # Verification: persist the EXACT instruction handed to the agent so
        # brief-reached-agent is provable from the real agent input (deterministic,
        # not telemetry and not a fragile filename match against the repo).
        try:
            import hashlib
            import json as _json
            import os as _os

            _os.makedirs("/tmp/gt", exist_ok=True)
            with open("/tmp/gt/delivered_instruction.txt", "w", encoding="utf-8") as _vf:
                _vf.write(augmented)
            _brief_hash = hashlib.sha256((brief or "").encode("utf-8")).hexdigest()
            witness = {
                "schema": "gt.adapter_witness.v1",
                "gt_prebuilt_active": _os.environ.get("GT_GRAPH_DB") is not None,
                "proof_mode": _proof_mode(),
                "baseline_arm": _GT_BASELINE,
                "self_verifier_retry": _retry_count() > 0,
                "official_verifier_repair": False,
                "delivered_instruction_sha256": hashlib.sha256(
                    augmented.encode("utf-8")
                ).hexdigest(),
                "substrate_brief_sha256": _brief_hash,
                "delivered_brief_block_sha256": _brief_hash,
                "delivered_contains_substrate_brief": bool(brief and brief in augmented),
                "brief_match": bool(brief and brief in augmented),
                "brief_match_semantics": "delivered_brief_block_or_containment",
                "brief_chars": len(brief or ""),
            }
            with open("/tmp/gt/adapter_witness.json", "w", encoding="utf-8") as _wf:
                _json.dump(witness, _wf, indent=2)
        except Exception:
            pass

        await self._run_with_test_retry(augmented, environment, context)
