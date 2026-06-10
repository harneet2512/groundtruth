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
import textwrap
from pathlib import Path
from pier.agents.installed.mini_swe_agent import MiniSweAgent
from pier.environments.base import BaseEnvironment
from pier.models.agent.context import AgentContext
from pier.models.agent.install import AgentInstallSpec, InstallStep

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

# ---------------------------------------------------------------------------
# Locate the two payloads we inject into the container
# ---------------------------------------------------------------------------
_GT_HOOK_CANDIDATES = [
    _THIS_DIR / "benchmarks" / "swebench" / "gt_hook.py",
    _THIS_DIR.parent / "benchmarks" / "swebench" / "gt_hook.py",
    _THIS_DIR / "gt_hook.py",
]
_PATCH_PATH = _THIS_DIR / "gt_mini_patch.py"


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
    "os.path.exists('/tmp/graph.db')))\n"
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


def _inject_steps() -> list[InstallStep]:
    """Build the install steps for GT injection.

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
        steps.append(InstallStep(user="root", run=_BUILD_GRAPH_DB))

    # --- Self-test: verify patch loaded (fails build with diagnostic if not) ---
    if patch_chunks:
        steps.append(InstallStep(user="root", run=_SELFTEST_STEP))

    return steps


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
    if brief.lstrip().startswith("<gt-task-brief"):
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
        prebuilt_active = bool(resolved_db and os.path.exists(resolved_db))

        if not prebuilt_active:
            # No substrate graph resolved/present. In proof/substrate mode this is a
            # GT_ARTIFACT_MISSING fail-closed (the substrate graph MUST be consumable —
            # never rebuild, never fall back). Outside proof mode it is the legacy
            # host-fallback path (warn + return).
            if proof or _substrate_active():
                _fail(
                    f"no_resolved_substrate_graph graph_db={resolved_db or '(unset)'} "
                    f"cert_dir={cert_dir or '(unset)'} (GT_ARTIFACT_MISSING — the "
                    f"substrate graph is absent and rebuild/fallback is forbidden)",
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


class GTMiniSweAgent(MiniSweAgent):
    """MiniSweAgent with full 3-phase GroundTruth integration.

    Phase 1: graph.db indexing in the container (install_spec)
    Phase 2: L1 brief prepended to instruction (run)
    Phase 3: observation interception via gt_mini_patch.py (install_spec)

    Set GT_BASELINE=1 to disable all GT injection (control arm).
    """

    @staticmethod
    def name() -> str:
        return "gt-mini-swe-agent"

    def install_spec(self) -> AgentInstallSpec:
        """Extend parent install_spec with GT injection steps."""
        spec = super().install_spec()
        if not _GT_BASELINE:
            spec.steps.extend(_inject_steps())
        return spec

    async def run(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None:
        """Run mini-swe-agent with GT brief and preamble prepended."""
        if _GT_BASELINE:
            # Control arm: pure mini-swe-agent, no GT content at all
            await super().run(instruction, environment, context)
            return

        augmented = instruction

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
            import os as _os
            _os.makedirs("/tmp/gt", exist_ok=True)
            with open("/tmp/gt/delivered_instruction.txt", "w", encoding="utf-8") as _vf:
                _vf.write(augmented)
        except Exception:
            pass

        await super().run(augmented, environment, context)
