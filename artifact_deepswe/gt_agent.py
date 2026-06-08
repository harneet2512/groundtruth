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

All phases are best-effort / correct-or-quiet: failures fall back silently to
the unaugmented mini-swe-agent behavior.

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

_GT_BASELINE = bool(os.environ.get("GT_BASELINE"))
_THIS_DIR = Path(__file__).resolve().parent

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

# --- groundtruth per-action import-closure — mirrors the OH path's _bundle_dir_payload
# provisioning. Ships the graph-based per-action engines the mini-swe push path now calls for
# PARITY with OH (gt_gt §6): post_edit (L3 contract evidence + contract-DELTA), post_view (L3b
# contracts/graph-nav), curation_map/contract_map (facts-only), drift_cli/drift_hook, + the
# state/runtime deps. import-closure is STDLIB-ONLY (verified: no pydantic / 3rd-party), so we
# ship those modules as a tar.gz and extract them into the container's python path — no pip.
# Covers hooks/ pretask/ runtime/ state/ + _binary.py + __init__.py (post_view/post_edit pull
# groundtruth.state, which the old drift-only closure missed).
_SRC_GT = _THIS_DIR.parent / "src" / "groundtruth"


def _drift_closure_modules() -> list:
    """Compute the per-action import-closure as the set of ``groundtruth.*`` modules
    transitively reachable from the two ``-m`` entry points (post_edit, post_view)
    whose TOP-LEVEL imports are stdlib-only — i.e. importable under ``python3 -S``
    with NO site-packages (the exact in-container runtime ``_run_graph_engine`` uses).

    Why computed, not a hardcoded dir list: post_edit/post_view fan out (lazily, at
    runtime) into index/ evidence/ graph/ telemetry/ config/ utils/ validators/
    confidence/ analysis/ — eleven subpackages beyond hooks/pretask/runtime/state.
    A static list silently rots when an import is added; this walk is complete by
    construction and re-derives itself every build.

    Heavy-dep modules (top-level numpy / onnxruntime / sentence_transformers /
    structlog — the embedder + brief-side ranking) are AUTO-EXCLUDED: they cannot
    import under ``-S`` and are reached only via runtime-guarded lazy imports that
    degrade correct-or-quiet. The closure stays stdlib-only and the core
    deterministic evidence path runs identically across all 5 languages (it reads
    graph.db, which is language-agnostic). Package ``__init__.py`` ancestors of every
    kept module are included so the packages import.

    Returns a sorted list of ``(abs_path, arcname)`` tuples. Falls back to the legacy
    4-dir shipment if AST introspection fails, so a build is never left empty.
    """
    import ast
    import sys as _sys

    stdlib = set(getattr(_sys, "stdlib_module_names", set())) | {"__future__"}

    def _mod_path(mod: str):
        rel = mod[len("groundtruth.") :].replace(".", "/")
        p = _SRC_GT / (rel + ".py")
        if p.is_file():
            return p
        pk = _SRC_GT / rel / "__init__.py"
        return pk if pk.is_file() else None

    _scan_cache: dict = {}

    def _scan(mod: str):
        """(deep_gt, top_gt, direct_heavy) for one module.

        deep_gt  — every ``groundtruth.*`` imported ANYWHERE (lazy included): the
                   DISCOVERY edges, since a lazy ``import groundtruth.index...`` fires
                   at runtime under ``-S``.
        top_gt   — ``groundtruth.*`` imported at TOP LEVEL: the edges that decide
                   whether THIS module can import under ``-S`` (transitive shippability).
        direct_heavy — a TOP-LEVEL non-stdlib, non-groundtruth import (numpy / structlog
                   / onnxruntime / sentence_transformers): makes the module itself
                   un-importable under ``-S``."""
        if mod in _scan_cache:
            return _scan_cache[mod]
        p = _mod_path(mod)
        deep: set = set()
        top: set = set()
        heavy = False
        if p is None:
            _scan_cache[mod] = (deep, top, True)
            return _scan_cache[mod]
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except Exception:
            _scan_cache[mod] = (deep, top, True)  # unparseable -> skip, never crash build
            return _scan_cache[mod]
        toplevel_ids = {id(n) for n in tree.body}
        for n in ast.walk(tree):
            names = []
            if isinstance(n, ast.ImportFrom) and n.module:
                names = [n.module]
            elif isinstance(n, ast.Import):
                names = [a.name for a in n.names]
            for nm in names:
                first = nm.split(".")[0]
                is_top = id(n) in toplevel_ids
                if nm.startswith("groundtruth."):
                    deep.add(nm)
                    if is_top:
                        top.add(nm)
                elif first not in stdlib and is_top:
                    heavy = True
        _scan_cache[mod] = (deep, top, heavy)
        return _scan_cache[mod]

    _ship_cache: dict = {}

    def _shippable(mod: str, stack: tuple = ()):
        """True iff ``mod`` imports under ``-S`` — no direct heavy top-level dep AND
        every top-level groundtruth dep is itself shippable (TRANSITIVE). Cyclic
        edges are treated as satisfiable so a legit import cycle doesn't exclude the
        whole group."""
        if mod in _ship_cache:
            return _ship_cache[mod]
        if mod in stack:
            return True  # cycle: assume OK (resolved by the other members)
        if _mod_path(mod) is None:
            _ship_cache[mod] = False
            return False
        _deep, top, heavy = _scan(mod)
        if heavy:
            _ship_cache[mod] = False
            return False
        ok = all(_shippable(d, stack + (mod,)) for d in top)
        _ship_cache[mod] = ok
        return ok

    # DISCOVERY: deep-walk the reachable universe from the two -m entry points,
    # following lazy imports so nothing runtime-reachable is missed.
    entry = ("groundtruth.hooks.post_edit", "groundtruth.hooks.post_view")
    universe: set = set()
    queue: list = list(entry)
    while queue:
        m = queue.pop()
        if m in universe:
            continue
        universe.add(m)
        deep, _top, _heavy = _scan(m)
        for g in deep:
            if g not in universe:
                queue.append(g)
        # ancestor package __init__ chain is part of the universe (needed to import)
        parts = m.split(".")
        for i in range(2, len(parts)):
            anc = ".".join(parts[:i])
            if anc not in universe:
                queue.append(anc)

    # SHIP = reachable AND transitively -S-importable. The rest are reached only via
    # runtime-guarded lazy imports and degrade correct-or-quiet.
    keep: dict = {}
    for m in universe:
        p = _mod_path(m)
        if p is not None and _shippable(m):
            keep[m] = p

    out: dict = {}
    for top in ("__init__.py", "_binary.py"):
        p = _SRC_GT / top
        if p.is_file():
            out[f"groundtruth/{top}"] = str(p)
    for m, p in keep.items():
        arc = "groundtruth/" + str(p.relative_to(_SRC_GT)).replace("\\", "/")
        out[arc] = str(p)
    return sorted(out.items())


def _build_drift_tarball_b64() -> str | None:
    """tar.gz the per-action import-closure under arcname ``groundtruth/...`` and
    return base64. The module set is computed transitively (see
    ``_drift_closure_modules``) so the fixed post_edit/post_view run their FULL
    deterministic evidence path in-container under ``python3 -S`` on every language.
    None if source absent."""
    if not _SRC_GT.is_dir():
        logger.warning("GT: per-action closure source not found at %s", _SRC_GT)
        return None
    import io
    import tarfile

    try:
        members = _drift_closure_modules()
    except Exception as exc:  # never let introspection break a build
        logger.warning("GT: closure introspection failed (%s); using legacy 4-dir shipment", exc)
        members = None

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        if members:
            for arc, src in members:
                tar.add(src, arcname=arc)
        else:
            for top in ("__init__.py", "_binary.py"):
                p = _SRC_GT / top
                if p.is_file():
                    tar.add(str(p), arcname=f"groundtruth/{top}")
            for sub in ("hooks", "pretask", "runtime", "state"):
                d = _SRC_GT / sub
                if not d.is_dir():
                    continue
                for f in sorted(d.rglob("*.py")):
                    arc = "groundtruth/" + str(f.relative_to(_SRC_GT)).replace("\\", "/")
                    tar.add(str(f), arcname=arc)
    return base64.b64encode(buf.getvalue()).decode("ascii")


_DRIFT_TARBALL_B64 = _build_drift_tarball_b64()

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

    # --- groundtruth drift import-closure: make `gt drift` (groundtruth.hooks.drift_cli)
    # importable in the agent container. Inject the stdlib-only closure tar.gz, extract it,
    # and install into python SITE-PACKAGES (robust for pier's /bin/sh `gt` shim, which does
    # not source ~/.bashrc), with a PYTHONPATH fallback. Mirrors OH's bundle provisioning;
    # guarded so it never clobbers an existing groundtruth.
    if _DRIFT_TARBALL_B64:
        tgz = f"{_GT_DIR}/_drift/gt_closure.tgz"
        steps.append(InstallStep(user="root", run=f"mkdir -p {_GT_DIR}/_drift"))
        chunks = [_DRIFT_TARBALL_B64[i:i + _B64_CHUNK_SIZE]
                  for i in range(0, len(_DRIFT_TARBALL_B64), _B64_CHUNK_SIZE)]
        for i, chunk in enumerate(chunks):
            op = ">" if i == 0 else ">>"
            steps.append(InstallStep(user="root", run=f'echo "{chunk}" {op} {tgz}.b64'))
        steps.append(InstallStep(user="root", run=(
            f"base64 -d {tgz}.b64 > {tgz} "
            f"&& tar xzf {tgz} -C {_GT_DIR}/_drift "
            f"&& rm -f {tgz}.b64 {tgz}")))
        steps.append(InstallStep(user="root", run=(
            'SP="$(python3 -c "import site;print(site.getsitepackages()[0])" 2>/dev/null '
            '|| python3 -c "import sysconfig;print(sysconfig.get_paths()[\'purelib\'])" 2>/dev/null)"; '
            f'if [ -n "$SP" ] && [ ! -e "$SP/groundtruth" ]; then '
            f'cp -r {_GT_DIR}/_drift/groundtruth "$SP/" && echo "GT: drift closure -> $SP" >&2; '
            f'else for rc in /etc/profile "$HOME/.profile" "$HOME/.bashrc" /root/.bashrc; do '
            f'grep -q "PYTHONPATH={_GT_DIR}/_drift" "$rc" 2>/dev/null '
            f'|| echo "export PYTHONPATH={_GT_DIR}/_drift:\\${{PYTHONPATH:-}}" >> "$rc" 2>/dev/null || true; '
            f'done; echo "GT: drift closure PYTHONPATH fallback {_GT_DIR}/_drift" >&2; fi; '
            f'PYTHONPATH={_GT_DIR}/_drift:${{PYTHONPATH:-}} python3 -c "import groundtruth.hooks.drift_cli" '
            f'&& echo "GT: drift_cli importable" >&2 || echo "GT: drift_cli NOT importable" >&2'
        )))

    # --- Repo root detection ---
    steps.append(InstallStep(user="root", run=_ROOT_DETECT))

    # --- Phase 1: graph.db indexing (best-effort) ---
    steps.append(InstallStep(user="root", run=_BUILD_GRAPH_DB))

    # --- Self-test: verify patch loaded (fails build with diagnostic if not) ---
    if patch_chunks:
        steps.append(InstallStep(user="root", run=_SELFTEST_STEP))

    return steps


def _generate_brief(instruction: str) -> str:
    """Phase 2: host-side brief from a preindexed graph.db.

    Requires GT_GRAPH_DB and GT_REPO_ROOT env vars on the HOST pointing to
    a pre-built graph.db and the repo root.  When running the deepswe_preindex
    workflow, these are set from the pre-indexed artifacts.
    """
    db = os.environ.get("GT_GRAPH_DB", "")
    root = os.environ.get("GT_REPO_ROOT", "")
    if not db or not root or not os.path.isfile(db):
        logger.info("GT: no GT_GRAPH_DB/GT_REPO_ROOT -- skipping brief (Phase 2)")
        return ""
    try:
        from groundtruth.pretask.v1r_brief import generate_v1r_brief

        res = generate_v1r_brief(instruction, root, db)
        return (getattr(res, "brief_text", "") or "").strip()
    except Exception as e:  # noqa: BLE001
        # §7 PARITY (matches the OH wrapper host-primary fix): under the strict
        # full-stack flags a brief failure is most often a DEGRADED dimension
        # (e.g. GT_REQUIRE_EMBEDDER=1 raising because the ONNX embedder did not
        # load -> W_SEM would be 0). "No silent fallback — the stack is live or
        # the run aborts": re-raise so the paid run fails closed instead of
        # silently shipping a brief-less (or semantic-less) trajectory. Only the
        # non-strict path keeps the correct-or-quiet skip.
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

        # Phase 2: host-side brief
        brief = _generate_brief(instruction)
        if brief:
            augmented = (
                f"<gt-task-brief>\n{brief}\n</gt-task-brief>\n\n{augmented}"
            )

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
