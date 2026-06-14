"""In-container GroundTruth patch for mini-swe-agent (observation interception).

Injected into the task container and loaded at interpreter startup via a .pth
file in site-packages (primary) or an append to mini-swe-agent's default.py
(backup). Patches the environment's execute() method to append GT evidence
after edit/view commands.

Attachment mapping (GT integration guide -> mini-swe-agent):
  run_action            -> Environment.execute
  classify_tool_event   -> _classify(command)
  observation text      -> output["output"] (rendered into <output> by the
                           model's format_observation_messages, so appended
                           text reaches the agent verbatim)
  GT_BASELINE switch    -> _GT_BASELINE early no-op

Per-turn <gt-evidence> is built DIRECTLY from graph.db (tree-sitter, ALL
languages) via the same deterministic, categorical-fact-gated pillars the
host-side brief uses (resolved-witness / caller-contract / sibling +
edit-target callee contracts). This is pure SQL and cross-language by
construction — it replaces the old `gt_hook.py understand/verify` route, which
was Python-`ast`-only (`.py`-filtered) and therefore emitted EMPTY evidence on
the ~70% of DeepSWE tasks that are Go/Rust/TS/JS.

The pillar logic is PORTED INLINE here (stdlib-only) rather than imported from
`groundtruth.pretask.*`, because only the two single files gt_hook.py +
gt_mini_patch.py (plus /tmp/graph.db) are injected into the task container —
the full `groundtruth` package is NOT importable in-container. The categorical
FACT gate (`_DETERMINISTIC_METHODS`) + stdlib-shadow guard are reproduced
verbatim from curation_map / v1r_brief so no name_match edge is ever laundered
as a fact (parity with the brief).

gt_hook.py is still injected at /opt/gt/gt_hook.py for the agent's optional
manual use, but the AUTOMATIC per-view/per-edit evidence no longer routes
through it.
"""
from __future__ import annotations

import enum
import hashlib
import json
import os
import sys as _sys
import re
import subprocess
import sys

# Graceful import of groundtruth.runtime.* — these modules live in the substrate's
# baked src/ tree. If the import path isn't available (container bootstrap timing,
# missing substrate, dev environment), fall back to inline stubs so the core
# delivery engine (pre-CP011 behavior) still works. The CP011-015 features
# (phase policy, action templates, context budget, ledger) degrade to no-ops.
_GT_SRC = os.path.join(os.environ.get("GT_HOME", "/opt/gt"), "src")
if os.path.isdir(_GT_SRC) and _GT_SRC not in sys.path:
    sys.path.insert(0, _GT_SRC)

_RUNTIME_AVAILABLE = False
try:
    from groundtruth.runtime.action_translation import translate_to_action as _product_translate_to_action
    from groundtruth.runtime.context_budget import ContextBudgeter as _ProductContextBudgeter
    from groundtruth.runtime.ledger import Ledger as _ProductLedger
    from groundtruth.runtime.ledger import LedgerEntry as _ProductLedgerEntry
    from groundtruth.runtime.ledger import SignalOutcome as _ProductSignalOutcome
    from groundtruth.runtime.trajectory_state import TrajectoryState as _ProductTrajectoryState
    from groundtruth.runtime.trajectory_state import derive_phase as _product_derive_phase
    from groundtruth.runtime.verification_horizon import HorizonThresholds as _ProductHorizonThresholds
    from groundtruth.runtime.verification_horizon import composite_severity as _product_composite_severity
    from groundtruth.runtime.verification_horizon import render_verify_emission as _product_render_verify_emission
    from groundtruth.runtime.verification_horizon import verify_horizon_band as _product_verify_horizon_band
    from groundtruth.runtime.edit_risk import structural_edit_risk as _structural_edit_risk
    _RUNTIME_AVAILABLE = True
except ImportError as _import_err:
    # Fallback stubs — pre-CP011 behavior
    print(f"[GT_META] runtime_import_fallback=true reason={_import_err}", file=sys.stderr, flush=True)
    _structural_edit_risk = None  # type: ignore
    def _product_translate_to_action(block, phase=None):
        return block
    class _ProductContextBudgeter:
        def __init__(self, *a, **kw): pass
        def trim(self, payload, max_tokens=500):
            class _R:
                text = payload
                meta = {}
                pending_lines = payload.splitlines() if payload else []
            return _R()
        def commit_delivered(self, lines): pass
        def reset(self): pass
    class _ProductLedger:
        def record(self, *a): pass
    class _ProductLedgerEntry:
        def __init__(self, **kw): pass
    class _ProductSignalOutcome:
        DELIVERED = "delivered"
        SUPPRESSED_WRONG_PHASE = "suppressed_wrong_phase"
        # LANE-SPLIT 2026-06-13: Lane A cross-lane content-hash dedup records a
        # suppressed re-send under this outcome (parity with the real enum's
        # SignalOutcome.SUPPRESSED_DUPLICATE — must exist on the stub too so the
        # in-container fallback path never AttributeErrors).
        SUPPRESSED_DUPLICATE = "suppressed_duplicate"
    class _ProductTrajectoryState:
        def __init__(self, **kw): pass
    def _product_derive_phase(state): return None
    class _ProductHorizonThresholds:
        def __init__(self, **kwargs):
            for _k, _v in kwargs.items():
                setattr(self, _k, _v)
    def _product_composite_severity(base, budget, ratio):
        return float(base) + 2.0 * float(budget) + float(ratio)
    def _product_render_verify_emission(*a, **kw):
        return ""
    def _product_verify_horizon_band(*a, **kw):
        return None

# ---------------------------------------------------------------------------
# DELIVERY FACT-FILTER POLICY — SINGLE SOURCE (B1, 2026-06-13) with a FUNCTIONAL
# in-container fallback (B1-FIX, 2026-06-13, run 27462282736).
# The path-class + name-class classifiers live in groundtruth.delivery
# (path_policy + name_policy); the FACT gate + cross-language disqualifier live
# in groundtruth.pretask.curation_map. When importable (the substrate PROOF
# process), AGENT-TIME delivery applies the IDENTICAL decisions as the proof brief.
#
# CRITICAL: this hook runs INSIDE the eval TASK CONTAINER, which legitimately does
# NOT have the groundtruth package importable (same reason runtime.* falls back to
# stubs). It MUST degrade GRACEFULLY — NEVER fail-closed here. The earlier proof-mode
# `raise` (GT_DELIVERY_POLICY_IMPORT_FAILED) crashed the whole monkeypatch in-container
# and KILLED ALL per-turn evidence (regression caught by run 27462282736: no
# <gt-evidence>/<gt-contract>/<gt-scope> reached the agent). The fail-closed for a
# missing delivery policy belongs ONLY in the substrate proof process (gt_run_proof),
# where groundtruth IS importable. Here we fall back to a FUNCTIONAL inline copy (the
# pre-B1 filter) so per-turn delivery is byte-identical; the Product single source
# still governs the proof-time brief. (The substrate also injects the delivery modules
# so the import path is preferred whenever available — see gt_agent injection list.)
# ---------------------------------------------------------------------------
_DELIVERY_POLICY_AVAILABLE = False
try:
    from groundtruth.delivery.path_policy import (
        is_vendored_path as _is_vendored_path,
        is_minified_file as _is_minified_file,
        is_delivery_excluded as _is_delivery_excluded,
    )
    from groundtruth.delivery.name_policy import (
        BUILTIN_CALLABLE_NAMES as _BUILTIN_CALLABLE_NAMES,
        STDLIB_MODULES as _STDLIB_MODULES,
        is_builtin_shadow_name as _is_builtin_shadow_name,
        is_stdlib_shadow as _is_stdlib_shadow,
    )
    from groundtruth.pretask.curation_map import (
        DETERMINISTIC_RESOLUTION_METHODS as _DETERMINISTIC_METHODS,
        _is_cross_language_pair as _is_cross_language_pair,
        _nodes_have_language as _nodes_have_language,
    )
    _DELIVERY_POLICY_AVAILABLE = True
except Exception as _delivery_import_err:  # noqa: BLE001
    # GRACEFUL in-container fallback — NEVER raises (a raise kills per-turn delivery).
    # FUNCTIONAL pre-B1 filter so agent-time exclusion decisions are unchanged.
    print(f"[GT_META] delivery_policy_import_fallback=true reason={_delivery_import_err}",
          file=sys.stderr, flush=True)
    _DETERMINISTIC_METHODS = frozenset({
        "same_file", "import", "import_type", "type_flow", "verified_unique",
        "impl_method", "inherited", "unique_method", "return_type", "lsp", "lsp_verified",
    })
    _VENDOR_DIR_MARKERS_FB = (
        "/extern/", "/externals/", "/vendor/", "/vendored/", "/third_party/",
        "/thirdparty/", "/node_modules/", "/bower_components/", "/dist/",
        "/_generated/", "/generated/", "/site-packages/")
    _MINIFIED_SUFFIXES_FB = (".min.js", ".min.css", ".min.mjs", ".min.map")
    _GENERATED_FILE_MARKERS_FB = (
        "zz_generated", ".pb.go", ".pb.gw.go", "_pb2.py", "_pb2_grpc.py",
        ".generated.", "_generated.go", ".g.dart", ".freezed.dart")
    _MINIFIED_MEAN_LINE_LEN_FB = 200
    _minified_cache_fb: dict = {}

    def _is_vendored_path(fp):
        f = "/" + (fp or "").replace("\\", "/").lstrip("./").lstrip("/").lower()
        if any(m in f for m in _VENDOR_DIR_MARKERS_FB):
            return True
        base = f.rsplit("/", 1)[-1]
        if base.endswith(_MINIFIED_SUFFIXES_FB):
            return True
        return any(m in base for m in _GENERATED_FILE_MARKERS_FB)

    def _is_minified_file(repo_root, rel):
        if rel in _minified_cache_fb:
            return _minified_cache_fb[rel]
        verdict = False
        try:
            with open(os.path.join(repo_root or "", rel), encoding="utf-8", errors="ignore") as fh:
                head = fh.read(16384)
            lines = [ln for ln in head.splitlines() if ln.strip()]
            if lines:
                verdict = (sum(len(ln) for ln in lines) / len(lines)) > _MINIFIED_MEAN_LINE_LEN_FB
        except OSError:
            verdict = False
        _minified_cache_fb[rel] = verdict
        return verdict

    def _is_delivery_excluded(fp, repo_root=""):
        if _is_vendored_path(fp):
            return True
        if repo_root:
            return _is_minified_file(repo_root, _norm_fp(fp))
        return False

    _BUILTIN_CALLABLE_NAMES = frozenset({
        "join", "split", "splitlines", "strip", "lstrip", "rstrip", "lower", "upper",
        "title", "startswith", "endswith", "encode", "decode", "format", "replace",
        "find", "rfind", "get", "keys", "values", "items", "setdefault", "update",
        "popitem", "append", "extend", "pop", "insert", "remove", "index", "count",
        "sort", "reverse", "add", "discard", "clear", "copy", "rsplit", "zfill",
        "casefold", "loads", "dumps", "isinstance", "issubclass", "len", "print",
        "open", "type", "super", "getattr", "setattr", "hasattr", "delattr", "repr",
        "str", "int", "float", "bool", "list", "dict", "set", "tuple", "iter", "next",
        "range", "zip", "map", "filter", "sorted", "reversed", "enumerate", "sum",
        "min", "max", "abs", "round", "all", "any", "id", "hash", "vars", "dir",
        "callable", "exists", "push", "shift", "unshift", "slice", "splice", "concat",
        "indexof", "foreach", "tostring", "write", "read", "close", "new", "make",
        "clone", "unwrap", "expect",
    })

    def _is_builtin_shadow_name(name):
        n = (name or "").strip()
        if not n:
            return False
        if n.startswith("__") and n.endswith("__") and len(n) > 4:
            return True
        return n.lower() in _BUILTIN_CALLABLE_NAMES

    _STDLIB_MODULES = frozenset({
        "os", "sys", "re", "io", "json", "math", "time", "copy", "glob", "uuid",
        "shutil", "random", "typing", "logging", "pathlib", "datetime", "string",
        "decimal", "inspect", "warnings", "argparse", "textwrap", "itertools",
        "functools", "operator", "collections", "subprocess", "contextlib",
    })
    _STDLIB_SHADOW_RE_FB = re.compile(r"([A-Za-z_][\w.]*)\.([A-Za-z_]\w*)\s*\(")

    def _is_stdlib_shadow(code, target_name):
        if not code or not target_name:
            return False
        for m in _STDLIB_SHADOW_RE_FB.finditer(code):
            if m.group(2) == target_name and m.group(1).split(".")[0] in _STDLIB_MODULES:
                return True
        return False

    _LANG_FAMILIES_FB = {
        "javascript": "jslike", "typescript": "jslike", "jsx": "jslike", "tsx": "jslike",
        "vue": "jslike", "svelte": "jslike", "java": "jvm", "kotlin": "jvm",
        "scala": "jvm", "groovy": "jvm", "clojure": "jvm", "c": "cfamily",
        "cpp": "cfamily", "c++": "cfamily", "objc": "cfamily", "objcpp": "cfamily",
        "objective-c": "cfamily", "swift": "cfamily", "python": "python", "go": "go",
        "rust": "rust", "ruby": "ruby", "php": "php", "csharp": "csharp", "c#": "csharp",
        "lua": "lua", "elixir": "elixir", "erlang": "erlang", "haskell": "haskell",
        "dart": "dart", "r": "r", "julia": "julia", "perl": "perl", "bash": "shell",
        "shell": "shell", "sh": "shell", "zig": "zig", "ocaml": "ocaml",
    }

    def _is_cross_language_pair(lang_a, lang_b):
        fa = _LANG_FAMILIES_FB.get(str(lang_a or "").strip().lower())
        fb = _LANG_FAMILIES_FB.get(str(lang_b or "").strip().lower())
        return fa is not None and fb is not None and fa != fb

    def _nodes_have_language(con):
        try:
            cols = {r[1] for r in con.execute("PRAGMA table_info(nodes)").fetchall()}
        except Exception:  # noqa: BLE001
            return False
        return "language" in cols

# Strict flag parse (bug #6 parity with gt_agent / every other GT flag):
# bool(env) made GT_BASELINE=0 enable the baseline arm.
_GT_BASELINE = os.environ.get("GT_BASELINE") == "1"
_ROOT_FILE = os.environ.get("GT_ROOT_FILE", "/opt/gt/gt_root.txt")
_HOOK_TIMEOUT = int(os.environ.get("GT_HOOK_TIMEOUT", "30"))

# FACT gate (_DETERMINISTIC_METHODS) + stdlib-module set (_STDLIB_MODULES) are
# imported from the Product single source at the top of this file (B1) — the
# previous inline copies were removed so a policy change in curation_map /
# groundtruth.delivery reaches agent-time delivery, not just the proof brief.

# DELIVERY FACT-FILTER (path-class + name-class) is SINGLE-SOURCED in
# groundtruth.delivery (path_policy + name_policy) and imported at the top of
# this file (B1, 2026-06-13). The previous inline path markers + _is_vendored_path
# / _is_minified_file / _is_delivery_excluded / _BUILTIN_CALLABLE_NAMES /
# _is_builtin_shadow_name / _is_stdlib_shadow were removed so a policy change
# reaches BOTH the proof brief AND agent-time delivery. Mirrors the localizer's
# `_is_generated` W_GEN demote (ranking) on the delivery surface, and resolver.go's
# T2 builtin drop (index-time, QUALIFIED-only) on the bare-call residual.
# The OBLIGATION-CREDIT / EDIT-DETECTION helpers below (_SOURCE_EXTS,
# _SCRATCH_DIR_MARKERS, _has_source_ext, _is_repo_source_path) are a DIFFERENT
# concern (which writes count as edits) and stay local.
_SOURCE_EXTS: tuple[str, ...] = (
    ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java",
    ".kt", ".c", ".h", ".cc", ".cpp", ".hpp", ".rb", ".php", ".cs",
    ".swift", ".scala",
)
_SCRATCH_DIR_MARKERS: tuple[str, ...] = (
    "/tmp/", "/temp/", "/scratch/", "/.tmp/", "/.cache/", "/logs/",
)


def _has_source_ext(fp: str) -> bool:
    """EDIT-DETECTION gate: any source-extension file, INCLUDING scratch/temp.
    The agent often stages edits in /tmp/X_new.ts then copies to src — the sensor
    and governor (source_edit_count, failure_persisted, scaffold_trap) must count
    these as edit ACTIONS (parity with the recorded corpus). Scratch exclusion
    belongs ONLY in obligation CREDIT, not in edit detection (the D4 conflation
    that broke byte-parity + the stage-0 sensor: 77/182 source writes missed)."""
    low = (fp or "").replace("\\", "/").lower()
    return low.endswith(tuple(e.lower() for e in _SRC_EXT))


def _is_repo_source_path(fp: str) -> bool:
    """OBLIGATION-CREDIT gate: a REAL repo source file, not scratch/temp/vendor/
    generated. Used ONLY where an obligation is credited as 'edited' — writing a
    /tmp/ scratch file with obligation keywords must NOT falsely credit it."""
    f = "/" + (fp or "").replace("\\", "/").lstrip("./").lstrip("/")
    low = f.lower()
    if any(m in low for m in _SCRATCH_DIR_MARKERS):
        return False
    if _is_vendored_path(fp):
        return False
    return low.endswith(_SOURCE_EXTS)


# per-file-once dedup, keyed (kind, relpath)
_seen: set[tuple[str, str]] = set()
# Layer-A consensus fires once per run (first source-view), like the OH wrapper.
_consensus_fired = False
# Consensus PROGRESSIVE (Layer-B) + OVERRIDE-on-divergence (OH parity): remember the
# scope set so subsequent in-scope views get "also in scope" reinforcement, and if the
# agent wanders off-scope for a while, re-anchor consensus on where it actually is.
_consensus_scope: set[str] = set()
_offscope_views = 0
# L5 (trajectory governor, minimal port): track actions/edits/loops so a stuck
# trajectory gets ONE nudge instead of burning to maxiter unguarded (the OH governor's
# core job; the full L5Governor cannot run here — execute() has no max_iter — so we
# port the two highest-value heuristics: scaffold-trap + repeated-command loop).
_action_count = 0
_source_edit_count = 0
_cmd_history: list[str] = []
_l5_fired = False
# Additional L5 governor behaviours (each fires once): unsafe-finish (submit with no
# source edit) and repeated-test-failure (the same test fails again after an edit —
# OH's hook_same_failure_persisted / hypothesis-falsified).
_l5_finish_fired = False
_l5_failure_fired = False
_test_fail_history: list[str] = []
# no_test_evidence governor state (2026-06-10, boa [243]-[333]): blind test
# runs counted; any observed pass/fail result latches _test_evidence_seen.
_l5_notest_fired = False
_blind_test_runs = 0
_test_evidence_seen = False
# L6 (incremental freshness, minimal port): the gt_hook understand AST cache
# (/tmp/gt_index.json) has no mtime invalidation, and graph.db is frozen at base commit.
# After a source EDIT we invalidate the cache + best-effort single-file reindex so the
# next understand/consensus/verify sees the agent's NEW code, not base-commit.
_GT_INDEX_CACHE = os.environ.get("GT_INDEX_CACHE", "/tmp/gt_index.json")
# COMPLETENESS / co-change fires once on the first source edit (the multi-file scope
# signal DeepSWE entirely lacked — OH ships it from the cochanges table).
_cochange_fired = False
# diagnostic: one-time marker so trajectory analysis can tell
# "patch never loaded" from "loaded but no evidence". Printed to STDERR
# (harness log) — never appended to agent-visible output (2026-06-10 fix:
# it leaked into the agent's context at MSG 3 on 10/10 PATH B tasks).
_marker_sent = False

# Source-file extensions GT indexes (matches gt-index language set).
_SRC_EXT = (
    ".py", ".go", ".ts", ".tsx", ".js", ".jsx", ".rs", ".java", ".rb",
    ".c", ".cc", ".cpp", ".h", ".hpp", ".cs", ".php", ".kt", ".scala", ".swift",
)

# Edit-shaped commands: sed -i, tee, patch, apply_patch, redirects, heredocs.
_EDIT_RE = re.compile(
    r"(^|[|&;]\s*)(sed\s+-i|tee\b|patch\b|apply_patch\b)"
    r"|>>?\s*\S+"
    r"|<<\s*'?[A-Z_]+'?\s*>\s*\S+",
)
# Read-shaped commands: cat, grep, head, tail, etc.
_VIEW_RE = re.compile(
    r"(^|[|&;]\s*)(cat|grep|rg|head|tail|less|more|view|nl|awk|sed\s+-n)\b",
)


def _root() -> str:
    try:
        return (open(_ROOT_FILE).read().strip()) or "/"
    except Exception:  # noqa: BLE001
        return "/"


# Python/Node in-place file WRITE (the agent's DOMINANT JS edit shape: a python heredoc
# `python3 << EOF ... open('file','w') ... EOF`). The filename lives INSIDE the heredoc
# body, so a redirect/heredoc-strip scan misses it entirely (the bug the JS re-audit found:
# 24/36 real gold-file edits were uncaught). Match the open()/writeFileSync target directly.
_PY_WRITE_RE = re.compile(r"""open\(\s*['"]([^'"]+)['"]\s*,\s*['"][wa]""")
_JS_WRITE_RE = re.compile(r"""(?:writeFileSync|appendFileSync|writeFile)\(\s*['"]([^'"]+)['"]""")
# sed -i / tee / patch / apply_patch, at line start or after a shell separator.
_EDIT_KW_RE = re.compile(r"(?:^|[|&;]\s*)(sed\s+-i|tee\b|patch\b|apply_patch\b)")

# ---------------------------------------------------------------------------
# PATCH-APPLICATION edit family (apply_patch / git apply / patch -pN). These are
# the universal agent edit channels (apply_patch = OpenAI/Codex format; git
# apply / patch -pN = POSIX) where the TARGET FILE is NOT a shell redirect/arg —
# it lives inside the DIFF PAYLOAD (`*** Update File: <path>` for apply_patch,
# `+++ b/<path>` for unified diffs). The payload is either INLINE in a heredoc
# body or in a SEPARATE staged file the command reads via `< file` (or as the
# trailing operand of `git apply`/`patch`). Recognizing these is a general
# property of agent edit channels — no task IDs / gold / repo logic. Correct-or-
# quiet: if the payload/target cannot be parsed, degrade to today's behaviour
# (return None — never fabricate a target).
_PATCH_APPLY_RE = re.compile(
    r"(?:^|[|&;]\s*)(apply_patch\b|git\s+apply\b|patch\b)",
)
# A patch command carrying a no-op/inspection flag (git apply --check/--stat/
# --numstat/--summary; patch --dry-run) WRITES NOTHING -> it must NOT classify as
# an edit. Correct-or-quiet: never fabricate an edit event for a command that does
# not modify a file (the LIPI-found dry-run false-positive in a7a4be87).
_PATCH_NOOP_RE = re.compile(r"(?:^|\s)--(?:check|stat|numstat|summary|dry-run)\b")
# apply_patch payload markers (OpenAI/Codex format).
_APPLY_PATCH_FILE_RE = re.compile(
    r"^\s*\*\*\*\s+(?:Update|Add|Delete)\s+File:\s*(.+?)\s*$", re.MULTILINE,
)
# unified-diff new-side / old-side path headers. The RAW path (incl. the
# `b/`/`a/` prefix git emits) is captured; _strip_diff_path applies the `-p<n>`
# strip count (default p1 removes the one leading `b/`/`a/` segment).
_DIFF_PLUS_RE = re.compile(r"^\+\+\+\s+(\S+)", re.MULTILINE)
_DIFF_MINUS_RE = re.compile(r"^---\s+(\S+)", re.MULTILINE)
# `-p<n>` strip count for git apply / patch (default 1).
_P_STRIP_RE = re.compile(r"(?:^|\s)-p(\d+)\b")
# `< file` input redirection (the staged-diff form). Excludes `<<` heredocs.
_IN_REDIR_RE = re.compile(r"(?<!<)<(?!<)\s*([^\s'\"<>|&;]+)")
# read-back of a staged diff file is size-capped (correct-or-quiet on huge/blobs).
_MAX_STAGED_DIFF_BYTES = 4_000_000


def _is_patch_apply(cmd: str) -> bool:
    """True when ``cmd`` is a patch-application channel (apply_patch / git apply /
    patch -pN). `patch` alone is ambiguous (the legacy `patch`-keyword path
    already routes through _EDIT_KW_RE), but it is the same edit family."""
    if not cmd:
        return False
    first = cmd.split("\n", 1)[0]
    if not _PATCH_APPLY_RE.search(first):
        return False
    # No-op/inspection flags write nothing -> not an edit (correct-or-quiet).
    return not _PATCH_NOOP_RE.search(first)


def _strip_diff_path(raw: str, strip: int = 1) -> str:
    """Strip the leading `b/`/`a/` and ``strip``-1 extra path components from a
    unified-diff header path (the `-p<n>` semantics). Default p1 removes one
    leading component (`b/<rel>` -> `<rel>`)."""
    p = (raw or "").strip().replace("\\", "/")
    if p in ("/dev/null", "dev/null"):
        return ""
    parts = [seg for seg in p.split("/")]
    # -p<n> strips n leading path segments (git's `a/`,`b/` is one such segment).
    if strip > 0 and len(parts) > strip:
        parts = parts[strip:]
    return "/".join(parts)


def _read_staged_diff(cmd: str) -> str:
    """Read the diff payload for a `< file` / trailing-operand patch command from
    the staged file on disk. Correct-or-quiet: returns "" if no such operand, the
    file is absent/unreadable, or it exceeds the size cap (never raises)."""
    if not cmd:
        return ""
    first = cmd.split("\n", 1)[0]
    path = None
    m = _IN_REDIR_RE.search(first)
    if m:
        path = m.group(1).strip("\"'`()")
    else:
        # git apply <file> / patch ... <file>: the trailing non-flag operand.
        toks = [t for t in re.split(r"\s+", first.strip()) if t]
        for t in reversed(toks):
            if t and not t.startswith("-") and t not in (
                "git", "apply", "patch", "apply_patch", "--3way", "--3way=",
            ):
                # heuristic operand: looks like a path (has a slash or an ext).
                if "/" in t or "." in t:
                    path = t.strip("\"'`()")
                    break
    if not path:
        return ""
    try:
        if not os.path.isfile(path):
            return ""
        if os.path.getsize(path) > _MAX_STAGED_DIFF_BYTES:
            return ""
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception:  # noqa: BLE001 -- unreadable staged diff -> degrade quiet
        return ""


def _patch_payload(cmd: str) -> str:
    """The diff/patch PAYLOAD text for a patch-apply command, from wherever it
    lives: the heredoc body (`<<EOF ... EOF`) inline in ``cmd``, or the staged
    file read back for the `< file` / trailing-operand form. "" when neither
    yields content (correct-or-quiet)."""
    if not cmd:
        return ""
    if "<<" in cmd:
        body = _edit_body(cmd)
        if body.strip():
            return body
    return _read_staged_diff(cmd)


def _patch_apply_target(cmd: str) -> str | None:
    """The repo file a patch-apply command WRITES, parsed from the diff payload.
    apply_patch -> `*** Update/Add/Delete File: <path>`; unified diff -> `+++ b/`
    (falls back to `--- a/` when the new side is /dev/null, a deletion). Honours
    the `-p<n>` strip count for git apply / patch. Prefers the FIRST repo-source
    target. None when the payload is empty/unparseable (correct-or-quiet)."""
    payload = _patch_payload(cmd)
    if not payload:
        return None
    # 1. apply_patch markers (no path stripping — the marker path is repo-rel).
    ap = [m.group(1).strip().replace("\\", "/")
          for m in _APPLY_PATCH_FILE_RE.finditer(payload)]
    cands: list[str] = []
    for p in ap:
        if p and p != "/dev/null":
            cands.append(p)
    # 2. unified-diff `+++ b/<path>` (or `--- a/<path>` on a deletion).
    if not cands:
        sm = _P_STRIP_RE.search(cmd.split("\n", 1)[0])
        strip = int(sm.group(1)) if sm else 1
        for m in _DIFF_PLUS_RE.finditer(payload):
            tgt = _strip_diff_path(m.group(1), strip)
            if tgt:
                cands.append(tgt)
        if not cands:
            for m in _DIFF_MINUS_RE.finditer(payload):
                tgt = _strip_diff_path(m.group(1), strip)
                if tgt:
                    cands.append(tgt)
    if not cands:
        return None
    # Prefer the first repo-source target; else the first parsed target.
    for c in cands:
        if _has_source_ext(c):
            return c
    return cands[0]


def _src_tokens(text: str) -> list[str]:
    out: list[str] = []
    for tok in re.split(r"\s+", text or ""):
        t = tok.strip("\"'`()<>;|&")
        # EDIT DETECTION: broad ext check (incl. /tmp/ staging) — see _has_source_ext.
        if _has_source_ext(t) and "*" not in t and "$" not in t:
            out.append(t)
    return out


def _edit_target(cmd: str) -> str | None:
    """The SOURCE file this command WRITES, or None. Covers every shape the real agent uses:
      - redirect to a source file (`cat > x.js`, `... >> x.js`);
      - sed -i / tee / apply_patch on a source arg (incl. multi-line sed-append);
      - python/node in-place write (`open('x.js','w'|'a')`, `writeFileSync('x.js')`) — incl.
        inside a heredoc body.
    A redirect to a NON-source path (`cat x.js > /tmp/x.bak`, `git diff x.js > /tmp/p.txt`)
    is NOT a source write — that falls to _view_target (read) or to nothing."""
    if not cmd:
        return None
    # 0. PATCH-APPLICATION family (apply_patch / git apply / patch -pN): the
    #    target file lives in the DIFF PAYLOAD, not a shell redirect/arg. Parse
    #    it from the heredoc body or the staged `< file`. Correct-or-quiet: a
    #    None here falls through to the legacy branches (no regression).
    if _is_patch_apply(cmd):
        pt = _patch_apply_target(cmd)
        if pt:
            return pt
    nohd = cmd.split("<<", 1)[0] if "<<" in cmd else cmd  # shell scans exclude heredoc body
    # 1. redirect whose TARGET is a source file (broad — incl. /tmp/ staging)
    for mm in re.finditer(r">>?\s*([^\s'\"<>|&;]+)", nohd):
        t = mm.group(1).strip("\"'`()")
        if _has_source_ext(t) and "*" not in t and "$" not in t:
            return t
    # 2. sed -i / tee / apply_patch -> the source-file argument (last source token)
    first = cmd.split("\n", 1)[0]
    if _EDIT_KW_RE.search(first.lstrip()) or _EDIT_KW_RE.search(first):
        toks = _src_tokens(nohd)
        if toks:
            return toks[-1]
    # 3. python/node in-place write (scans the FULL cmd incl. heredoc body)
    for rx in (_PY_WRITE_RE, _JS_WRITE_RE):
        m = rx.search(cmd)
        if m and _has_source_ext(m.group(1)) and "*" not in m.group(1):
            return m.group(1)
    return None


# RC5 Signal-1 (CONTENT lexical) + Signal-3 (line range) extractors. The edit
# CONTENT the agent wrote lives in the patch/heredoc/write BODY — NOT the command
# verb. apply_patch reads the diff from a heredoc/file; a sed rewrites a value not
# a name; a python write spells the symbol inside the open()...write() string. So
# the obligation token must be sought in the BODY, and the touched LINE RANGE is
# read from diff hunk headers / sed addresses (the edit-site precision gate).
_HUNK_RE = re.compile(r"@@\s*-\d+(?:,\d+)?\s+\+(\d+)(?:,(\d+))?\s*@@")
_SED_ADDR_RE = re.compile(r"(?:^|[|&;]\s*)sed\s+-i[^\s]*\s+(?:-e\s+)?['\"]?(\d+)(?:,(\d+))?")


def _edit_body(cmd: str) -> str:
    """The CONTENT region of an edit command — the heredoc body / apply_patch
    payload / sed replacement / python|node write string — as raw text. This is
    where the symbol the agent actually wrote appears; the command verb often
    does not name it (`apply_patch < /tmp/p`). Conservative: returns the whole
    command when no body delimiter is found (a redirect/sed inline replacement
    already carries its content in the command text), so we never lose Signal 1."""
    if not cmd:
        return ""
    if "<<" in cmd:
        # heredoc: everything after the FIRST << delimiter line is the body.
        after = cmd.split("<<", 1)[1]
        nl = after.find("\n")
        return after[nl + 1:] if nl != -1 else after
    # PATCH-APPLY `< file` form (`apply_patch < /tmp/p.diff`, `git apply f`,
    # `patch -p1 < f`): the CONTENT is the STAGED diff file on disk, NOT the
    # command string. Read it back so Signal 1 (body tokens) + Signal 3 (hunk
    # ranges) see the symbol/`@@` the command line never carries. Correct-or-
    # quiet: empty staged read -> fall through to the command text (unchanged).
    if _is_patch_apply(cmd):
        staged = _read_staged_diff(cmd)
        if staged:
            return staged
    return cmd


def _edit_body_tokens(cmd: str) -> set[str]:
    """Identifier tokens (>=3 chars) of the edit CONTENT body (Signal 1).
    Broadened from the old command-verb tokenization so a symbol introduced in
    the patch/heredoc/write body is captured even when the command line lacks it."""
    body = _edit_body(cmd)
    return {t for t in _BLOCK_TOKEN_RE.findall(body) if len(t) >= 3}


def _edited_line_ranges(cmd: str) -> list:
    """The (start,end) 1-based line ranges this command writes (Signal 3), from
    unified-diff hunk headers (`@@ -a,b +c,d @@` -> new side c..c+d-1) or a sed
    line address (`sed -i '40,55s/.../.../'`). Empty when no line data is
    derivable -> Signal 3 degrades to 2-of-2 (correct-or-quiet, never invents a
    range)."""
    if not cmd:
        return []
    ranges: list[tuple] = []
    # Scan the diff PAYLOAD for `@@ ... +c,d @@` hunks. For an inline heredoc the
    # payload is a substring of cmd; for the `< file` form _edit_body reads the
    # STAGED diff file back, so the `< /tmp/p.diff` case now yields its hunks too.
    payload = _edit_body(cmd)
    for m in _HUNK_RE.finditer(payload):
        start = int(m.group(1))
        count = int(m.group(2)) if m.group(2) else 1
        if start > 0 and count > 0:
            ranges.append((start, start + count - 1))
    if not ranges:
        m = _SED_ADDR_RE.search(cmd)
        if m:
            lo = int(m.group(1))
            hi = int(m.group(2)) if m.group(2) else lo
            if lo > 0:
                ranges.append((lo, max(hi, lo)))
    return ranges


def _view_target(cmd: str) -> str | None:
    """A SOURCE file being READ (cat/grep/head/...) without being written."""
    head = (cmd or "").split("\n", 1)[0].lstrip()
    if not _VIEW_RE.search(head):
        return None
    toks = _src_tokens(head)
    return toks[0] if toks else None


def _first_src_file(cmd: str) -> str | None:  # kept for compatibility
    return _edit_target(cmd) or _view_target(cmd)


def _classify(cmd: str) -> tuple[str | None, str | None]:
    """Map a bash command to (kind, file). A WRITE to a source file (_edit_target) takes
    priority over a READ (_view_target). Verified by replaying the FULL real agent command
    stream offline — sed, heredoc cat, multi-line sed, python/node open-write, redirects."""
    if not cmd:
        return None, None
    et = _edit_target(cmd)
    if et:
        return "post_edit", et
    vt = _view_target(cmd)
    if vt:
        return "post_view", vt
    return None, None


# ---------------------------------------------------------------------------
# STRUCTURED EDITOR CHANNEL (F3 — structured-action-first edit detection).
#
# The bash-string classifier above sees ONLY action["command"] as a SHELL
# string. But the agent's DOMINANT edit surface on OH/CodeAct/Anthropic/Codex
# harnesses is a STRUCTURED editor action — a dict whose `command` is an EDITOR
# VERB (create / str_replace / insert / write / append), NOT a shell command,
# and whose target + content live in explicit fields (path/file_path + file_text
# / new_str / old_str / insert_line). On that channel action.get("command") is
# e.g. "str_replace" — it matches NO shell verb, so _edit_target/_view_target
# return None and the edit is 100% INVISIBLE: every post-edit consumer (the
# F6 edit-credit, the governor source_edit_count, L3b contract, the verify
# horizon) goes dark, and a structured READ ("view") would misclassify.
#
# THE GENERAL FIX (not a verb-whitelist extension): read the STRUCTURED args
# FIRST. If the action carries an explicit editor target (a path field) AND a
# write verb / write content, classify it as post_edit with that path and lift
# the body/lines from the content fields for the RC5 signals. This covers the
# agent's real edit surface on ANY structured harness — no task IDs / gold /
# repo logic, no shell-verb growth. Correct-or-quiet: a structured action with
# no path, or a structured READ (view), never fabricates a post_edit.
# ---------------------------------------------------------------------------
# Editor verbs that WRITE a file (OH str_replace_editor: create/str_replace/
# insert; Anthropic text_editor_*: str_replace/insert/create; Codex/generic:
# write/append/edit/overwrite). `view` is a READ (handled below).
_STRUCT_WRITE_VERBS = frozenset({
    "create", "str_replace", "insert", "write", "append",
    "edit", "overwrite", "str_replace_based_edit", "modify",
})
# Editor verbs that READ a file (no write) — a structured view of a source file.
_STRUCT_READ_VERBS = frozenset({"view", "read", "open", "cat"})
# The fields a structured editor action uses to name its TARGET file.
_STRUCT_PATH_KEYS = ("path", "file_path", "file", "filename", "target", "filepath")
# The fields that carry the WRITTEN CONTENT (the body the agent authored).
_STRUCT_BODY_KEYS = ("file_text", "new_str", "new_string", "content", "text",
                     "code", "insert_text", "old_str", "old_string")


def _struct_field(action, keys) -> str | None:
    """First non-empty string value among ``keys`` in the action dict (the
    structured editor names its target/content with one of several keys across
    harnesses). None when absent — correct-or-quiet."""
    if not isinstance(action, dict):
        return None
    for k in keys:
        v = action.get(k)
        if isinstance(v, str) and v.strip():
            return v
    return None


def _struct_body(action) -> str:
    """The CONTENT the agent WROTE in a structured edit, joined from whichever
    body fields are present (file_text for a create; new_str [+ old_str] for a
    str_replace; insert_text/text for an insert). The symbol the agent authored
    lives here, NOT in the editor verb — this feeds RC5 Signal-1 (body tokens)."""
    if not isinstance(action, dict):
        return ""
    parts: list[str] = []
    for k in _STRUCT_BODY_KEYS:
        v = action.get(k)
        if isinstance(v, str) and v.strip():
            parts.append(v)
    return "\n".join(parts)


def _struct_line_ranges(action) -> list:
    """RC5 Signal-3 (touched line range) for a structured edit, when the action
    carries explicit line data — an `insert_line` (insert-after index) or an
    explicit start/end. str_replace/create carry no line numbers -> empty
    (Signal 3 degrades to 2-of-2, correct-or-quiet, never invents a range)."""
    if not isinstance(action, dict):
        return []
    ranges: list[tuple] = []
    il = action.get("insert_line")
    try:
        if il is not None:
            n = int(il)
            if n >= 0:
                # an insert lands the new content AT line n+1 (1-based).
                ranges.append((n + 1, n + 1))
    except (TypeError, ValueError):
        pass
    sl, el = action.get("start_line"), action.get("end_line")
    try:
        if sl is not None:
            lo = int(sl)
            hi = int(el) if el is not None else lo
            if lo > 0:
                ranges.append((lo, max(hi, lo)))
    except (TypeError, ValueError):
        pass
    return ranges


def _structured_edit(action):
    """Read the STRUCTURED editor channel FIRST. Returns
    ``(kind, path, body, line_ranges)`` when ``action`` is a structured editor
    action (an editor VERB in ``command`` + an explicit path field), else None
    so the caller falls back to the bash command-string parse.

    A structured WRITE verb (or any structured action that carries both a path
    and written content) -> ("post_edit", path, body, lines). A structured READ
    verb (view) on a path -> ("post_view", path, "", []). Correct-or-quiet: no
    path field, or a non-source path with no write content, -> None."""
    if not isinstance(action, dict):
        return None
    path = _struct_field(action, _STRUCT_PATH_KEYS)
    if not path:
        return None
    path = path.strip()
    verb = action.get("command")
    verb = verb.strip().lower() if isinstance(verb, str) else ""
    body = _struct_body(action)
    # A structured READ verb on a path is a view (no write content treated as edit).
    if verb in _STRUCT_READ_VERBS and not (
            action.get("file_text") or action.get("new_str")
            or action.get("new_string")):
        if _has_source_ext(path):
            return ("post_view", path, "", [])
        return None
    # A structured WRITE: an explicit write verb, OR a path that carries written
    # content (file_text/new_str/...). The verb being a NON-shell editor token
    # (not a real bash command) is what makes the command-string parser blind to
    # it — reading the structured args is the general fix.
    is_write = verb in _STRUCT_WRITE_VERBS or bool(body)
    if is_write and _has_source_ext(path):
        return ("post_edit", path, body, _struct_line_ranges(action))
    return None


def _action_command(action) -> str:
    """The bash command STRING for an action — action['command'] when that is a
    real shell command (the bash channel), else "". A STRUCTURED editor action
    carries an editor VERB (create/str_replace/...) in `command`, which is NOT a
    shell command; the structured channel is handled by ``_structured_edit`` and
    must NOT be re-parsed as bash, so return "" for it (the verb token would
    otherwise leak into loop signatures / token sets)."""
    if not isinstance(action, dict):
        return str(action) if action is not None else ""
    cmd = action.get("command", "")
    if not isinstance(cmd, str):
        return ""
    # If the action is structured-edit-shaped (editor verb + path field), the
    # `command` is an editor verb, not bash -> not a shell command.
    if _structured_edit(action) is not None and _struct_field(action, _STRUCT_PATH_KEYS):
        return ""
    return cmd


def _classify_action(action) -> tuple[str | None, str | None]:
    """STRUCTURED-FIRST classification: read the structured editor channel
    (_structured_edit) BEFORE the bash command-string parse. Falls back to
    _classify on the shell command for bash edits/views. This is the general
    edit-surface detector for ANY harness (structured editor OR bash)."""
    se = _structured_edit(action)
    if se is not None:
        return se[0], se[1]
    return _classify(_action_command(action))


def _effective_cmd(action) -> str:
    """The bash-equivalent command STRING the downstream string extractors
    (_classify / _edit_body_tokens / _edited_line_ranges / _evidence / the loop
    signature) consume for ``action``.

    For a BASH action this is just action['command']. For a STRUCTURED editor
    action — invisible to the shell parsers — synthesize a parser-faithful
    command that carries the SAME target + body (+ line ranges when known), so
    every existing cmd-consumer (target classification, RC5 Signal-1 body
    tokens, Signal-3 line ranges, evidence) works UNCHANGED on the structured
    channel. A structured WRITE with line data -> an apply_patch unified-diff
    heredoc (target via `+++ b/`, body via `+`, lines via `@@`); a structured
    WRITE without line data -> a `cat > <path>` heredoc (target via redirect,
    body via heredoc); a structured READ -> `cat <path>` (view). Correct-or-
    quiet: a non-structured / unparseable action returns its raw command."""
    se = _structured_edit(action)
    if se is None:
        return _action_command(action)
    kind, path, body, lranges = se
    safe_path = path.replace("\n", " ").strip()
    if kind == "post_view":
        return f"cat {safe_path}"
    # post_edit: prefer a unified-diff heredoc when we have explicit line ranges
    # (so Signal-3 flows through _edited_line_ranges' `@@` reader); else a
    # `cat >` heredoc (target via redirect, body tokens via the heredoc reader).
    if lranges:
        lo, hi = lranges[0]
        count = max(hi - lo + 1, 1)
        hunk_lines = "\n".join("+" + ln for ln in (body or "").split("\n"))
        return (
            f"apply_patch <<'GT_STRUCT_EOF'\n"
            f"--- a/{safe_path}\n"
            f"+++ b/{safe_path}\n"
            f"@@ -{lo},{count} +{lo},{count} @@\n"
            f"{hunk_lines}\n"
            f"GT_STRUCT_EOF"
        )
    return f"cat > {safe_path} <<'GT_STRUCT_EOF'\n{body}\nGT_STRUCT_EOF"


# ---------------------------------------------------------------------------
# BEHAVIORAL SIGNAL PRIMITIVES (delivery-engine Stage 1, 2026-06-11).
#
# The validated trajectory-structure signals from RESEARCH_AGENT_BEHAVIORAL_
# SIGNALS.md (Category 5 feature table), computed deterministically from the
# agent's own (command, observation) stream — no LLM, no gold, no task IDs:
#   - loop_ratio / new_state_rate  — TIDE (arXiv 2602.02196): degenerate
#     repetition forms recursive cycles revisiting identical state sequences
#     with no new nodes; adaptive cycles EXPAND the graph. Also the "stale
#     score" complement (arXiv 2604.13151).
#   - edit churn per target        — TRAJEVAL (arXiv 2603.24631) "Coherence
#     Collapse": 60-69% of failures reach the right code then thrash it; the
#     `Pr` re-patch symbol of arXiv 2604.02547.
#   - edit/test coverage ratios    — SWE-Next (arXiv 2603.20691): 97.6% of
#     successes ran >=1 test; validation share rho=+0.50 (arXiv 2604.02547).
#
# These live HERE (not in the sensor) because gt_oracle_sense.py binds its
# primitives FROM this module ("the sensor and the live patch can never
# disagree") — the same one-direction reuse that already covers _classify and
# the test regexes. One formula, two consumers: the LEGITIMACY deduction-1
# (live/replay twin drift) cannot reopen on these signals.
# ---------------------------------------------------------------------------
_STATE_WINDOW = 12  # the live loop window (gt_gt §12) — the TIDE window K


def _obs_collapse(obs: str, n: int = 400) -> str:
    """Collapsed-observation prefix — the no-new-state proof half of the live
    loop signature (gt_mini_patch._l5_nudge / gt_oracle._loop_signature)."""
    return " ".join((obs or "").split())[:n]


def _behavior_state_key(cmd: str, raw_obs: str) -> str:
    """TIDE state-graph node identity for ONE action: (action TYPE, TARGET
    file) when the command classifies as an edit/view — an action is 'new'
    iff its target file+type hasn't appeared in the recent window — else the
    command head token + a collapsed-observation hash (a non-file action is
    'new' when it produces new output)."""
    kind, fpath = _classify(cmd)
    if kind and fpath:
        return f"{kind}\x00{fpath}"
    import hashlib as _h
    head = (cmd or "").strip().split(None, 1)[0] if (cmd or "").strip() else ""
    oh = _h.sha256(_obs_collapse(raw_obs).encode("utf-8", "replace")).hexdigest()[:8]
    return f"cmd\x00{head}\x00{oh}"


def compute_loop_ratio(signatures) -> float:
    """TIDE Loop Ratio (deterministic form): the fraction of actions whose
    full (command, collapsed-observation) signature occurs >=2 times in the
    trajectory — actions inside recurring identical-state cycles / length.
    A no-new-state revisit contributes; a same-command-NEW-observation
    iteration does not (the 2026-06-10 '13453 false fire' discipline: same
    command + different output is iteration, not a loop)."""
    sigs = list(signatures or ())
    if not sigs:
        return 0.0
    counts: dict[str, int] = {}
    for s in sigs:
        counts[s] = counts.get(s, 0) + 1
    inside = sum(c for c in counts.values() if c >= 2)
    return inside / len(sigs)


def compute_new_state_rate(state_keys, window: int = _STATE_WINDOW) -> float:
    """Fraction of the last `window` actions whose state key did NOT appear in
    the `window` actions preceding it (TIDE new-node production rate; the
    arXiv 2604.13151 'stale score' complement). Empty -> 1.0 (everything is
    new); a healthy exploring agent stays near 1.0, a stale-binary loop
    (fd: 5x identical command+output) collapses toward 0."""
    keys = list(state_keys or ())
    if not keys:
        return 1.0
    tail = keys[-window:]
    base = len(keys) - len(tail)
    new = 0
    for i, k in enumerate(tail):
        gi = base + i
        if k not in keys[max(0, gi - window):gi]:
            new += 1
    return new / len(tail)


# Language keywords / structural noise tokens: never coverage EVIDENCE (an
# edit command and a test output sharing `return` proves nothing). Structural
# set in the _BUILTIN_CALLABLE_NAMES tradition — not a tuned threshold.
_SIG_STOPWORDS: frozenset[str] = frozenset({
    "def", "return", "class", "self", "this", "func", "const", "let", "var",
    "pub", "impl", "import", "from", "for", "while", "else", "elif", "none",
    "true", "false", "null", "async", "await", "print", "and", "not", "with",
    "pass", "raise", "new", "mut", "use", "type", "interface", "export",
    "function", "static", "void", "int", "str", "string", "bool", "float",
    "public", "private", "protected", "match", "case", "break", "continue",
    "struct", "enum", "trait", "where", "package", "module", "require",
})


def _coverage_idents(tokens) -> set[str]:
    """Identifier-shaped tokens usable as coverage evidence (>=4 chars, not a
    language keyword) — the precision guard on the token-intersection ratios."""
    return {t for t in (tokens or ()) if len(t) >= 4
            and t.lower() not in _SIG_STOPWORDS}


def symbol_tested(sym: str, tested_tokens) -> bool:
    """plan §5.2 'tested?' at SYMBOL grain: exact token intersection, plus
    substring containment for compound names (`test_capture_snapshot` IS
    observed evidence for `capture_snapshot`). Mirrors the obligation-level
    gt_oracle._obligation_tested; looser matching is the SAFE direction —
    'tested' SUPPRESSES an emission (correct-or-quiet)."""
    if not sym:
        return False
    tested = tested_tokens or ()
    if sym in tested:
        return True
    compound = ("_" in sym or "." in sym or any(c.isdigit() for c in sym)
                or any(c.isupper() for c in sym[1:]))
    if not compound:
        return False
    return any(sym in t for t in tested)


def composite_severity(base, budget_fraction, unmet_ratio) -> float:
    """severity = base + (budget_fraction × 2) + (unmet_ratio × 1) — a
    COMPUTED score compositing 3 signals (hybrid pillar), in the contract-
    algorithm urgency form (Zilberstein, AI Magazine 1996; BATS arXiv
    2511.17006): budget position multiplies URGENCY, it never triggers on
    its own.  THE one severity formula — gt_oracle binds this (one product,
    one formula)."""
    return _product_composite_severity(base, budget_fraction, unmet_ratio)


# ---------------------------------------------------------------------------
# RC5 — HYBRID obligation edit-credit (>=3 signals, FACT-tier, correct-or-quiet)
#
# The old credit was SINGLE-SOURCE lexical: an obligation symbol counted as
# "edited" iff its name appeared as a >=3-char token in the edit COMMAND string
# (gt_mini_patch.py:3406 `_BLOCK_TOKEN_RE.findall(cmd)`). That violates all three
# mandatory properties (.claude/CLAUDE.md): not dynamic, not hybrid (one signal),
# not confidence-gated. It produced the measured inversion:
#   - UNDER-COUNT (solved task -> 0.0): apply_patch/sed/heredoc landed the symbol
#     in the patch BODY / file state, but the COMMAND verb (`apply_patch < p`)
#     never spelled the token -> membership failed -> credit dropped to 0.
#   - OVER-COUNT (failed task -> 1.0): the symbol was merely NAMED in the command
#     (sed search pattern, grep pipe, comment, an edit to a DIFFERENT function in
#     the same file) -> token present -> credited, though the edit never landed
#     AT the symbol's definition.
#
# The fix recomputes the credit from THREE composited signals per obligation
# symbol (gt_gt.md §15.2:1215-1221 — credit by the CONTENT written, across all
# edit shapes, not by the command verb; §16.5 issue J:1657 — token-membership is
# "too coarse"; rank by exactness, not first-matched token):
#   Signal 1 (lexical, broadened to CONTENT): the symbol token is in the edit
#     BODY (heredoc / apply_patch payload / sed replacement / python|node write
#     string), not the command verb. Fixes the under-count.
#   Signal 2 (structural, graph co-location): the edited file resolves to a
#     graph Function/Method node whose NAME is the obligation symbol — a real
#     definition in the edited file, not merely a name in the command. FACT-tier
#     (deterministic node identity, same discipline as the contract pillar).
#     Fixes the over-count.
#   Signal 3 (path / line-range overlap): the edited line range overlaps the
#     symbol node's [start_line,end_line] span. The precision tie-breaker that
#     distinguishes "wrote AT the symbol" from "named it elsewhere in the file".
#     REQUIRED when line data is derivable; otherwise (2-of-2) it degrades.
#
# Confidence gate: FACT only when the structural signal actually fired. Graph
# unreachable (no db / _connect_ro None) -> Signals 2,3 cannot run -> degrade to
# content-lexical-only (Signal 1) and the credit is UNCERTAIN, never asserted as
# FACT. None=DORMANT (no obligations) is preserved verbatim. Monotone-safe: can
# only move a wrongly-0.0 solved task UP and a wrongly-1.0 failed task DOWN.
# ---------------------------------------------------------------------------
def _spans_overlap(a_lo, a_hi, b_lo, b_hi) -> bool:
    """Closed-interval [a_lo,a_hi] overlaps [b_lo,b_hi]. Any bound None/<=0 ->
    no usable line data -> return None-equivalent (caller treats as 'no Signal 3
    data', not 'no overlap')."""
    if not a_lo or not b_lo:
        return False
    a_hi = a_hi if a_hi else a_lo
    b_hi = b_hi if b_hi else b_lo
    return a_lo <= b_hi and b_lo <= a_hi


def _file_symbol_spans(db_path, rel_file):
    """{symbol_name: (start_line, end_line)} for every Function/Method node in
    ``rel_file``, from graph.db. Reuses the EXACT contract-pillar access path
    (_connect_ro -> SELECT name,start_line,end_line FROM nodes WHERE
    file_path=? AND label IN ('Function','Method')) — zero new graph plumbing,
    fully within the read-only, fail-quiet-on-missing-graph contract.

    Returns None when the graph is UNREACHABLE (no db file / open failed) — the
    confidence-gate signal that Signals 2,3 could not run. Returns {} when the
    graph is reachable but the file has no nodes (a real 'symbol not a
    definition here' answer, used to REJECT the over-count)."""
    if not db_path or not os.path.isfile(db_path):
        return None
    con = _connect_ro(db_path)
    if con is None:
        return None
    try:
        nfp = _norm_fp(rel_file)
        out: dict[str, tuple] = {}
        for name, sl, el in con.execute(
            "SELECT name, start_line, end_line FROM nodes "
            "WHERE file_path = ? AND label IN ('Function','Method')",
            (nfp,),
        ).fetchall():
            if name:
                out[name] = (sl, el)
        return out
    except Exception:  # noqa: BLE001 -- unreadable graph -> degrade (None)
        return None
    finally:
        try:
            con.close()
        except Exception:  # noqa: BLE001
            pass


def _credit_obligation_symbol(sym, content_toks, file_spans, edited_lines):
    """Decide whether ONE obligation symbol is credited EDITED, by the >=3-signal
    composite. Returns (credited: bool, uncertain: bool).

    Signals:
      1. content lexical — ``sym`` token is in ``content_toks`` (the edit BODY
         tokens across all files edited this trajectory; heredoc/patch payload,
         NOT the command verb).
      2. structural co-location — ``sym`` is a Function/Method node-name in some
         edited file (``file_spans`` = {rel_file: {name: (start,end)} | None}).
      3. line-range overlap — the edit's touched line range for that file
         overlaps the symbol node's [start,end] span.

    Composition (faithful to the design's confidence gate):
      * Graph REACHABLE for >=1 edited file -> FACT tier. The structural signal
        (S2) is the authoritative content source — it carries the symbol even
        when neither the command nor the inline body names it (the apply_patch
        `< file` / sed value-rewrite UNDER-COUNT shape, where the symbol lives in
        the edited file's node, not the command text).
          - S2 ∧ S3 (line data on both sides) -> credit. (The OVER-COUNT killer:
            a symbol DEFINED in the file but edited in a DIFFERENT region is
            rejected because the touched range misses its span.)
          - S2 ∧ (no line data) -> require S1 as the disambiguator (2-of-2): the
            content body must mention the symbol, else a same-file-different-fn
            edit could still over-credit. Correct-or-quiet on the residual.
      * Graph UNREACHABLE for every edited file -> structural signal could not
        run -> degrade to S1 alone and mark UNCERTAIN (never assert FACT on a
        signal that did not fire; on a missing graph the obligation rides as
        UNADDRESSED — the safe, suppress side)."""
    if not sym:
        return (False, False)
    # Is ANY edited file's graph reachable? (spans is a dict; None => unreachable)
    structural_ran = any(spans is not None for spans in file_spans.values())
    if not structural_ran:
        # Graph unreachable everywhere -> Signal-1-only, UNCERTAIN (degrade).
        return ((sym in content_toks), True) if sym in content_toks else (False, True)
    # Structural ran (FACT tier). The node-set is the authoritative content.
    for rel_file, spans in file_spans.items():
        if not spans:
            continue  # unreachable graph for this file, or no nodes -> no S2 here
        span = spans.get(sym)
        if span is None:
            continue  # S2 fails: sym is not a definition in this edited file
        node_lo, node_hi = span
        ranges = edited_lines.get(rel_file) or []
        if node_lo and ranges:
            # S2 ∧ S3: line data on both sides -> REQUIRE overlap (precision).
            if any(_spans_overlap(lo, hi, node_lo, node_hi) for lo, hi in ranges):
                return (True, False)
            # defined here but the edit landed OUTSIDE its span -> reject for this
            # file; keep scanning other edited files (over-count killer).
            continue
        # No line data on one side -> 2-of-2: S2 ∧ S1 (content disambiguates).
        if sym in content_toks:
            return (True, False)
        # S2 held but no line range AND content body does not mention it ->
        # cannot tell WHICH function was edited -> correct-or-quiet (reject here).
        continue
    # No edited file structurally co-locates (or line-range rejected everywhere)
    # -> NOT credited. The OVER-COUNT killer (named-but-not-co-located).
    return (False, False)


def edit_coverage_ratio(obligation_syms, edited_tokens, *,
                        content_toks=None, file_spans=None, edited_lines=None,
                        db_path=None, edited_files=None):
    """Stage-1 (c) — HYBRID obligation edit-credit. obligation symbols credited
    EDITED / total obligation symbols. None = DORMANT (no obligations -> no
    signal -> every consumer stays correct-or-quiet on this clause).

    LEGACY/DEGRADE contract: called with two positional args
    ``(obligation_syms, edited_tokens)`` only, it falls back to the module-level
    structural evidence (the per-file edit-content tokens, line ranges, and the
    set of edited files) populated alongside ``_oracle_edited_tokens`` at the
    post_edit site. The denominator and the None=DORMANT return are IDENTICAL to
    the old contract.

    SOLE CONSUMER (LIPI-verified): the ONLY call site is at the verification-
    horizon producer — ``ec = edit_coverage_ratio(_obligation_symbol_set(),
    _oracle_edited_tokens)`` -> ``verify_horizon_band(edit_coverage=ec, ...)``,
    where ``edit_coverage`` participates in the ADVISORY band predicate
    (``edit_coverage > 0``). It does NOT gate ``spec.obligation`` (that gate uses
    ObligationTracker.statuses_tuple, an unrelated path). Earlier docs that said
    this feeds the "spec.obligation precision gate" were WRONG — it feeds
    verify_horizon_band SEVERITY, nothing else.

    Signals (>=3, composited per symbol — see _credit_obligation_symbol):
      1. content lexical (edit BODY, not command verb),
      2. structural graph co-location (Function/Method node in the edited file),
      3. line-range overlap (edit landed in the symbol's span).
    Confidence-gated: FACT when the structural signal fired; on a missing graph
    it degrades to content-lexical-only and the credit is UNCERTAIN (never a
    confident-on-weak-signal assertion)."""
    syms = {s for s in (obligation_syms or ()) if s}
    if not syms:
        return None
    # CONTENT lexical evidence (Signal 1 domain). Explicit arg (tests) wins;
    # otherwise the module-level per-file edit-content tokens (the BODY tokens),
    # falling back to the command-token set so a 2-positional-arg call is never
    # weaker than today on the lexical axis.
    if content_toks is None:
        content_toks = set(_oracle_edit_content_tokens) if _oracle_edit_content_tokens \
            else set(edited_tokens or ())
    else:
        content_toks = set(content_toks)
    # Structural evidence: {rel_file: {name: (start,end)} | None}. Explicit arg
    # (tests) wins; otherwise resolve each edited file against graph.db via the
    # same read-only path the contract pillar uses.
    if file_spans is None:
        files = edited_files if edited_files is not None else set(_oracle_edited_rels)
        db = db_path if db_path is not None else _db_path()
        file_spans = {rf: _file_symbol_spans(db, rf) for rf in files}
    if edited_lines is None:
        edited_lines = dict(_oracle_edited_lines_by_file)
    credited = 0
    for s in syms:
        ok, _uncertain = _credit_obligation_symbol(
            s, content_toks, file_spans, edited_lines)
        if ok:
            credited += 1
    return credited / len(syms)


def test_coverage_ratio(edited_tokens_by_file, tested_tokens):
    """Stage-1 (d): edited source files whose edit-evidence identifiers show
    test evidence / total edited source files. None = DORMANT (nothing
    edited). Exact-intersection fast path first; the compound-containment
    scan is capped (perf guard, not a behavior threshold)."""
    files = dict(edited_tokens_by_file or {})
    if not files:
        return None
    tested = set(tested_tokens or ())
    covered = 0
    for _f, toks in files.items():
        idents = _coverage_idents(toks)
        if idents & tested:
            covered += 1
            continue
        if any(symbol_tested(t, tested) for t in sorted(idents)[:50]):
            covered += 1
    return covered / len(files)


def _substrate_active() -> bool:
    """True when GT runs in SUBSTRATE-CONSUME mode (handoff §B AFTER / §G): the pinned
    portable substrate already produced the resolved graph + certs into /gt_artifacts and
    the harness handed them to us READ-ONLY via GT_HOST_GRAPH_DB / GT_CERT_DIR. In this
    mode the substrate graph is AUTHORITATIVE and immutable — the adapter must NEVER rebuild
    or mutate it (the "never rebuilds a divergent graph" rule). Detected purely from the
    handoff env so it is harness-agnostic. Mirrors gt_agent._substrate_active exactly."""
    return bool(
        os.environ.get("GT_PORTABLE_SUBSTRATE") == "1"
        or os.environ.get("GT_HOST_GRAPH_DB")
        or os.environ.get("GT_CERT_DIR")
    )


def _db_path() -> str:
    """The graph the per-turn pillars read (hole #6).

    SUBSTRATE-CONSUME (authoritative, no fallback): GT_HOST_GRAPH_DB is read
    UNCONDITIONALLY as THE graph — the SAME LSP-enriched graph the gates measured and
    the host witness fingerprinted. In substrate/proof mode we NEVER fall back to the
    legacy in-container /tmp/graph.db: there IS no second graph (gt_agent removed the
    dual-graph build), so a missing GT_HOST_GRAPH_DB must surface as 'no graph' (the
    pillars are correct-or-quiet on a missing db), never silently read a divergent
    rebuild. The /tmp/graph.db legacy fallback applies ONLY on the non-substrate,
    non-proof (preindex/trial) path."""
    host = os.environ.get("GT_HOST_GRAPH_DB")
    if host:
        return host
    if _substrate_active() or os.environ.get("GT_PROOF_MODE") == "1":
        # Substrate/proof mode but GT_HOST_GRAPH_DB unset -> GT_GRAPH_DB if the harness
        # used the canonical name; NEVER the legacy /tmp/graph.db (no divergent rebuild).
        return os.environ.get("GT_GRAPH_DB") or ""
    return os.environ.get("GT_GRAPH_DB") or "/tmp/graph.db"


def _has_columns(con) -> tuple[bool, bool]:
    """(has_confidence, has_resolution_method) for the edges table.
    Ported from curation_map._has_columns."""
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(edges)").fetchall()}
    except Exception:  # noqa: BLE001
        return (False, False)
    return ("confidence" in cols, "resolution_method" in cols)


# CROSS-LANGUAGE edge disqualifier (_is_cross_language_pair) + the language-column
# probe (_nodes_have_language) are imported from groundtruth.pretask.curation_map
# at the top of this file (B1, 2026-06-13) — the previous inline copies (the
# _LANG_FAMILIES map + helpers) were removed so the same disqualifier governs the
# proof brief AND agent-time delivery. A cross-language CALLS edge cannot be a real
# source call whatever its resolution_method; unknown languages stay PERMISSIVE.


# ---------------------------------------------------------------------------
# SNIPPET ATTESTATION (2026-06-10, DeepSWE non-Python audit). L6 reindex is
# OFF by design on the substrate (authoritative read-only graph), so after the
# agent edits a file the graph's line numbers DRIFT against the live file —
# boa [109] quoted the agent's OWN just-inserted doc comment as
# `root_shape calls -> context/mod.rs:545 '/// Returns an error...'`; [257]
# quoted a bare `}`; arktype's json.ts:92 snippet went stale after a rewrite.
# A line-keyed claim whose live line no longer mentions the attributed symbol
# is GT asserting a falsehood ("wrong info is worse than no info"). The gate:
# a witness/caller-fact row renders ONLY when its snippet still mentions the
# symbol the graph attributes to it; a drifted row is dropped entirely (the
# line number is no longer a fact either). An unreadable file (empty snippet)
# is NOT drift evidence — the row keeps rendering without a snippet, as
# before. Content-based, so it also catches indexer line-skew on unedited
# files; subsumes "track edited files" without needing edit bookkeeping.
# ---------------------------------------------------------------------------
def _snippet_attests(code: str, symbol: str) -> bool:
    """True when the live source line plausibly mentions ``symbol`` (the
    graph's line-keyed claim still holds). Empty code/symbol -> True."""
    if not code or not symbol:
        return True
    return symbol in code


def _code_at(repo_root: str, rel_file: str, line: int) -> str:
    """The source line at (rel_file, line), 1-based, or '' on any error."""
    if not rel_file or not line or line <= 0:
        return ""
    try:
        with open(os.path.join(repo_root, rel_file), encoding="utf-8", errors="ignore") as fh:
            lines = fh.readlines()
        if 0 < line <= len(lines):
            return lines[line - 1].strip()
    except OSError:
        pass
    return ""


def _norm_fp(file_path: str) -> str:
    """Normalize a path to the form gt-index stores in nodes.file_path:
    repo-relative, forward slashes, no leading `./` or `/` (walker.go does
    filepath.Rel + ToSlash). Used for EXACT `file_path = ?` matches — never
    suffix-LIKE (bug #1: `%__init__.py` matched EVERY package's __init__)."""
    return (file_path or "").replace("\\", "/").lstrip("./").lstrip("/")


# Container repo-root prefixes the AGENT'S observations carry. The task runs in
# the eval CONTAINER (verified_gt.yaml cwd=/testbed); its `docker exec` output
# names files as /testbed/<repo-rel> (or the other roots the workflow probes:
# verified_run.yml:284 `for d in /testbed /home/user /workspace /app /repo`).
# But graph.db stores nodes.file_path REPO-RELATIVE (walker.go filepath.Rel over
# the HOST extract /tmp/gt/src). So an absolute container path must have its
# container-root prefix stripped to recover the graph's key — NOT os.path.relpath
# against the host root (that yields `../../testbed/x` and matches nothing ->
# every pillar goes correct-or-quiet EMPTY, the bug). Longest-first so a nested
# root never half-strips. Trailing slash REQUIRED in the match so only a true
# path-segment prefix is removed (never a sibling like `/testbedX/...`).
_CONTAINER_ROOTS = ("/testbed/", "/home/user/", "/workspace/", "/app/", "/repo/")


def _to_repo_rel(f: str, root: str) -> str:
    """Map an observation path ``f`` to a repo-relative key matching graph.db.

    Order (correct-or-quiet — never invent a match):
      1. Absolute container path under a known container root (/testbed/ ...):
         strip that prefix ONCE -> repo-relative. This is the dominant agent
         shape (the task container cwd is /testbed). Both an absolute and the
         equivalent relative path then map to the SAME graph key.
      2. Absolute path that IS under the host extract root: os.path.relpath
         (legitimate when the agent somehow surfaced a host path).
      3. Any other absolute path: fall back to relpath (prior behaviour) — it
         won't match the graph, but that's a quiet miss, not a false hit.
      4. Already-relative path: pass through unchanged.

    The container-root strip touches ONLY absolute paths, so a legitimate
    top-level repo dir named `testbed/` (relative) is never over-stripped."""
    if not f:
        return f
    nf = f.replace("\\", "/")
    if nf.startswith("/"):
        for cr in _CONTAINER_ROOTS:
            if nf.startswith(cr):
                return nf[len(cr):]
        if root:
            nroot = root.replace("\\", "/").rstrip("/") + "/"
            if nf.startswith(nroot):
                return nf[len(nroot):]
        try:
            return os.path.relpath(f, root) if root else f
        except (ValueError, TypeError):
            return f
    return f


# ---------------------------------------------------------------------------
# Sanitizers (bug #4) — ported verbatim from groundtruth (stdlib-only, the
# package is NOT importable in the task container):
#   _sanitize_signature  <- contract_map._sanitize_signature (pyright hover-
#                           markdown D-2 defect: ```python\n(method) def ...```)
#   _clip_balanced       <- runtime.sanitizer.clip_balanced (mid-string /
#                           dangling-operator truncation repair)
# ---------------------------------------------------------------------------
_HOVER_KIND_RE = re.compile(
    r"^\((?:method|function|property|variable|class|parameter|field|constant|module|overload)\)\s*"
)


def _sanitize_signature(sig: str) -> str:
    """Strip leaked LSP/Pyright hover markdown from a stored signature.
    Ported from contract_map._sanitize_signature. No-op on already-clean
    signatures (fast path). Language-agnostic (fences/markers, not AST)."""
    if not sig:
        return sig
    s = sig.strip()
    if "```" not in s and not s.startswith("("):
        return s  # already clean — no hover markdown
    s = s.replace("```python", " ").replace("```", " ")
    cleaned: list[str] = []
    for ln in s.splitlines():
        ln = _HOVER_KIND_RE.sub("", ln.strip()).strip()
        if ln:
            cleaned.append(ln)
    if not cleaned:
        return ""
    for ln in cleaned:
        if "(" in ln:
            return ln
    return cleaned[0]


# A trailing binary/word operator means the clause was cut mid-expression.
_TRAILING_OP_RE = re.compile(
    r"(?:\s+(?:and|or|not|in|is)\b"
    r"|\s*(?:->|\+|-|\*|/|%|<=|>=|==|!=|<|>|&&|\|\||&|\||\^|~|=|,))\s*$"
)


def _clip_balanced(text: str, max_len: int | None = None) -> str:
    """Longest well-formed prefix of ``text`` (quotes balanced, bracket depth
    zero, no dangling operator, never mid-identifier), or "" when none exists.
    Ported from groundtruth.runtime.sanitizer.clip_balanced."""
    if not text:
        return ""
    text = text.rstrip()
    budget = len(text) if max_len is None else min(len(text), max_len)

    in_str = ""
    esc = False
    depth = 0
    safe = 0  # furthest prefix length that is balanced and outside any string
    for i, ch in enumerate(text):
        if i <= budget and not in_str and depth == 0:
            safe = i
        if esc:
            esc = False
            continue
        if in_str:
            if ch == "\\":
                esc = True
            elif ch == in_str:
                in_str = ""
            continue
        if ch in "\"'":
            in_str = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth = max(0, depth - 1)
    if not in_str and depth == 0 and len(text) <= budget:
        safe = len(text)

    if 0 < safe < len(text):
        before = text[safe - 1]
        after = text[safe]
        if (before.isalnum() or before == "_") and (after.isalnum() or after == "_"):
            m = re.search(r"\w+$", text[:safe])
            if m:
                safe = m.start()

    prefix = text[:safe].rstrip()
    prev = None
    while prefix and prev != prefix:
        prev = prefix
        prefix = _TRAILING_OP_RE.sub("", prefix).rstrip()
    return prefix


# One-time state for the in-container readability probe's classified line (bug #5):
# the print fires at most ONCE per process; the probe itself runs on every open
# (sqlite3.connect is lazy — a doomed handle on an unreadable graph only surfaces
# at first query, so the trivial SELECT is what actually proves readability).
_graph_probe_printed = False


def _connect_ro(db: str):
    """Open graph.db READ-ONLY via sqlite URI (bug #5).

    The substrate graph is bind-mounted READ-ONLY into the container; a plain
    ``sqlite3.connect(db)`` against it can fail on a WAL graph (an old
    in-container sqlite, or a leftover ``-wal``, needs write access for WAL
    recovery/locking) — and every pillar then silently returned "" per turn.
    ``mode=ro`` never writes; ``immutable=1`` additionally skips locking + WAL
    entirely and is correct ONLY when nothing can modify the file, i.e. the
    truly-ro substrate/proof mount. On the legacy (non-substrate) path
    /tmp/graph.db is OURS and L6 rewrites it between turns, so plain ``mode=ro``
    is used there instead (immutable on a mutating file risks stale/torn reads).

    READABILITY PROBE: every open runs a trivial schema SELECT (one cached page;
    sqlite3.connect alone is lazy and proves nothing). On the FIRST failure it
    prints a single classified ``[gt-patch] GRAPH_UNREADABLE_IN_CONTAINER:``
    line — so a silently-dead per-turn surface becomes visible in the
    trajectory — then stays quiet on later failures (correct-or-quiet, no spam).
    Returns a connection or None.
    """
    global _graph_probe_printed
    import sqlite3
    dbu = (db or "").replace("\\", "/")
    immutable = _substrate_active() or os.environ.get("GT_PROOF_MODE") == "1"
    uri = f"file:{dbu}?mode=ro" + ("&immutable=1" if immutable else "")
    con = None
    try:
        con = sqlite3.connect(uri, uri=True, timeout=10)
        con.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
        return con
    except Exception as e:  # noqa: BLE001
        if con is not None:
            try:
                con.close()
            except Exception:  # noqa: BLE001
                pass
        if not _graph_probe_printed:
            _graph_probe_printed = True
            try:
                print(f"[gt-patch] GRAPH_UNREADABLE_IN_CONTAINER: {e}", flush=True)
            except Exception:  # noqa: BLE001
                pass
        return None


def _top_func_names(con, file_path: str, limit: int = 3) -> list[str]:
    """Most-referenced non-test Function/Method names in the file. EXACT
    normalized-relpath match (bug #1 — canonical v1r_brief._top_function_names
    uses `n.file_path = ?`; a basename suffix-LIKE matched EVERY package's
    `__init__.py` and cross-attributed other files' evidence as facts).
    Cross-language: pure node-label query, no per-language branch. Issueless
    variant — per-view has no issue-anchor context; the host brief owns
    issue-anchored selection."""
    out: list[str] = []
    try:
        # Over-fetch then drop builtin/dunder-shadow names in Python (2026-06-10
        # fact-filter): a project method shadowing a builtin (`isinstance`)
        # carries a poisoned reference count (callers call the BUILTIN), so it
        # must neither rank here nor seed the caller/contract pillars.
        rows = con.execute(
            "SELECT n.name, COUNT(e.id) AS rc FROM nodes n "
            "LEFT JOIN edges e ON e.target_id = n.id AND e.type = 'CALLS' "
            "WHERE n.file_path = ? "
            "AND n.label IN ('Function','Method') AND COALESCE(n.is_test,0)=0 "
            "GROUP BY n.id ORDER BY rc DESC, n.name LIMIT ?",
            (_norm_fp(file_path), limit * 3 + 5),
        ).fetchall()
        for (name, _rc) in rows:
            if name and name not in out and not _is_builtin_shadow_name(name):
                out.append(name)
            if len(out) >= limit:
                break
    except Exception:  # noqa: BLE001
        pass
    return out


def _resolved_witnesses_for_file(con, file_path: str, repo_root: str, max_each: int = 2) -> list[dict]:
    """Deterministic-provenance caller AND callee witnesses for ``file_path``.

    Ported from v1r_brief._resolved_witnesses_for_file (pure SQL, cross-language).
    A witness is emitted ONLY when its edge resolution_method is in
    ``_DETERMINISTIC_METHODS``; name_match is NEVER a witness. The same
    stdlib-shadow guard the brief applies is applied here. Correct-or-quiet."""
    _, has_method = _has_columns(con)
    if not has_method:
        return []  # cannot judge provenance -> emit nothing (never launder)
    has_lang = _nodes_have_language(con)
    lang_caller_sel = ", nsrc.language, nt.language" if has_lang else ", '', ''"
    lang_callee_sel = ", nt.language, nsrc.language" if has_lang else ", '', ''"
    det_sql = "','".join(sorted(_DETERMINISTIC_METHODS))
    nfp = _norm_fp(file_path)
    out: list[dict] = []
    try:
        caller_rows = con.execute(
            f"""
            SELECT nsrc.file_path, e.source_line, nsrc.name, nt.name{lang_caller_sel}
            FROM nodes nt
            JOIN edges e ON e.target_id = nt.id AND e.type = 'CALLS'
            JOIN nodes nsrc ON e.source_id = nsrc.id
            WHERE nt.file_path = ? AND nsrc.file_path != nt.file_path
              AND COALESCE(nsrc.is_test,0) = 0 AND e.source_line > 0
              AND LOWER(TRIM(e.resolution_method)) IN ('{det_sql}')
            ORDER BY e.source_line LIMIT ?
            """,
            (nfp, max_each * 4),
        ).fetchall()
        for caller_file, line, caller_name, target_name, src_lang, tgt_lang in caller_rows:
            # 2026-06-10 fact-filter: a vendored/minified caller or a
            # builtin/dunder-shadow target is never a delivered [WITNESS] fact.
            if _is_delivery_excluded(caller_file or "", repo_root):
                continue
            if _is_builtin_shadow_name(target_name or ""):
                continue
            # 2026-06-10 cross-language disqualifier (boa [57]): a caller in a
            # different language family cannot be a real call edge.
            if _is_cross_language_pair(src_lang, tgt_lang):
                continue
            code = _code_at(repo_root, caller_file, line)
            if _is_stdlib_shadow(code, target_name or ""):
                continue
            # 2026-06-10 snippet attestation (boa [109]/[257]): the live call-
            # site line must still mention the CALLED symbol, else the graph's
            # line has drifted (post-edit, L6 OFF) and the row is no fact.
            if not _snippet_attests(code, target_name or ""):
                continue
            out.append({"direction": "caller", "file_path": caller_file,
                        "line": int(line) if line else 0, "symbol": caller_name or "",
                        "target": target_name or "", "code": code})
            if sum(1 for w in out if w["direction"] == "caller") >= max_each:
                break

        callee_rows = con.execute(
            f"""
            SELECT nt.file_path, e.source_line, nt.name, nsrc.name, nt.start_line{lang_callee_sel}
            FROM nodes nsrc
            JOIN edges e ON e.source_id = nsrc.id AND e.type = 'CALLS'
            JOIN nodes nt ON e.target_id = nt.id
            WHERE nsrc.file_path = ? AND nt.file_path != nsrc.file_path
              AND COALESCE(nt.is_test,0) = 0
              AND LOWER(TRIM(e.resolution_method)) IN ('{det_sql}')
            ORDER BY e.source_line LIMIT ?
            """,
            (nfp, max_each * 4),
        ).fetchall()
        for (callee_file, source_line, callee_name, src_name, def_line,
             tgt_lang, src_lang) in callee_rows:
            # 2026-06-10 fact-filter: vendored/minified callee files and
            # builtin/dunder-shadow callee names are never [WITNESS] facts.
            if _is_delivery_excluded(callee_file or "", repo_root):
                continue
            if _is_builtin_shadow_name(callee_name or ""):
                continue
            # 2026-06-10 cross-language disqualifier: a callee in a different
            # language family cannot be a real call edge.
            if _is_cross_language_pair(src_lang, tgt_lang):
                continue
            call_code = _code_at(repo_root, file_path, source_line)
            if _is_stdlib_shadow(call_code, callee_name or ""):
                continue
            def_code = _code_at(repo_root, callee_file, def_line) if def_line else ""
            # 2026-06-10 snippet attestation: the live DEFINITION line must
            # still mention the callee's name, else the line drifted.
            if not _snippet_attests(def_code, callee_name or ""):
                continue
            out.append({"direction": "callee", "file_path": callee_file,
                        "line": int(def_line) if def_line else 0, "symbol": callee_name or "",
                        "target": src_name or "",
                        "code": def_code})
            if sum(1 for w in out if w["direction"] == "callee") >= max_each:
                break
    except Exception:  # noqa: BLE001
        return []
    return out


def _caller_contract_for_file(con, file_path: str, repo_root: str, func_names: list[str]) -> str:
    """Categorical, correct-or-quiet caller evidence.
    Ported from v1r_brief._caller_contract_for_file. A cross-file caller renders as a
    confident FACT (``name() in file:line``) ONLY over a DETERMINISTIC edge; name_match
    is never laundered — at/above the floor it renders as a bare `file:line (unverified)`
    location hint with no caller-name claim, facts-first. Cross-language: pure SQL."""
    if not func_names:
        return ""
    has_conf, has_method = _has_columns(con)
    conf_sel = "e.confidence" if has_conf else "0.0"
    method_sel = "e.resolution_method" if has_method else "''"
    has_lang = _nodes_have_language(con)
    lang_sel = ", nsrc.language, nt.language" if has_lang else ", '', ''"
    det_sql = "','".join(sorted(_DETERMINISTIC_METHODS))
    nfp = _norm_fp(file_path)
    fact_parts: list[str] = []
    unverified_parts: list[str] = []
    try:
        for fname in func_names[:2]:
            # 2026-06-10 fact-filter: never claim callers for a builtin/dunder-
            # shadow name (the `isinstance` launder) — defense in depth behind
            # the _top_func_names exclusion.
            if _is_builtin_shadow_name(fname):
                continue
            rows = con.execute(
                f"""
                SELECT nsrc.file_path, e.source_line, nsrc.name, {conf_sel}, {method_sel}{lang_sel}
                FROM nodes nt
                JOIN edges e ON e.target_id = nt.id AND e.type = 'CALLS'
                JOIN nodes nsrc ON e.source_id = nsrc.id
                WHERE nt.name = ? AND nt.file_path = ?
                  AND nsrc.file_path != nt.file_path AND COALESCE(nsrc.is_test,0) = 0
                  AND e.source_line > 0
                ORDER BY CASE WHEN LOWER(TRIM({method_sel})) IN ('{det_sql}') THEN 0 ELSE 1 END,
                         {conf_sel} DESC, e.source_line
                LIMIT ?
                """,
                (fname, nfp, 8),
            ).fetchall()
            for caller_file, source_line, caller_name, conf, method, src_lang, tgt_lang in rows:
                # 2026-06-10 fact-filter: a vendored/minified caller is never a
                # fact NOR an unverified location hint.
                if _is_delivery_excluded(caller_file or "", repo_root):
                    continue
                # 2026-06-10 cross-language disqualifier (boa [57]): a caller in
                # a different language family is never a fact nor a hint —
                # whatever its recorded resolution_method claims.
                if _is_cross_language_pair(src_lang, tgt_lang):
                    continue
                try:
                    conf_f = float(conf) if conf is not None else 0.0
                except (TypeError, ValueError):
                    conf_f = 0.0
                code = _code_at(repo_root, caller_file, source_line)
                if _is_stdlib_shadow(code, fname):
                    continue
                # 2026-06-10 snippet attestation (boa [257]): a FACT render
                # requires the live call-site line to still mention the called
                # function — a drifted line (post-edit, L6 OFF) is no fact.
                if not _snippet_attests(code, fname):
                    continue
                is_fact = (method or "").strip().lower() in _DETERMINISTIC_METHODS
                if is_fact:
                    snippet = code if len(code) <= 80 else code[:77] + "..."
                    rendered = (f"{caller_name}() in {caller_file}:{source_line} `{snippet}`"
                                if snippet else f"{caller_name}() in {caller_file}:{source_line}")
                    if rendered not in fact_parts:
                        fact_parts.append(rendered)
                elif conf_f >= 0.5 or not has_conf:
                    # Honesty marker (curation_map._fmt_edge discipline, bug #9):
                    # a floor-clearing-but-unverified hint is labeled, never shown
                    # indistinguishably from a structurally-resolved fact.
                    hint = f"{caller_file}:{source_line} (unverified)"
                    if hint not in unverified_parts:
                        unverified_parts.append(hint)
                if len(fact_parts) >= 3:
                    break
            if len(fact_parts) >= 3:
                break
    except Exception:  # noqa: BLE001
        return ""
    if fact_parts:
        return " | ".join(fact_parts[:3])
    if unverified_parts:
        return " | ".join(unverified_parts[:2])
    return ""


def _sibling_context(con, file_path: str, func_names: list[str]) -> str:
    """Sibling functions at the same scope — parallel patterns to follow.
    Ported from v1r_brief._sibling_context. EXACT normalized-relpath match
    (bug #1: a basename suffix-LIKE pulled "siblings" from OTHER files with the
    same basename — e.g. every other package's __init__.py). Cross-language."""
    if not func_names:
        return ""
    try:
        rows = con.execute(
            "SELECT DISTINCT n.name FROM nodes n "
            "WHERE n.file_path = ? "
            "AND n.label IN ('Function','Method') AND COALESCE(n.is_test,0)=0 "
            "AND n.name NOT IN ({}) ORDER BY n.start_line LIMIT 8".format(
                ",".join("?" * len(func_names))),
            (_norm_fp(file_path), *func_names),
        ).fetchall()
        # 2026-06-10 fact-filter: builtin-shadow names are not sibling patterns.
        names = [r[0] for r in rows if r[0] and len(r[0]) > 2 and not r[0].startswith("_")
                 and not _is_builtin_shadow_name(r[0])]
        return ", ".join(names[:5]) if names else ""
    except Exception:  # noqa: BLE001
        return ""


def _edit_target_callee_contracts(con, file_path: str, func_names: list[str],
                                  max_funcs: int = 3, max_callees: int = 3,
                                  repo_root: str = "") -> list[str]:
    """Verified callees (signature + location) of the edit-target functions.
    Ported from contract_map.edit_target_callee_contracts (the deciding
    "what does the method I'm editing CALL, and how" fact). name_match callees
    are NEVER included; the deterministic gate is re-checked PER ROW in Python
    (contract_map:558-559 parity, bug #3) and a legacy schema without
    resolution_method ABSTAINS — provenance unknowable means no fact, never
    "include everything". Cross-language: pure SQL over edges/nodes + signatures."""
    if not func_names:
        return []
    _, has_method = _has_columns(con)
    if not has_method:
        # Legacy schema: cannot judge provenance -> emit nothing (never launder).
        return []
    has_lang = _nodes_have_language(con)
    lang_sel = ", nt.language, nsrc.language" if has_lang else ", '', ''"
    nfp = _norm_fp(file_path)
    det_sql = "','".join(sorted(_DETERMINISTIC_METHODS))
    method_clause = f"AND LOWER(TRIM(e.resolution_method)) IN ('{det_sql}')"
    out: list[str] = []
    seen: set[tuple[str, str, str]] = set()
    try:
        for fname in func_names[:max_funcs]:
            if not fname:
                continue
            rows = con.execute(
                f"""
                SELECT nt.name, nt.signature, nt.file_path, nt.start_line,
                       e.resolution_method{lang_sel}
                FROM nodes nsrc
                JOIN edges e ON e.source_id = nsrc.id AND e.type = 'CALLS' {method_clause}
                JOIN nodes nt ON e.target_id = nt.id
                WHERE nsrc.name = ? AND nsrc.file_path = ?
                  AND COALESCE(nt.is_test,0) = 0
                  AND nt.signature IS NOT NULL AND TRIM(nt.signature) != ''
                ORDER BY nt.start_line LIMIT ?
                """,
                (fname, nfp, max_callees * 3),
            ).fetchall()
            added = 0
            for callee_name, sig, callee_file, line, method, tgt_lang, src_lang in rows:
                if added >= max_callees:
                    break
                # Verified-edge gate, re-checked per row (contract_map parity):
                # never surface a non-deterministic callee as a fact.
                if (method or "").strip().lower() not in _DETERMINISTIC_METHODS:
                    continue
                # 2026-06-10 cross-language disqualifier: a callee in a
                # different language family is never a [CALLEE] fact.
                if _is_cross_language_pair(src_lang, tgt_lang):
                    continue
                # 2026-06-10 fact-filter: vendored callee files / builtin-shadow
                # callee names are never [CALLEE] facts.
                if _is_delivery_excluded(callee_file or "", repo_root):
                    continue
                if _is_builtin_shadow_name(callee_name or ""):
                    continue
                # 2026-06-10 snippet attestation (parity with the witness-callee
                # gate): the [CALLEE] render is line-keyed (file:line) — the live
                # DEFINITION line must still mention the callee's name, else the
                # graph's line drifted (post-edit, L6 OFF) and the row is no
                # fact. Unreadable file (empty snippet) is NOT drift evidence.
                if repo_root and line and not _snippet_attests(
                        _code_at(repo_root, callee_file, line), callee_name or ""):
                    continue
                if callee_name == fname and _norm_fp(callee_file) == nfp:
                    continue
                key = (fname, callee_name or "", callee_file or "")
                if key in seen:
                    continue
                seen.add(key)
                sig = _sanitize_signature((sig or "").strip())
                if not sig:
                    continue  # correct-or-quiet: no clean signature, no fact
                loc = f" ({callee_file}:{int(line)})" if line else ""
                out.append(f"[CALLEE] {fname} -> {sig}{loc}")
                added += 1
    except Exception:  # noqa: BLE001
        return []
    return out


def _norm_rel(p: str) -> str:
    """Normalize a path for scope-membership comparison."""
    return (p or "").replace("\\", "/").lstrip("./").lower()


def _query_scope(rel: str) -> list[str]:
    """Graph 1-hop neighbours of `rel`, confidence-gated (>= 0.5). Shared by the
    first-view consensus and the override re-anchor. EXACT normalized-relpath
    match (bug #1) over a read-only URI connection (bug #5)."""
    db = _db_path()
    if not os.path.isfile(db):
        return []
    out: list[str] = []
    try:
        con = _connect_ro(db)
        if con is None:
            return []
        # 2026-06-10 cross-language disqualifier (boa [57] parity): scope is a
        # DELIVERED claim ("graph-connected") and must obey the same gate as the
        # witness/caller facts. Legacy graphs without nodes.language stay
        # PERMISSIVE ('' selects -> cannot judge -> never suppress).
        _lang_sel = ", n1.language, n2.language" if _nodes_have_language(con) else ", '', ''"
        q = (
            f"SELECT DISTINCT n2.file_path{_lang_sel} FROM nodes n1 "
            "JOIN edges e ON (e.source_id = n1.id OR e.target_id = n1.id) "
            "JOIN nodes n2 ON n2.id = (CASE WHEN e.source_id = n1.id "
            "                          THEN e.target_id ELSE e.source_id END) "
            "WHERE n1.file_path = ? "
            "AND n2.file_path != n1.file_path AND n2.file_path IS NOT NULL "
            "AND COALESCE(e.confidence, 0) >= 0.5 ORDER BY e.confidence DESC LIMIT 6"
        )
        try:
            for fp, _l1, _l2 in con.execute(q, (_norm_fp(rel),)):
                # 2026-06-10 fact-filter: vendored/minified/generated neighbours
                # are never delivered scope; nor are cross-language "neighbours"
                # (a call edge between language families is not a real edge).
                if (fp and fp not in out and not _is_vendored_path(fp)
                        and not _is_cross_language_pair(_l1, _l2)):
                    out.append(fp)
        finally:
            con.close()
    except Exception:  # noqa: BLE001
        return []
    return out


def _consensus_progressive(rel: str) -> str:
    """Consensus Layer-B (progressive) + OVERRIDE-on-divergence — OH parity.
    On subsequent source-views: if the file is in the established scope, reinforce it
    once ("also in GT scope"); if the agent has wandered OFF-scope repeatedly, RE-ANCHOR
    consensus on where it actually is now (OH's prefer-divergent-evidence rescue)."""
    global _offscope_views
    if _GT_BASELINE or not _consensus_scope:
        return ""
    n = _norm_rel(rel)

    def _short(p: str) -> str:
        return "/".join((p or "").replace("\\", "/").split("/")[-2:])

    if n in _consensus_scope:
        _offscope_views = 0
        key = ("consensus_b", n)
        if key in _seen:
            return ""
        _seen.add(key)
        return f'\n<gt-scope note="in-scope">\n[GT] {_short(rel)}: also in GT scope.\n</gt-scope>'
    # off-scope view
    _offscope_views += 1
    if _offscope_views < 3:
        return ""
    _offscope_views = 0
    key = ("consensus_override", n)
    if key in _seen:
        return ""
    _seen.add(key)
    scope = _query_scope(rel)
    _consensus_scope.add(n)
    for s in scope:
        _consensus_scope.add(_norm_rel(s))
    lines = [f"1. {_short(rel)} — you have moved here; re-grounding scope"]
    for i, s in enumerate(scope[:4], 2):
        lines.append(f"{i}. {_short(s)} — graph-connected")
    return ('\n<gt-scope reason="re-anchored">\n' + "\n".join(lines)
            + "\nGT re-anchored scope on your current file — confirm the edit target with grep.\n</gt-scope>")


def _consensus_block(rel: str, root: str) -> str:
    """Layer-A CONSENSUS (architecture parity with the OH wrapper's <gt-scope>).

    On the FIRST source-view, deliver the graph-connected SCOPE around the file the
    agent just opened — re-grounding it the moment it starts exploring, the same role
    consensus plays on the OpenHands path. Correct-or-quiet: we list the connected
    scope and tell the agent to confirm the edit target with grep; we do NOT anoint a
    single "primary target" (the imperative steer) here — that confident claim lives in
    the brief's gt-localization, now gated to require >=2 issue anchors. Pure graph
    1-hop neighbours; empty/absent graph -> a minimal scope note, never a guess."""
    global _consensus_fired
    if _consensus_fired:
        return ""
    _consensus_fired = True
    try:
        db = _db_path()
        scope: list[str] = []
        con = _connect_ro(db) if os.path.isfile(db) else None
        if con is not None:
            # 2026-06-10 cross-language disqualifier (boa [57] parity) — same
            # gate as _query_scope; legacy no-language graphs stay PERMISSIVE.
            _lang_sel = (", n1.language, n2.language" if _nodes_have_language(con)
                         else ", '', ''")
            q = (
                f"SELECT DISTINCT n2.file_path{_lang_sel} FROM nodes n1 "
                "JOIN edges e ON (e.source_id = n1.id OR e.target_id = n1.id) "
                "JOIN nodes n2 ON n2.id = (CASE WHEN e.source_id = n1.id "
                "                          THEN e.target_id ELSE e.source_id END) "
                # Confidence gate (parity with OH _detect_scope, which filters >= 0.7):
                # the graph is 70-80% name_match; without this, 0.2-confidence SPECULATIVE
                # neighbours were shown identically to verified edges as "graph-connected".
                # >= 0.5 keeps CERTIFIED + CANDIDATE, drops SPECULATIVE (correct-or-quiet).
                # EXACT normalized-relpath match (bug #1: basename-LIKE pulled
                # neighbours of OTHER same-named files into this file's scope).
                "WHERE n1.file_path = ? "
                "AND n2.file_path != n1.file_path AND n2.file_path IS NOT NULL "
                "AND COALESCE(e.confidence, 0) >= 0.5 "
                "ORDER BY e.confidence DESC "
                "LIMIT 6"
            )
            try:
                for fp, _l1, _l2 in con.execute(q, (_norm_fp(rel),)):
                    # 2026-06-10 fact-filter: vendored/minified/generated
                    # neighbours are never delivered scope; nor cross-language
                    # "neighbours" (not a real call edge — boa [57]).
                    if (fp and fp not in scope and not _is_vendored_path(fp)
                            and not _is_cross_language_pair(_l1, _l2)):
                        scope.append(fp)
            finally:
                con.close()

        # Remember the scope so Layer-B progressive + override can re-ground later views.
        _consensus_scope.add(_norm_rel(rel))
        for _s in scope:
            _consensus_scope.add(_norm_rel(_s))

        def _short(p: str) -> str:
            r = (p or "").replace("\\", "/")
            return "/".join(r.split("/")[-2:]) if "/" in r else r

        if not scope:
            return (
                f'\n<gt-scope files="1">\n'
                f"1. {_short(rel)} — in scope (you are viewing this); GT could not expand "
                f"scope from the graph — confirm the edit target with grep.\n</gt-scope>"
            )
        lines = [f"1. {_short(rel)} — in scope (you are viewing this)"]
        for i, fp in enumerate(scope[:4], 2):
            lines.append(f"{i}. {_short(fp)} — graph-connected")
        return (
            f'\n<gt-scope files="{len(scope[:4]) + 1}">\n'
            + "\n".join(lines)
            + "\nThese files are related in scope; GT has not confirmed a single primary "
            "target — confirm the edit target with grep.\n</gt-scope>"
        )
    except Exception:  # noqa: BLE001 -- correct-or-quiet, never break the loop
        return ""


def _evidence_body(kind: str, rel: str, root: str) -> str:
    """Build the <gt-evidence> body from graph.db (pure SQL, cross-language).

    post_view : resolved-witness facts + caller-contract for the viewed file.
    post_edit : edit-target callee contracts + resolved witnesses.
    Both obey the categorical FACT gate (_DETERMINISTIC_METHODS) + stdlib-shadow
    guard, so no name_match edge is ever laundered as a fact (parity with the
    brief). Correct-or-quiet: empty body -> the caller emits nothing.

    This replaces the old gt_hook.py understand/verify shell-out, which was
    Python-ast-only (.py-filtered at gt_hook.py:4110) and therefore EMPTY on
    every Go/Rust/TS/JS file. graph.db is tree-sitter over ALL languages."""
    # 2026-06-10 fact-filter: a vendored/minified/generated file gets NO
    # evidence at all (correct-or-quiet — its edges are not facts).
    if _is_delivery_excluded(rel, root):
        return ""
    db = _db_path()
    if not os.path.isfile(db):
        return ""
    con = _connect_ro(db)
    if con is None:
        return ""
    lines: list[str] = []
    try:
        func_names = _top_func_names(con, rel, limit=3)
        if kind == "post_edit":
            # What the edited functions CALL, and how to call it correctly.
            for cl in _edit_target_callee_contracts(con, rel, func_names,
                                                    repo_root=root):
                if cl not in lines:
                    lines.append(cl)
        # Resolved cross-file witnesses (caller + callee FACTS) for both kinds.
        for w in _resolved_witnesses_for_file(con, rel, root, max_each=2):
            arrow = "called by" if w["direction"] == "caller" else "calls"
            loc = f"{w['file_path']}:{w['line']}" if w["line"] else w["file_path"]
            # D5 fix: for "caller" direction, the subject of "X called by -> Y"
            # must be the CALLEE (the function in THIS file being called), not
            # the caller.  w["target"] = callee name for caller direction;
            # w["symbol"] = callee name for callee direction (already correct).
            sym = (w["target"] if w["direction"] == "caller" else w["symbol"]) or "?"
            snippet = f" `{w['code']}`" if w.get("code") else ""
            ln = f"[WITNESS] {sym} {arrow} -> {loc}{snippet}".rstrip()
            if ln not in lines:
                lines.append(ln)
        # Caller-contract line for the viewed file (facts-first, unverified hint
        # only when no fact exists). Mainly meaningful on a view.
        if kind == "post_view":
            cc = _caller_contract_for_file(con, rel, root, func_names)
            if cc:
                ln = f"[CALLERS] {cc}"
                if ln not in lines:
                    lines.append(ln)
            sib = _sibling_context(con, rel, func_names)
            if sib:
                ln = f"[SIBLINGS] {sib}"
                if ln not in lines:
                    lines.append(ln)
    except Exception:  # noqa: BLE001 -- correct-or-quiet
        return ""
    finally:
        try:
            con.close()
        except Exception:  # noqa: BLE001
            pass
    return "\n".join(lines[:6]).strip()


def _evidence(cmd: str) -> str:
    if _GT_BASELINE:
        return ""
    kind, f = _classify(cmd)
    if not kind or not f:
        return ""
    root = _root()
    rel = _to_repo_rel(f, root)
    key = (kind, rel)
    if key in _seen:
        return ""
    _seen.add(key)
    ev = _evidence_body(kind, rel, root)
    if not ev:
        return ""
    ev = _translate_to_action(ev, _detect_phase())
    ev = _budget_trim(ev)
    if not ev:
        return ""
    return f"\n<gt-evidence kind=\"{kind}\" file=\"{rel}\">\n{ev}\n</gt-evidence>"


_contract_seen: set[str] = set()


def _graph_contract_block(rel: str) -> str:
    """CROSS-LANGUAGE per-edit contract (parity with OH post_edit [SIGNATURE]/[CALLERS]).
    gt_hook.verify is Python-AST-only — it no-ops on every Go/Rust/TS/JS edit
    (_get_modified_files filters to .py). But the graph (tree-sitter, ALL languages) has
    nodes.signature + CALLS edges for every language. So we deliver the contract + blast
    radius straight from graph.db, which works cross-language by construction. Per-file
    once. Correct-or-quiet: empty graph / no functions -> nothing."""
    if _GT_BASELINE or rel in _contract_seen:
        return ""
    _contract_seen.add(rel)
    # 2026-06-10 fact-filter: no contract for a vendored/minified/generated file.
    if _is_delivery_excluded(rel):
        return ""
    try:
        db = _db_path()
        if not os.path.isfile(db):
            return ""
        con = _connect_ro(db)
        if con is None:
            return ""
        nfp = _norm_fp(rel)
        rows: list = []
        preserve: list[str] = []
        try:
            # Caller COUNT discipline (bug #2 — curation_map._verified_neighbor_count
            # parity): count ONLY deterministic-method edges, confidence >= 0.7,
            # non-test callers. An ungated COUNT laundered name_match guesses into a
            # confident "N caller(s)" number. Legacy schema without resolution_method
            # -> ABSTAIN from the count entirely (no number rather than a fake one).
            has_conf, has_method = _has_columns(con)
            if has_method:
                det_sql = "','".join(sorted(_DETERMINISTIC_METHODS))
                conf_gate = ("AND COALESCE(e.confidence, 0) >= 0.7 " if has_conf else "")
                ncallers_sel = (
                    "(SELECT COUNT(DISTINCT e.source_id) FROM edges e "
                    "   JOIN nodes ns ON ns.id = e.source_id "
                    "   WHERE e.target_id = n.id AND e.type='CALLS' "
                    "   AND COALESCE(ns.is_test,0)=0 "
                    f"  AND LOWER(TRIM(e.resolution_method)) IN ('{det_sql}') "
                    f"  {conf_gate})"
                )
                nfiles_sel = (
                    "(SELECT COUNT(DISTINCT ns.file_path) FROM edges e "
                    "   JOIN nodes ns ON ns.id = e.source_id "
                    "   WHERE e.target_id = n.id AND e.type='CALLS' "
                    "   AND COALESCE(ns.is_test,0)=0 "
                    f"  AND LOWER(TRIM(e.resolution_method)) IN ('{det_sql}') "
                    f"  {conf_gate})"
                )
            else:
                ncallers_sel = "0"
                nfiles_sel = "0"
            q = (
                "SELECT n.id, n.name, n.signature, "
                f" {ncallers_sel} AS ncallers, "
                f" {nfiles_sel} AS nfiles "
                # EXACT normalized-relpath match (bug #1): basename-LIKE attributed
                # other files' functions (every __init__.py) to this contract.
                "FROM nodes n WHERE n.file_path = ? "
                "AND n.label IN ('Function','Method') AND COALESCE(n.is_test,0)=0 "
                "ORDER BY ncallers DESC, n.name LIMIT 8"
            )
            rows = [
                (rid, name, _sanitize_signature((sig or "").strip()), nc, nf)
                for rid, name, sig, nc, nf in con.execute(q, (nfp,)).fetchall()
            ]
            # bug #4: a signature that sanitizes to nothing is no fact — drop the row.
            rows = [r for r in rows if r[2]]
            # 2026-06-10 fact-filter: a builtin/dunder-shadow name carries a
            # POISONED caller count (callers call the language builtin, not the
            # project symbol — the `isinstance: 1048 verified caller(s)` launder)
            # and must never render as a [SIGNATURE]/[CALLERS] contract fact.
            # Over-fetched (LIMIT 8) so real functions still fill the top-3.
            rows = [r for r in rows if not _is_builtin_shadow_name(r[1])][:3]
            # 2026-06-10 fact-filter: recompute the DELIVERED caller count
            # excluding vendored/minified/generated caller FILES — a jquery
            # bundle calling into this file is not a "verified caller" the
            # agent must preserve an interface for. (≤3 rows, cheap.)
            if rows and has_method:
                _has_lang = _nodes_have_language(con)
                _lang_sel = (", ns.language, nt.language" if _has_lang
                             else ", '', ''")
                _fixed = []
                for _rid, _name, _sig, _nc, _nf in rows:
                    _crows = con.execute(
                        f"SELECT DISTINCT e.source_id, ns.file_path{_lang_sel} "
                        "FROM edges e "
                        "JOIN nodes ns ON ns.id = e.source_id "
                        "JOIN nodes nt ON nt.id = e.target_id "
                        "WHERE e.target_id = ? AND e.type='CALLS' "
                        "AND COALESCE(ns.is_test,0)=0 "
                        f"AND LOWER(TRIM(e.resolution_method)) IN ('{det_sql}') "
                        f"{conf_gate}",
                        (_rid,)).fetchall()
                    # 2026-06-10 fact-filter: vendored caller files AND cross-
                    # language callers (boa [57]) are excluded from the
                    # DELIVERED "N verified caller(s)" count.
                    _keep = [(s, fp) for s, fp, _sl, _tl in _crows
                             if not _is_vendored_path(fp or "")
                             and not _is_cross_language_pair(_sl, _tl)]
                    _fixed.append((_rid, _name, _sig,
                                   len({s for s, _ in _keep}),
                                   len({fp for _, fp in _keep})))
                rows = _fixed
            # PRESERVE: behavioural properties of the top (most-called) function — the
            # cross-language equivalent of OH's guard_removed/return_shape safety family
            # (gt_hook's is Python-AST-only). Properties are tree-sitter-mined per language
            # (thin on Go, richer on Python/TS) — correct-or-quiet where absent.
            if rows:
                pq = ("SELECT kind, value FROM properties WHERE node_id = ? "
                      "AND kind IN ('guard_clause','conditional_return','exception_flow','return_shape') "
                      "LIMIT 5")
                for kind, val in con.execute(pq, (rows[0][0],)):
                    # bug #4: balanced clip (never a raw byte slice) so a value the
                    # indexer stored mid-expression never renders an unterminated
                    # literal or a dangling operator; empty after repair -> drop.
                    val = _clip_balanced((val or "").strip(), 120)
                    if not val:
                        continue
                    tag = {"guard_clause": "PRESERVE", "conditional_return": "PRESERVE",
                           "exception_flow": "[RAISES]", "return_shape": "[RETURNS]"}.get(kind, "PRESERVE")
                    preserve.append(f"{tag} {val}")
        finally:
            con.close()
        if not rows:
            return ""
        out = [f'<gt-contract file="{os.path.basename(rel)}">']
        for _id, name, sig, ncallers, nfiles in rows:
            out.append(f"[SIGNATURE] {sig}")
            if ncallers and int(ncallers) > 0:
                out.append(f"[CALLERS] {name}: {int(ncallers)} verified caller(s) in "
                           f"{int(nfiles)} file(s) — preserve this interface")
        for p in preserve:
            out.append(p)
        out.append("</gt-contract>")
        return "\n" + "\n".join(out)
    except Exception:  # noqa: BLE001 -- correct-or-quiet
        return ""


def _cochange_block(rel: str) -> str:
    """COMPLETENESS / co-change (parity with OH post_edit [CO-CHANGE]). On the first
    source EDIT, surface files that HISTORICALLY change together with the edited file —
    the graph's `cochanges` table, git-mined at index time (Zimmermann ICSE'04). This is
    the multi-file completeness signal DeepSWE entirely lacked — the recurring 'edited the
    primary gold file, missed its siblings' bottleneck. Count-gated, correct-or-quiet."""
    global _cochange_fired
    if _cochange_fired or _GT_BASELINE:
        return ""
    try:
        db = _db_path()
        if not os.path.isfile(db):
            return ""
        con = _connect_ro(db)
        if con is None:
            return ""
        nfp = _norm_fp(rel)
        rows: list[tuple[str, int]] = []
        try:
            # EXACT normalized-relpath match (bug #1): basename-LIKE attributed
            # ANOTHER file's co-change history (every __init__.py) to this edit.
            q = (
                "SELECT file_a, file_b, count FROM cochanges "
                "WHERE (file_a = ? OR file_b = ?) "
                "AND count >= 2 ORDER BY count DESC LIMIT 8"
            )
            for fa, fb, cnt in con.execute(q, (nfp, nfp)):
                other = fb if _norm_fp(fa) == nfp else fa
                # 2026-06-10 fact-filter: vendored/minified/generated co-change
                # partners are never delivered (jquery churn is not completeness).
                if other and _is_vendored_path(other):
                    continue
                if other and _norm_fp(other) != nfp and other not in [r[0] for r in rows]:
                    rows.append((other, cnt))
        except Exception:  # noqa: BLE001 -- cochanges table may be absent on old graphs
            return ""
        finally:
            con.close()
        if not rows:
            return ""

        def _short(p: str) -> str:
            r = (p or "").replace("\\", "/")
            return "/".join(r.split("/")[-2:]) if "/" in r else r

        _cochange_fired = True  # consume the one-shot only on a REAL emit (not an empty new-file)
        lines = [f"- {_short(o)} (co-changed {c}x)" for o, c in rows[:4]]
        return (
            "\n<gt-cochange>\nFiles that historically change WITH "
            f"{_short(rel)} — check whether THIS edit also needs them (completeness):\n"
            + "\n".join(lines)
            + "\n</gt-cochange>"
        )
    except Exception:  # noqa: BLE001
        return ""


# ---------------------------------------------------------------------------
# COVERING-TEST QUERY (Stage B / Verification Horizon §3 V1 rung)
# Given a set of edited symbols, find is_test=1 nodes with CALLS edges to those
# symbols (FACT-filtered: deterministic resolution + conf >= 0.7; name_match
# never admits, per the P0 stdlib-shadow rule). Returns up to 2 covering tests
# ranked by confidence desc.
#
# Product value: "GT tells you which test to run after your edit" — useful in
# Cursor, Claude Code, any MCP client — not just SWE-bench. The obligation nudge
# uses this to say "run test_X in tests/test_Y.py" instead of generic "run a
# test that exercises sym". The Verification Horizon H1/H2 uses this for
# targeted test selection.
#
# Language dispatch for the run command mirrors _RETRY_TEST_AUTODETECT's
# manifest-based detection, collapsed to a per-test-file suggestion.
# ---------------------------------------------------------------------------
def _covering_tests_for_symbols(symbol_names: set[str]) -> list[dict]:
    """Query graph.db for test nodes that CALL the given symbols.

    Returns internal test metadata dicts. These identifiers are for targeting
    only and must not be rendered verbatim to the agent-visible surface.
    Correct-or-quiet: no graph, no test nodes, no FACT edges -> empty list."""
    if not symbol_names:
        return []
    try:
        db = _db_path()
        if not os.path.isfile(db):
            return []
        con = _connect_ro(db)
        if con is None:
            return []
        has_conf, has_method = _has_columns(con)
        if not has_method:
            con.close()
            return []
        try:
            # Find node ids for the edited symbols (by name, non-test)
            placeholders = ",".join("?" * len(symbol_names))
            target_q = (
                f"SELECT id, name, file_path FROM nodes "
                f"WHERE name IN ({placeholders}) "
                f"AND COALESCE(is_test, 0) = 0 "
                f"LIMIT 20"
            )
            target_rows = con.execute(target_q, list(symbol_names)).fetchall()
            if not target_rows:
                return []
            target_ids = [r[0] for r in target_rows]

            # Find test nodes that call these targets with FACT-tier edges
            det_sql = "','".join(sorted(_DETERMINISTIC_METHODS))
            conf_gate = "AND COALESCE(e.confidence, 0) >= 0.7 " if has_conf else ""
            tid_placeholders = ",".join("?" * len(target_ids))
            test_q = (
                "SELECT DISTINCT nt.name, nt.file_path, "
                "  MAX(COALESCE(e.confidence, 1.0)) as max_conf "
                "FROM edges e "
                "JOIN nodes nt ON nt.id = e.source_id "
                f"WHERE e.target_id IN ({tid_placeholders}) "
                "AND e.type = 'CALLS' "
                "AND COALESCE(nt.is_test, 0) = 1 "
                f"AND LOWER(TRIM(e.resolution_method)) IN ('{det_sql}') "
                f"{conf_gate}"
                "GROUP BY nt.name, nt.file_path "
                "ORDER BY max_conf DESC, nt.name "
                "LIMIT 8"
            )
            test_rows = con.execute(test_q, target_ids).fetchall()
            if not test_rows:
                return []

            # HYBRID rank (design §3, >=3 signals, lexicographic priority):
            # (1) CALLS-edge confidence PRIMARY — safe-RTS reachability
            #     (Rothermel & Harrold, TOSEM 1997); then
            # (2) test-name overlap with the edited symbols; then
            # (3) path convention (test_<stem> / <stem>_test / __tests__ —
            #     the classical RTS file heuristic, Ekstazi ISSTA 2015).
            lower_syms = {s.lower() for s in symbol_names}

            def _hrank(row):
                tname, tfile, tconf = row
                low_name = (tname or "").lower()
                overlap = sum(1 for s in lower_syms if s in low_name)
                base = os.path.basename(tfile or "").lower()
                conv = 1 if (base.startswith("test") or base.endswith(
                    ("_test.py", "_test.go", "_test.rs", ".test.ts",
                     ".test.js", ".spec.ts", ".spec.js"))
                    or "__tests__" in (tfile or "").lower()) else 0
                return (-float(tconf or 0.0), -overlap, -conv, tname or "")

            test_rows = sorted(test_rows, key=_hrank)[:2]
            results = []
            for tname, tfile, tconf in test_rows:
                run_cmd = _test_run_command(tname, tfile or "")
                results.append({
                    "name": tname,
                    "file": tfile or "",
                    "confidence": float(tconf),
                    "run_cmd": run_cmd,
                })
            return results
        finally:
            con.close()
    except Exception:  # noqa: BLE001 -- correct-or-quiet
        return []


def _test_run_command(test_name: str, test_file: str) -> str:
    """Language-dispatched test run command for a single test function.

    ONE dispatch surface (mirrors _RETRY_TEST_AUTODETECT / _LANG_TO_EXT pattern):
    detects the language from the test file extension and emits the idiomatic
    single-test invocation. Generalized: no task/repo/benchmark logic."""
    ext = os.path.splitext(test_file)[1].lower() if test_file else ""
    if ext == ".py":
        return f"pytest {test_file}::{test_name}"
    elif ext == ".go":
        # Go test files: `go test -run '^TestName$' ./pkg/...`
        pkg_dir = os.path.dirname(test_file) or "."
        return f"go test -run '^{test_name}$' ./{pkg_dir}/..."
    elif ext == ".rs":
        return f"cargo test {test_name}"
    elif ext in (".ts", ".tsx", ".js", ".jsx"):
        return f'npx jest -t "{test_name}"'
    elif ext == ".java":
        # Extract class name from file
        class_name = os.path.splitext(os.path.basename(test_file))[0]
        return f"mvn test -pl . -Dtest={class_name}#{test_name}"
    elif ext == ".rb":
        return f"ruby -Itest {test_file} -n {test_name}"
    else:
        # Fallback: pytest-style (the most common Python test runner)
        return f"pytest {test_file}::{test_name}" if test_file else f"pytest -k {test_name}"


def _invalidate_on_edit(rel: str, root: str) -> None:
    """L6 (minimal incremental-freshness port): after a source edit, drop the stale
    gt_hook AST cache and best-effort single-file reindex graph.db, so the next
    understand / consensus / verify reads the agent's NEW code rather than base-commit.
    On OH the wrapper reindexes after every edit; DeepSWE had nothing, leaving the
    cross-file intelligence frozen for the whole trajectory.

    SUBSTRATE-CONSUME RECONCILIATION (handoff: "NEVER rebuilds a divergent graph",
    §B AFTER / §G; chosen OPTION (a)): when the pinned substrate produced the
    authoritative graph (_substrate_active -> GT_HOST_GRAPH_DB / GT_CERT_DIR set),
    the substrate's /gt_artifacts/graph.db is READ-ONLY and is the SAME graph the
    gates certified and the host witness fingerprinted. A single-file `gt-index -file`
    reindex would MUTATE it (or fork a divergent copy), breaking hook==post-LSP-hash
    parity and the no-divergent-graph rule. So in substrate mode L6 is GATED OFF:
    the per-turn pillars keep reading the substrate graph unchanged. (Option (b) — a
    per-task graph COPY — was rejected: it reintroduces a divergent graph the witness
    would fail to match, strictly worse for the proof.) L6 stays ENABLED only on the
    non-substrate (preindex/trial) path where the in-container /tmp/graph.db is ours."""
    if _substrate_active():
        return  # substrate graph is authoritative + read-only; never mutate/rebuild it.
    try:
        if os.path.isfile(_GT_INDEX_CACHE):
            os.remove(_GT_INDEX_CACHE)
    except Exception:  # noqa: BLE001
        pass
    try:
        gt_index = os.environ.get("GT_INDEX_BIN", "/tmp/gt-index")
        db = _db_path()
        if os.path.isfile(gt_index) and os.path.isfile(db):
            subprocess.run(
                [gt_index, f"-root={root}", f"-file={rel}", f"-output={db}"],
                capture_output=True, timeout=_HOOK_TIMEOUT,
            )
    except Exception:  # noqa: BLE001 -- best-effort, never break the loop
        pass


def _l5_nudge(cmd: str, out_text: str = "",
              loop_arm: bool = True, scaffold_arm: bool = True) -> str:
    """L5 (minimal trajectory-governor port): fire AT MOST ONCE on the two highest-value
    stuck patterns the OH governor catches. The full L5Governor cannot run here (execute()
    has no max_iter / per-turn callback), but these two prevent the unguarded burn:
      (a) scaffold trap  -- many actions, zero source edits (the dominant failure mode);
      (b) repeated-command loop -- the same command producing the SAME observation 4+
          times. 2026-06-10 (PATH B audit, 13453 false fire): "loop" requires NO NEW
          STATE — the same command with a DIFFERENT observation each run is iteration,
          not a loop, so the signature is (command, normalized observation), not the
          command alone. Correct-or-quiet.

    Delivery-engine STAGE 3 (2026-06-11): on the ORACLE route the windowed
    loop arm is RETIRED (loop_arm=False) — detect.loop (TIDE k-step cycles
    over the WHOLE trajectory, dynamic thresholds) supersedes it; the
    window-12 check measured 0 fires on the run it existed for (fd's 5x loop
    was spread >12 actions apart — DEEP_TRAJECTORY §3.2). The legacy route
    (GT_ORACLE_ROUTE=0) keeps the original arm unchanged.

    FIX 1 (2026-06-11 measurement-gate closeout): on the ORACLE route the
    scaffold arm is RETIRED too (scaffold_arm=False). Research INVALIDATES
    penalizing exploration volume (early-patch-intensity rho = -0.78 — high
    pre-edit exploration correlates with SUCCESS, not failure); live receipts:
    fired uniformly at ~step 25 on 7/9 oracle-run tasks, 0 consumed, 1 wrong
    steer (csstree -> fixture.js, gt_gt §16.4). The legacy route keeps it
    (backward compat / byte-parity with the pre-oracle governor)."""
    global _l5_fired
    if _l5_fired or _GT_BASELINE:
        return ""
    norm = (cmd or "").strip()
    if norm:
        # Loop signature = command + collapsed observation (no-new-state proof).
        sig = norm + "\x00" + " ".join((out_text or "").split())[:400]
        _cmd_history.append(sig)
        if len(_cmd_history) > 12:
            del _cmd_history[0]
        if loop_arm and _cmd_history.count(sig) >= 4:
            _l5_fired = True
            return ('\n<gt-nudge reason="loop">\nGT: you have repeated the same command 4+ '
                    "times with no progress. Stop, re-read the last error, and change approach "
                    "(open a different file or test a new hypothesis).\n</gt-nudge>")
    if scaffold_arm and _action_count >= 25 and _source_edit_count == 0:
        _l5_fired = True
        return ('\n<gt-nudge reason="scaffold_trap">\nGT: 25+ actions and no source-file edit '
                "yet — you are likely stuck exploring/scaffolding. Use the brief's gt-scope to "
                "localize and make a concrete edit to a SOURCE file now.\n</gt-nudge>")
    return ""


# ---------------------------------------------------------------------------
# failure_persisted classification (2026-06-10, PATH B audit run 27260307167:
# 5/7 firings were ENVIRONMENT errors — pip/C-ext/import/py-version shims —
# and 1 firing reinforced reverting a gold-equivalent edit that had only been
# checked against a scratch script + stale visible fixture, django-10097).
# gt_gt §12 L5 role: a nudge must never harm a correct course (Cursor
# mentality). Three gates, all required, uncertain -> SILENT:
#   1. the failing command is a REAL test-runner invocation (a scratch
#      script's failure cannot falsify a hypothesis);
#   2. NO environment/tooling failure marker in the output (an env failure
#      says nothing about the hypothesis — parity with the OH governor's
#      classify_observation.is_env_failure suppression, governor.py:307);
#   3. an explicit test/assertion FAILURE marker is present (a bare
#      Traceback / "Error:" is not proof a TEST failed).
# ---------------------------------------------------------------------------
# Canonical behavioral patterns — import from product (audit RED #1, #2).
# Graceful fallback keeps the IDENTICAL superset inline so the agent container
# never diverges even when /opt/gt/src is unavailable at .pth bootstrap.
try:
    from groundtruth.runtime.patterns import (
        TEST_RUNNER_RE as _TEST_RUNNER_RE,
        ENV_FAIL_RE as _ENV_FAIL_RE,
        TEST_FAIL_RE as _TEST_FAIL_RE,
        TEST_PASS_RE as _TEST_PASS_RE,
        COMPILE_FAIL_RE as _COMPILE_FAIL_RE,
    )
except ImportError:
    _TEST_RUNNER_RE = re.compile(
        r"(?:^|[|&;]\s*)(?:timeout\s+(?:-\S+\s+|\d+\S*\s+)+|time\s+|env\s+(?:\S+=\S+\s+)+"
        r"|python[\d.]*\s+(?=\S*\.py\b))*(?:"
        r"python[\d.]*\s+-m\s+(?:pytest|unittest|nose2?|tox)\b"
        r"|pytest\b|py\.test\b|tox\b|nose2?\b"
        r"|(?:\S*/)?(?:runtests?|run_tests?)\.py\b"
        r"|(?:\S*/)?manage\.py\s+test\b"
        r"|go\s+test\b|cargo\s+test\b"
        r"|npm\s+(?:run\s+)?test\b|yarn\s+(?:run\s+)?test\b|pnpm\s+(?:run\s+)?test\b"
        r"|jest\b|mocha\b|vitest\b|rspec\b|rake\s+test\b|phpunit\b|ctest\b"
        r"|mvn\s+\S*\s*test\b|gradlew?\s+\S*\s*test\b|make\s+(?:check|test)\b"
        r")", re.I)
    _ENV_FAIL_RE = re.compile(
        r"(ModuleNotFoundError|No module named|ImportError"
        r"|ERROR: Could not find a version|No matching distribution found"
        r"|Could not build wheels|subprocess-exited-with-error|metadata-generation-failed"
        r"|error: command .* failed|fatal error: |compilation terminated"
        r"|undefined reference to|ld returned \d+ exit status|collect2: error"
        r"|command not found|is not recognized as an internal or external command"
        r"|Connection refused|Network is unreachable|Temporary failure in name resolution"
        r"|CERTIFICATE_VERIFY_FAILED|ReadTimeoutError|ProxyError"
        r"|error while loading shared libraries|cannot open shared object"
        r"|ImproperlyConfigured"
        r"|AttributeError: module '[\w.]+' has no attribute"
        r"|errors? during collection|ERROR collecting|Interrupted: \d+ error)", re.I)
    _TEST_FAIL_RE = re.compile(
        r"(\bFAILED\b|\bAssertionError\b|\b\d+ failed\b|\bFAIL: "
        r"|FAILED \(failures=|--- FAIL:|test result: FAILED"
        r"|\b\d+ failing\b|Tests:\s+\d+ failed)")
    _TEST_PASS_RE = re.compile(
        r"(test result: ok\b|\b\d+ passed\b|\b\d+ passing\b"
        r"|^OK\b|^ok\s+\S+\s+[\d.]+s|^PASS$|^PASS\b"
        r"|OK \(\d+ tests?\)|Tests:\s+\d+ passed|\bpassed\b.*\b0 failed\b)",
        re.M)
    _COMPILE_FAIL_RE = re.compile(
        r"(error\[E\d+\]|error: could not compile|\bSyntaxError\b"
        r"|cannot find (?:value|function|type|module|symbol)"
        r"|undefined:\s|\bTS\d{4,}:|compilation error)")

# ---------------------------------------------------------------------------
# DELIVERY-ENGINE STAGE 5 (2026-06-11) — failure_persisted FP closure.
# Run 27321848581 measured failure_persisted 3/3 FALSE POSITIVE.  LIPI root
# cause, PROVEN on the frozen fd trajectory (turns 122/226/242/250/269/274):
# the "fail" lines the governor matched were **fully GREEN result lines** —
# `test result: ok. 106 passed; 0 failed; …` — because `\b\d+ failed\b`
# matches "0 failed".  Two green runs -> identical "failure" signature ->
# "persisted" -> fired right after a 106/106 pass (fd step 270).
# Three deterministic exclusions, all correct-or-quiet (they only ever
# SUPPRESS a nudge, never invent one):
#   1. ZERO-COUNT results are pass evidence, never failure lines
#      (_TEST_FAIL_STRICT_RE: counts must be >= 1);
#   2. patch/apply bookkeeping lines (`Hunk #N FAILED at L`, `error: patch
#      failed: …`) are never TEST-failure lines;
#   3. a failure signature first observed at BASELINE — before any source
#      edit, or while the agent's changes are stashed (`git stash` …
#      `git stash pop/apply`) — is the repo's own state, never "persisted
#      across your edit(s)" (the abs-module/abs-stepped pre-existing class).
# _TEST_FAIL_RE itself is FROZEN (the gt_oracle Stage-2 byte-parity corpus
# replays against it); the corrected predicate lives in _failure_lines and
# is what the live governor and parity_mode=False replay consume.
# ---------------------------------------------------------------------------
_TEST_FAIL_STRICT_RE = re.compile(
    r"(\bFAILED\b|\bAssertionError\b|\b[1-9]\d* failed\b|\bFAIL: "
    r"|FAILED \(failures=|--- FAIL:|test result: FAILED"
    r"|\b[1-9]\d* failing\b|Tests:\s+[1-9]\d* failed)")
_PATCH_NOISE_RE = re.compile(
    r"(^\s*error: (?:patch failed\b|corrupt patch\b|while searching for\b"
    r"|.*: (?:patch does not apply|does not exist in index|already exists))"
    r"|^\s*Hunk #\d+ (?:FAILED|succeeded)"
    r"|^\s*\d+ out of \d+ hunks? FAILED"
    r"|^\s*Reversed \(or previously applied\) patch)", re.I)
# `git stash` push (bare/push/save) opens a BASELINE window; pop/apply closes it.
_STASH_PUSH_RE = re.compile(r"git\s+stash\b(?!\s+(?:pop|apply|list|show|drop|branch|clear))")
_STASH_POP_RE = re.compile(r"git\s+stash\s+(?:pop|apply)\b")
_stash_depth = 0
_baseline_fail_sigs: set[str] = set()


def _failure_lines(text: str) -> list[str]:
    """TEST-failure lines only: zero-count-safe (_TEST_FAIL_STRICT_RE) AND
    not patch/apply bookkeeping. Shared by the live governor and the
    (non-parity) replay so the twins cannot disagree on what counts as a
    failure line."""
    return [ln.strip() for ln in (text or "").splitlines()
            if _TEST_FAIL_STRICT_RE.search(ln) and not _PATCH_NOISE_RE.search(ln)]


# ---------------------------------------------------------------------------
# no_test_evidence governor (2026-06-10, DeepSWE non-Python audit, run
# 27290157847 — the single highest-value missing layer the audit found).
# boa [243]-[333]: six `timeout N cargo test` runs were SIGKILLed mid-build;
# the agent saw only compile lines, concluded ([332]) "The tests seem to pass
# but it's not printing results", and SUBMITTED a 398-line feature it had
# never seen one test execute against (reward 0; the kills had also corrupted
# the incremental build the grader then failed on). The submit action itself
# NEVER reaches env.execute() (mini-swe intercepts it before execution:
# `<exception>action was not executed</exception>`), so a submit-time hook is
# structurally impossible — the governor fires on the PATTERN instead, at the
# moment it is actionable. Event definition (all required, else SILENT):
#   1. the command is a REAL test-runner invocation (_TEST_RUNNER_RE — the
#      same gate as failure_persisted);
#   2. the output carries NO test result: no pass marker (_TEST_PASS_RE), no
#      fail marker (_TEST_FAIL_RE) — a result either way is evidence and
#      latches _test_evidence_seen for the session;
#   3. no env-failure marker (_ENV_FAIL_RE) and no compile error
#      (_COMPILE_FAIL_RE) — those are actionable feedback, not blindness;
#   4. the 2nd such BLIND run, with >=1 source edit and ZERO test evidence
#      observed all session -> fire ONCE.
# Generalized: language-agnostic runner + result regexes, no benchmark/task
# logic; correct-or-quiet: any observed result, env error, or compile error
# keeps it silent.
# ---------------------------------------------------------------------------
def _l5_no_test_evidence_nudge(cmd: str, out_text: str) -> str:
    """Fire ONCE when the agent's real test-runner invocations repeatedly
    produce NO observable test result (timeout/SIGKILL mid-build) — before it
    concludes 'tests seem to pass' and submits blind. Correct-or-quiet."""
    global _l5_notest_fired, _blind_test_runs, _test_evidence_seen
    if _l5_notest_fired or _GT_BASELINE:
        return ""
    if not _TEST_RUNNER_RE.search(cmd or ""):
        return ""
    text = out_text or ""
    if _TEST_FAIL_RE.search(text) or _TEST_PASS_RE.search(text):
        _test_evidence_seen = True  # a result was observed — pattern moot
        return ""
    if _ENV_FAIL_RE.search(text) or _COMPILE_FAIL_RE.search(text):
        return ""  # actionable feedback, not blindness
    _blind_test_runs += 1
    if (_blind_test_runs >= 2 and not _test_evidence_seen
            and _source_edit_count >= 1):
        _l5_notest_fired = True
        return ('\n<gt-nudge reason="no_test_evidence">\nGT: your test commands have produced '
                "no visible test results (likely killed/timed out before any test ran). You have "
                "NOT observed a single test execute — do not conclude tests pass, and do not "
                "submit yet. Run a narrower target (one test name / one module) or raise the "
                "timeout until you see real pass/fail output.\n</gt-nudge>")
    return ""


# ---------------------------------------------------------------------------
# DELIVERY-ENGINE STAGE 3 (2026-06-11) — two behavioral detector classes over
# the Stage-1 signals.  Both sev-5 (below the verification gate, above the
# advisory band), both fire-once with gate-loss re-arm, both candidates in the
# ONE oracle gate (one product, one gate — never a side channel).
#
# (a) detect.loop — TIDE (arXiv 2602.02196) degenerate-cycle detector.
#     Replaces the window-12 same-signature arm on the oracle route: fd's
#     stale-binary loop (5x identical command+output at steps 177-234, spread
#     >12 apart) drew SILENCE from the windowed check — ~75 wasted steps and
#     a fabricated-observation hallucination (DEEP_TRAJECTORY §1.8/§3.2 "loop
#     nudge 0 fires; sensor not live").  HYBRID, 4 composited signals:
#       reps(current sig) >= 3            — a recursive cycle, not a revisit
#       loop_ratio  > median+MAD(history) — degenerate share above the
#                                           trajectory's OWN norm
#       new_state_rate < median(history)  — node production below its OWN norm
#       steps >= 2*window                 — window-derived minimum, no magic
#     DYNAMIC: thresholds are median/MAD over the trajectory-so-far's own
#     signal history (Leys et al., J.Exp.Soc.Psych 2013 — the same robust
#     pair as the oracle's distribution floor). No constant thresholds.
#
# (b) detect.coherence_collapse — TRAJEVAL (arXiv 2603.24631): 60-69% of
#     failures REACH the right code then thrash it (the `Pr` re-patch symbol,
#     arXiv 2604.02547).  HYBRID, 3 composited signals: per-file churn >= 3
#     ∧ no passing test between (churn resets on observed pass, by
#     construction) ∧ target anchored (issue anchors/obligations) or the last
#     observed test outcome failed; unknown anchors -> permissive (the
#     cross-language-disqualifier precedent: never suppress on data we
#     cannot judge).
# ---------------------------------------------------------------------------
_SEV_DETECT = 5  # spec: below gate (6), above advisory (4)
_traj_state_keys: list[str] = []
_traj_loop_sigs: list[str] = []
_lr_history: list[float] = []
_nsr_history: list[float] = []
_detect_loop_fired = False
_coherence_fired_files: set[str] = set()
_coherence_last_rel: str | None = None


def _degenerate_loop_candidate(cmd: str, raw_obs: str) -> tuple[float, str] | None:
    """detect.loop producer — called EVERY turn on the oracle route (it owns
    the trajectory-stream bookkeeping).  Returns (severity, payload) or None."""
    global _detect_loop_fired
    import statistics as _st
    norm = (cmd or "").strip()
    sig = (norm + "\x00" + _obs_collapse(raw_obs)) if norm else None
    _traj_state_keys.append(_behavior_state_key(cmd, raw_obs))
    if sig:
        _traj_loop_sigs.append(sig)
    lr = compute_loop_ratio(_traj_loop_sigs)
    nsr = compute_new_state_rate(_traj_state_keys)
    fire = False
    reps = 0
    if (not _detect_loop_fired and sig
            and len(_lr_history) >= 2 * _STATE_WINDOW):
        lr_med = _st.median(_lr_history)
        lr_mad = _st.median(abs(v - lr_med) for v in _lr_history)
        nsr_med = _st.median(_nsr_history)
        reps = _traj_loop_sigs.count(sig)
        fire = (reps >= 3 and lr > lr_med + lr_mad and nsr < nsr_med)
    # history = the PRIOR turns' distribution; the current value never gates itself.
    _lr_history.append(lr)
    _nsr_history.append(nsr)
    if not fire:
        return None
    _detect_loop_fired = True
    body = (
        f"GT: you have run this exact command with this exact output {reps} "
        "times, and your recent actions are producing almost no new state — "
        "this is a degenerate loop, not progress. If you edited a compiled "
        "binary's source, REBUILD before re-running; otherwise re-read the "
        "last real error and try a different approach (different file, "
        "different hypothesis, or a narrower test)."
    )
    return (float(_SEV_DETECT),
            f'\n<gt-nudge reason="degenerate_loop">\n{body}\n</gt-nudge>')


def _coherence_collapse_candidate(rel: str) -> tuple[float, str] | None:
    """detect.coherence_collapse producer called after edit churn increments."""
    global _coherence_last_rel
    if rel in _coherence_fired_files:
        return None
    churn = _edit_churn.get(rel, 0)
    if churn < 3:
        return None
    anch = _oracle_focus()
    stem = os.path.splitext(os.path.basename(rel))[0]
    ftoks = _oracle_edited_tokens_by_file.get(rel, set())
    anchored = (not anch) or (stem in anch) or bool(ftoks & anch) \
        or _last_test_outcome_failed
    if not anchored:
        return None
    _coherence_fired_files.add(rel)
    _coherence_last_rel = rel
    hint = ""
    try:
        idents = sorted(_coverage_idents(ftoks) & anch) or \
            sorted(_coverage_idents(ftoks))[:10]
        if _covering_tests_for_symbols(set(idents[:10])):
            hint = (
                " A graph-linked covering test exists; run the narrowest "
                "relevant repo test target before editing again."
            )
    except Exception:  # noqa: BLE001 -- enrichment is best-effort
        pass
    body = (
        f"GT: you have rewritten {os.path.basename(rel)} {churn} times with "
        "no passing test between edits - you are overwriting your own work "
        "blind. Run targeted verification FIRST to see what is actually "
        f"failing, then make one targeted edit.{hint}"
    )
    return (float(_SEV_DETECT),
            f'\n<gt-nudge reason="coherence_collapse">\n{body}\n</gt-nudge>')


def _l5_failure_nudge(cmd: str, out_text: str) -> str:
    """L5 hypothesis-falsified (OH hook_same_failure_persisted): the SAME genuine
    TEST failure recurs across the agent's edit(s) -> the hypothesis may be wrong.
    Fires once, only after a source edit, and ONLY when the three classification
    gates above all pass — an environment/tooling failure or a scratch-script
    failure stays SILENT (correct-or-quiet; wrong steering is worse than none)."""
    global _l5_failure_fired, _stash_depth
    # Stash-window tracking runs on EVERY turn (baseline detection must not
    # depend on the latch state).  The dominant agent shape is the SINGLE-TURN
    # disproof — `git stash && go test … ; git stash pop` (abs-module turn 69,
    # abs-stepped turn 55, live receipts) — so a turn that PUSHES a stash is
    # itself a baseline window, whatever it pops later in the same command.
    _pushes = len(_STASH_PUSH_RE.findall(cmd or ""))
    _pops = len(_STASH_POP_RE.findall(cmd or ""))
    _turn_is_stashed = _stash_depth > 0 or _pushes > 0
    _stash_depth = max(0, _stash_depth + _pushes - _pops)
    if _l5_failure_fired or _GT_BASELINE or not out_text:
        return ""
    # Gate 1: only a real test-runner invocation can falsify a hypothesis.
    if not _TEST_RUNNER_RE.search(cmd or ""):
        return ""
    # Gate 2: env/tooling failure -> not hypothesis evidence -> silent.
    if _ENV_FAIL_RE.search(out_text):
        return ""
    # Gate 3: an explicit test/assertion failure marker is required — and
    # patch/apply bookkeeping lines never qualify (Stage-5 fd FP closure).
    fails = _failure_lines(out_text)
    sig = "|".join(sorted(set(fails))[:3])[:200]
    if not sig:
        return ""
    # Gate 4 (Stage-5 abs FP closure): a failure observed at BASELINE — no
    # source edit yet, or the agent's changes are stashed — is the repo's own
    # state; record it and never let it drive "persisted across your edit(s)".
    if _source_edit_count == 0 or _turn_is_stashed:
        _baseline_fail_sigs.add(sig)
        return ""
    if sig in _baseline_fail_sigs:
        return ""
    _test_fail_history.append(sig)
    if _test_fail_history.count(sig) >= 2 and _source_edit_count >= 1:
        _l5_failure_fired = True
        return ('\n<gt-nudge reason="failure_persisted">\nGT: the same test failure has '
                "persisted across your edit(s) — your current hypothesis is likely wrong. "
                "Re-read the failing assertion and reconsider the root cause / target file.\n</gt-nudge>")
    return ""


# ---------------------------------------------------------------------------
# STAGE-4 ORACLE ROUTING (gt_gt §15.4 Stage 4 / ORACLE_ARCHITECTURE_PLAN §2.2).
# The block producers (L3 contract, L3 cochange, L3b evidence, consensus scope,
# L5 nudges) stop appending unconditionally; they become CANDIDATES behind one
# gate enforcing RELEVANCE (anchors ∪ obligations ∪ edit set), DEDUP
# (content-hash; rearm_on_change = a changed block re-arms), and BUDGET
# (<=1 GT block per turn, severity-ranked).  Consensus K-of-N completeness is
# RE-ROUTED from per-view to the live REVIEW-TRANSITION trigger (§11.4).
# Kill switch: GT_ORACLE_ROUTE=0 restores the legacy unconditional appends.
# Telemetry: 8-dp suppression records appended to GT_ORACLE_EVENTS
# (default /tmp/gt_oracle_events.jsonl) — never agent-visible.
# ---------------------------------------------------------------------------
_ORACLE_ROUTE = os.environ.get("GT_ORACLE_ROUTE", "1") != "0"
if os.environ.get("GT_PROOF_MODE") == "1" and os.environ.get("GT_ORACLE_ROUTE") == "0":
    raise RuntimeError(
        "GT_ORACLE_ROUTE=0 forbidden in GT_PROOF_MODE=1 (legacy unconditional appends)"
    )
_oracle_focus_cache: set[str] | None = None
_oracle_delivered_hashes: set[str] = set()
_oracle_edited_rels: set[str] = set()
_oracle_nonedit_streak = 0
_oracle_review_fired = False
# SPEC obligation state (LIPI 2026-06-10: gt_oracle.load_obligations had ZERO
# live callers — the Rank-1 test-evidence-gap nudge fired only in replay).
# The live producer mirrors the replay sensor's plan §5.2 surfaces:
#   edited_tokens — tokens of edit-command text (the edit EVIDENCE: an edit
#     command carries the code it writes), the "edited?" intersection set;
#   tested_tokens — tokens of test-runner command+output WHEN a real pass/fail
#     result was observed (the "tested?" intersection set).
# Dose (delivery-engine Stage 2, 2026-06-11 — supersedes the once-per-task
# latch): the obligation-STATUS emission fires at EVERY review transition
# whose status VECTOR is new (content×phase×status dedup) — the once-per-task
# dose was THE measured gap (DEEP_TRAJECTORY §3.2: review-transition emissions
# 0 of 9; the one early nudge "deduped forever").  The vector can only change
# when the agent's own edits/tests change an obligation's status, so the dose
# is bounded by the agent's own progress — data-derived, no invented cap.
_oracle_obligation_fired = False           # last-production marker (telemetry/tests)
_oblig_status_emitted: set[str] = set()    # delivered status-vector hashes
_oblig_status_last_hash: str | None = None  # released on a gate loss (deferred)
_oracle_edited_tokens: set[str] = set()
_oracle_tested_tokens: set[str] = set()
# per-file edit-evidence tokens (Stage-1 signal: test_coverage_ratio input).
_oracle_edited_tokens_by_file: dict[str, set] = {}
# RC5 hybrid edit-credit evidence (Signal 1 + Signal 3). edit_content_tokens are
# the identifier tokens of the edit BODY (heredoc/apply_patch payload, sed
# replacement, python|node write string) — broadened from the command verb so an
# obligation symbol introduced in the patch CONTENT is credited even when the
# command line never spells it (the under-count fix). edited_lines_by_file are
# the touched line ranges per repo-relative file, derived from diff hunk headers
# (`@@ -a,b +c,d @@`) or sed line addresses — Signal 3's edit-site range.
_oracle_edit_content_tokens: set[str] = set()
_oracle_edited_lines_by_file: dict[str, list] = {}
# per-file re-edit churn with no observed PASSING test between (Stage-1
# signal; TRAJEVAL Coherence Collapse) — reset on every observed pass.
_edit_churn: dict[str, int] = {}
_gt_oracle_mod = None
_gt_oracle_tried = False


def _reset_oracle_state() -> None:
    """Clear ALL oracle/delivery state — call between retry attempts (D2 fix)."""
    global _action_count, _oracle_nonedit_streak, _oracle_obligation_fired
    global _consensus_fired, _cochange_fired, _l5_fired
    global _obligation_tracker, _obligation_tracker_anchors
    global _last_budget_pending
    global _ledger_consumed_kinds, _ledger_ignore_counts
    global _last_delivered_kind, _last_gate_winner_kind
    global _horizon_advisory_fired
    _action_count = 0
    _oracle_nonedit_streak = 0
    _oracle_obligation_fired = False
    _consensus_fired = False
    _cochange_fired = False
    _l5_fired = False
    _oracle_edited_rels.clear()
    _oracle_tested_tokens.clear()
    _oracle_edited_tokens.clear()
    _oracle_edited_tokens_by_file.clear()
    _oracle_edit_content_tokens.clear()
    _oracle_edited_lines_by_file.clear()
    _edit_churn.clear()
    _oblig_status_emitted.clear()
    _oracle_delivered_hashes.clear()
    _HOOK_FIRE_COUNTS.clear()
    _obligation_tracker = None
    _obligation_tracker_anchors = None
    _last_budget_pending = []
    _PRODUCT_BUDGETER.reset()
    _ledger_consumed_kinds = set()
    _ledger_ignore_counts = {}
    _last_delivered_kind = ""
    _last_gate_winner_kind = ""
    _horizon_advisory_fired = False
    try:
        _oracle_last_losers.clear()
    except Exception:
        pass


def _anchors_path() -> str:
    """Resolve the per-task gt_issue_anchors.json artifact, call-time.
    Priority: GT_ANCHORS_PATH (explicit) -> $GT_CERT_DIR/gt_issue_anchors.json
    (the substrate-consume container mount /gt_artifacts, where the artifact
    actually lives in the live deepswe_full run — the /tmp default never exists
    in the agent container) -> the /tmp default (host/replay)."""
    p = os.environ.get("GT_ANCHORS_PATH")
    if p:
        return p
    cert = os.environ.get("GT_CERT_DIR", "")
    if cert:
        cand = os.path.join(cert, "gt_issue_anchors.json")
        if os.path.isfile(cand):
            return cand
    return "/tmp/gt_issue_anchors.json"


def _load_gt_oracle():
    """Lazy sibling load of gt_oracle.py (the SPEC/obligation producer + the
    distribution-derived gate_pool). Pre-registers THIS module under
    gt_oracle's sibling key so its primitives (_TEST_RUNNER_RE etc.) are
    SHARED, never re-executed. Correct-or-quiet: a missing sibling (gt_oracle
    or its gt_oracle_sense dependency) -> None, no obligation candidates."""
    global _gt_oracle_mod, _gt_oracle_tried
    if _gt_oracle_tried:
        return _gt_oracle_mod
    _gt_oracle_tried = True
    try:
        import importlib.util as _ilu
        sys.modules.setdefault("gt_mini_patch_oracle", sys.modules[__name__])
        here = os.path.dirname(os.path.abspath(__file__))
        path = os.path.join(here, "gt_oracle.py")
        if not os.path.isfile(path):
            return None
        spec = _ilu.spec_from_file_location("gt_oracle_live", path)
        if spec and spec.loader:
            mod = _ilu.module_from_spec(spec)
            sys.modules["gt_oracle_live"] = mod
            spec.loader.exec_module(mod)
            _gt_oracle_mod = mod
    except Exception:  # noqa: BLE001 -- correct-or-quiet, never break the loop
        _gt_oracle_mod = None
    return _gt_oracle_mod


def _obligation_nudge_block() -> tuple[float, str] | None:
    """SPEC producer at the live review transition — delivery-engine STAGE 2
    (2026-06-11): the per-obligation STATUS checklist (gt_gt §15.4 Stage 3 /
    DEEP_TRAJECTORY §3.3 fix #1, the #1 lever).

    WHAT: every unmet obligation with its sensed status — `[✓ edited, ✗
    untested]` / `[✗ not addressed]` — issue text verbatim, the covering test
    named per untested obligation (graph.db assertions, FACT-tier only).
    WHEN: every review-transition turn whose status VECTOR is new (the
    content×phase×status dedup; same vector -> suppress, changed -> re-fire).
    TRIGGER (hybrid, 4 composited signals): review transition (>=1 edit +
    >=3 non-edit) ∧ >=1 obligation edited-but-untested (the proven-aimed
    intersection — 3/3 named the hidden verifier's target, LEGITIMACY §1.5)
    ∧ unmet obligations exist ∧ status vector unseen.
    SEVERITY (computed, never a constant): composite_severity(base=5,
    budget_fraction, unmet_ratio) — Zilberstein-form urgency.
    Correct-or-quiet: no sibling module / no obligations / nothing
    edited-untested / vector already delivered -> None.

    Returns (severity, payload) or None."""
    global _oracle_obligation_fired, _oblig_status_last_hash
    om = _load_gt_oracle()
    if om is None:
        return None
    try:
        obls = om.load_obligations(_anchors_path())
        if not obls:
            return None
        tracker = _get_obligation_tracker(om)
        tracker.update(
            _oracle_edited_tokens, _oracle_tested_tokens, _action_count)
        _persist_obligation_status(tracker)
        statuses = tracker.statuses_tuple(
            _oracle_edited_tokens, _oracle_tested_tokens)
        unmet = om.order_unmet(statuses)
        if not unmet:
            return None
        # Precision gate: fire only when >=1 obligation is EDITED-but-untested
        # (the intersection that demonstrably aims at the verifier's target);
        # unaddressed obligations ride along as checklist rows, never trigger
        # alone (edit tokens are a noisy proxy — correct-or-quiet).
        if not any(s == om.OBL_EDITED_UNTESTED for _v, s, _t, _c in unmet):
            return None
        h = om.status_vector_hash(statuses)
        # Near budget end (>80% steps spent), clear the dedup so the obligation
        # gets one final shot even if the status vector hasn't changed — the
        # agent may never have tested, so the hash is the same as the early fire.
        budget_b_now = (_action_count / _GT_STEP_LIMIT) if _GT_STEP_LIMIT else 0.0
        if budget_b_now > 0.80 and h in _oblig_status_emitted:
            _oblig_status_emitted.discard(h)
        if h in _oblig_status_emitted:
            return None
        # ── COVERING-TEST per untested obligation (Stage B query, FACT-tier
        # CALLS edges only). Render all unmet rows; attach commands only when
        # the real graph query returns a covering test and _test_run_command
        # formats it. No fabricated fallback command text.
        covering: dict[int, dict] = {}
        for v, s, touched, _conf in unmet:
            if s != om.OBL_EDITED_UNTESTED:
                continue
            try:
                hits = _covering_tests_for_symbols(set(touched))
                if hits:
                    covering[v.idx] = hits[0]
            except Exception:  # noqa: BLE001 -- enrichment is best-effort
                pass
        payload = om.render_obligation_status_block(statuses, covering)
        if not payload:
            return None
        # production-time latch: delivered-vector set; a gate LOSS releases it
        # (deferred, not destroyed — the established re-arm law).
        _oblig_status_emitted.add(h)
        _oblig_status_last_hash = h
        _oracle_obligation_fired = True
        budget_b_sev = (_action_count / _GT_STEP_LIMIT) if _GT_STEP_LIMIT else 0.0
        unmet_ratio = len(unmet) / max(len(statuses), 1)
        sev = om.composite_severity(_SEV_OBLIGATION, budget_b_sev, unmet_ratio)
        # D3 fix: pre-submit boost uses composite so it BEATS horizon gate.
        # Horizon: composite(_SEV_GATE, budget, tcov) ≈ 7.8-8.9.
        # Obligation: composite(_SEV_GATE+1, budget, unmet) ≈ 9.0+ → always wins.
        if budget_b_sev > 0.90 and unmet:
            sev = om.composite_severity(_SEV_GATE + 1, budget_b_sev, unmet_ratio)
        # Also drop the nonedit_streak requirement in the final 10% — the agent
        # may be editing right up to submit and still needs the checklist.
        return (sev, payload)
    except Exception:  # noqa: BLE001 -- never break the agent loop
        return None


# Kinds the gate suppressed THIS turn as a re-armable loss (outranked /
# irrelevant — NOT 'delivered'): _augment_output releases their producers'
# production-time latches so a one-shot class is DEFERRED to a later turn,
# never destroyed (LIPI 2026-06-10: the first-edit contract permanently ate
# the cochange completeness signal; a sev-tie scope block could permanently
# eat the failure_persisted nudge).
_oracle_last_losers: set[str] = set()
_BLOCK_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# severity ranks (the plan §3.4 WHAT ordering, mirrored from gt_oracle):
#   6 = verification gate (NEW MAX RANK — budget-critical self-verify),
#   5 = issue-verbatim obligation (the un-misdirectable class),
#   4 = verification-gap nudge, 3 = edit-bound contract, 2 = stuck nudge /
#   review-time scope completeness, 1 = code-map narration (evidence/cochange).
_SEV_GATE = 6           # verification horizon GATE band (displaces everything)
_SEV_OBLIGATION = 5
_SEV_NUDGE_VERIFY = 4
_SEV_CONTRACT = 3
_SEV_STUCK = 2
_SEV_SCOPE = 2
_SEV_CODEMAP = 1


# ---------------------------------------------------------------------------
# CP013 — phase detection + policy filter (P5 symbol narrowing).
# ---------------------------------------------------------------------------
_pp_dir = os.path.dirname(os.path.abspath(__file__))
if _pp_dir not in _sys.path:
    _sys.path.insert(0, _pp_dir)
try:
    from phase_policy import (
        PHASE_POLICY as _PHASE_POLICY,
        Event,
        Phase,
        phase_allows as _phase_allows_policy,
        should_emit as _phase_should_emit,
    )
except ImportError:
    # Fallback: no phase filtering — all candidates pass (pre-CP013 behavior)
    import enum as _pp_enum
    class Phase(_pp_enum.Enum):
        ORIENT = "orient"
        VIEW = "view"
        EDIT = "edit"
        VERIFY = "verify"
        SUBMIT = "submit"
    class Event(_pp_enum.Enum):
        TASK_START = "task_start"
        POST_VIEW = "post_view"
        POST_EDIT = "post_edit"
        TEST_RESULT = "test_result"
        REVIEW_TRANSITION = "review_transition"
        PRE_SUBMIT = "pre_submit"
    _PHASE_POLICY = {}
    class _PolicyDecision:
        allowed = True
        reason = "fallback"
    def _phase_allows_policy(kind, phase, policy=None): return True
    def _phase_should_emit(kind, phase, **kw): return _PolicyDecision()


def _detect_phase() -> Phase:
    state = _ProductTrajectoryState(
        action_count=_action_count,
        step_limit=_GT_STEP_LIMIT,
        edited_files=set(_oracle_edited_rels),
        source_edit_count=_source_edit_count,
        nonedit_streak=_oracle_nonedit_streak,
    )
    return _product_derive_phase(state)


def _phase_allows(kind: str, phase: Phase) -> bool:
    return _phase_allows_policy(kind, phase, _PHASE_POLICY)


def _current_event(kind: str) -> Event | None:
    if kind == "post_view":
        return Event.POST_VIEW
    if kind == "post_edit":
        return Event.POST_EDIT
    if _oracle_nonedit_streak >= 3 and _oracle_edited_rels:
        return Event.REVIEW_TRANSITION
    if _detect_phase() == Phase.SUBMIT:
        return Event.PRE_SUBMIT
    return None


# ---------------------------------------------------------------------------
# CP011 — persistent obligation tracker (singleton per anchors file).
# ---------------------------------------------------------------------------
_obligation_tracker = None
_obligation_tracker_anchors: str | None = None


def _get_obligation_tracker(om):
    global _obligation_tracker, _obligation_tracker_anchors
    path = _anchors_path()
    if _obligation_tracker is None or _obligation_tracker_anchors != path:
        obls = om.load_obligations(path)
        _obligation_tracker = om.ObligationTracker(obls)
        _obligation_tracker_anchors = path
    return _obligation_tracker


def _persist_obligation_status(tracker, *, turn: int | None = None) -> None:
    """Write obligation vector to disk + oracle jsonl (P1-18/19)."""
    try:
        import json as _j

        snap = {
            "event": "obligation_status",
            "turn": turn if turn is not None else _action_count,
            "coverage_ratio": float(f"{tracker.coverage_ratio():.8f}"),
            "obligations": tracker.snapshot(),
        }
        path = os.environ.get("GT_OBLIGATION_STATUS", "/tmp/gt/obligation_status.json")
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            _j.dump(snap, fh, indent=2)
            fh.write("\n")
        ev = os.environ.get("GT_ORACLE_EVENTS", "/tmp/gt_oracle_events.jsonl")
        with open(ev, "a", encoding="utf-8") as fh:
            fh.write(_j.dumps(snap) + "\n")
    except Exception:  # noqa: BLE001 -- telemetry must never break the loop
        pass


def _maybe_persist_obligation_status() -> None:
    om = _load_gt_oracle()
    if om is None:
        return
    try:
        obls = om.load_obligations(_anchors_path())
        if not obls:
            return
        tracker = _get_obligation_tracker(om)
        tracker.update(_oracle_edited_tokens, _oracle_tested_tokens, _action_count)
        _persist_obligation_status(tracker)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# CP014 — graph-to-action templates (deterministic, no LLM).
# ---------------------------------------------------------------------------
_ACTION_TEMPLATES = {
    "caller_risk": (
        "Changing {callee} risks breaking {caller} ({file}:{line}). "
        "Inspect before editing."
    ),
    "contract_must": (
        "{symbol} must return {return_type} — {n_callers} callers depend on this."
    ),
    "witness_call": "Inspect {sym} at {loc} before changing related code.",
    "sibling_match": (
        "Sibling pattern nearby: {line}. Your implementation should match."
    ),
}


def _translate_to_action(evidence_block: str, phase: Phase) -> str:
    return _product_translate_to_action(evidence_block, phase)


# ---------------------------------------------------------------------------
# CP015 — context budget trim + cross-turn dedup.
# ---------------------------------------------------------------------------
_DELIVERED_FACTS: set[str] = set()
_DELIVERED_FACT_IDS: set[str] = set()
_PRODUCT_BUDGETER = _ProductContextBudgeter(_DELIVERED_FACTS, _DELIVERED_FACT_IDS)
_FACT_TAG_RE = re.compile(r"\[([A-Z][A-Z0-9_]*)\]")
_IMPERATIVE_PREFIXES = (
    "Changing", "Must", "Check", "Run", "You edited", "Inspect",
    "GT:", "Before",
)


def _stable_fact_id(line: str) -> str:
    """Semantic dedupe key (P1-15) — tag + primary symbol, else content hash."""
    stripped = line.strip()
    if not stripped:
        return ""
    m = _FACT_TAG_RE.search(stripped)
    if m:
        tag = m.group(1)
        rest = stripped[m.end() :].strip()
        sym = re.split(r"\s|→|->|,|\(", rest, maxsplit=1)[0].strip()
        if sym:
            return f"{tag}:{sym.lower()}"
    return hashlib.sha256(stripped.encode("utf-8")).hexdigest()[:16]


_last_budget_pending: list[str] = []


def _budget_trim(payload: str, max_tokens: int = 500) -> str:
    global _last_budget_meta, _last_budget_pending
    _budget_result = _PRODUCT_BUDGETER.trim(payload, max_tokens=max_tokens)
    _last_budget_meta = _budget_result.meta
    _last_budget_pending = _budget_result.pending_lines
    return _budget_result.text


_last_budget_meta: dict = {}
_RUNTIME_LEDGER = _ProductLedger()


def _runtime_ledger_path() -> str:
    return os.environ.get("GT_RUNTIME_LEDGER", "/tmp/gt_runtime_ledger.jsonl")


def _runtime_ledger_flush() -> None:
    try:
        path = _runtime_ledger_path()
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            payload = _RUNTIME_LEDGER.to_jsonl()
            if payload:
                fh.write(payload)
                fh.write("\n")
    except Exception:
        pass


def _runtime_ledger_record(
    *,
    kind: str,
    outcome,
    reason: str = "",
    chars: int = 0,
    file_path: str = "",
    event=None,
) -> None:
    ev = event.value if event is not None else ""
    _RUNTIME_LEDGER.record(
        _ProductLedgerEntry(
            layer=kind,
            event_type=ev,
            file_path=file_path,
            outcome=outcome,
            reason=reason,
            chars_delivered=chars,
            iteration=_action_count,
        )
    )
    _runtime_ledger_flush()


# ---------------------------------------------------------------------------
# Layer-4b FIRE counter (auditability) — how many times each hook (producer)
# FIRED this run, independent of whether it DELIVERED. The runtime ledger above
# records DELIVERED / SUPPRESSED_* outcomes only; an empty correct-or-quiet
# producer is skipped BEFORE any ledger record, so "how many times did
# l3.contract / l3.cochange / l3b.evidence fire?" was previously unanswerable
# from disk. This counts every fire (incl. fired-but-quiet) to a small JSON.
# ---------------------------------------------------------------------------
_HOOK_FIRE_COUNTS: dict[str, int] = {}


def _hook_fire_counts_path() -> str:
    return os.environ.get("GT_HOOK_FIRE_COUNTS", "/tmp/gt_hook_fire_counts.json")


def _record_hook_fire(kind: str) -> None:
    """Count ONE fire of a hook (producer invoked), regardless of delivery outcome.
    Persisted to a JSON {kind: count} so fire counts are answerable from disk."""
    if not kind:
        return
    _HOOK_FIRE_COUNTS[kind] = _HOOK_FIRE_COUNTS.get(kind, 0) + 1
    try:
        import json as _j
        path = _hook_fire_counts_path()
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            _j.dump(_HOOK_FIRE_COUNTS, fh, sort_keys=True)
    except Exception:  # noqa: BLE001 — auditability must never break delivery
        pass


def _filter_candidates_by_phase(cands, phase: Phase, event, *, file_path: str = ""):
    kept = []
    for sev, kind, text, event_bound in cands:
        if not text:
            continue
        decision = _phase_should_emit(
            kind, phase, event=event, event_bound=bool(event_bound)
        )
        if decision.allowed:
            kept.append((sev, kind, text, event_bound))
            continue
        _runtime_ledger_record(
            kind=kind,
            outcome=_ProductSignalOutcome.SUPPRESSED_WRONG_PHASE,
            reason=decision.reason,
            file_path=file_path,
            event=event,
        )
    return kept


# ---------------------------------------------------------------------------
# Piece 3 — runtime_suppression_heuristic (ledger-driven ignore/boost; NOT consumption proof).
# D7 fix: judge consumption from the NEXT action (deferred one turn), not the
# trigger command. Decay ignore counts instead of permanent mute.
# ---------------------------------------------------------------------------
_ledger_consumed_kinds: set[str] = set()
_ledger_ignore_counts: dict[str, int] = {}
_last_delivered_kind: str = ""
_last_gate_winner_kind: str = ""
_pending_delivery: tuple[str, int] | None = None  # (kind, turn) awaiting next-turn judgment


def _ledger_cmd_acted(cmd: str) -> bool:
    """True when the command is an edit or test invocation (consumption signal)."""
    c = (cmd or "").strip()
    if not c:
        return False
    if _TEST_RUNNER_RE.search(c):
        return True
    if _EDIT_KW_RE.search(c):
        return True
    # D7: exclude bare stderr redirects (2>&1) from the redirect heuristic
    return bool(re.search(r"(?<!\d)>>?\s*[^\s/&]", c))


def _ledger_judge_pending(cmd: str) -> None:
    """D7: judge the PREVIOUS delivery from THIS turn's command (one-turn defer)."""
    global _pending_delivery
    if _pending_delivery is None:
        return
    kind, _turn = _pending_delivery
    _pending_delivery = None
    if not kind:
        return
    acted = _ledger_cmd_acted(cmd)
    if acted:
        _ledger_consumed_kinds.add(kind)
        # Consumed → decay ignore count by 1 (the ONLY decay path)
        if kind in _ledger_ignore_counts:
            _ledger_ignore_counts[kind] = max(0, _ledger_ignore_counts[kind] - 1)
    else:
        _ledger_ignore_counts[kind] = _ledger_ignore_counts.get(kind, 0) + 1


def _ledger_note_delivery(kind: str, cmd: str) -> None:
    """Record that kind was delivered; judgment deferred to next turn."""
    global _last_delivered_kind, _pending_delivery
    _last_delivered_kind = kind
    _pending_delivery = (kind, _action_count)


def _ledger_boost_severity(kind: str, sev: float) -> float:
    if kind in _ledger_consumed_kinds:
        return sev + 0.5
    return sev


def _ledger_should_skip_kind(kind: str) -> bool:
    return _ledger_ignore_counts.get(kind, 0) >= 3  # D7: raised from 2 to 3


# ---------------------------------------------------------------------------
# VERIFICATION HORIZON (Stage C — H0 budget sensor + H1 covering-test + H2 gate)
#
# A budget-aware, escalating self-verification candidate class inside the oracle.
# Product value: GT proactively reminds you to test before shipping — any
# developer in Cursor/Claude Code benefits, not just SWE-bench agents.
#
# 4 bands (DORMANT/ADVISORY/URGENT/GATE) keyed to budget_remaining and the
# verification gap (edited > 0 AND tested = 0 for those symbols). Scale-free:
# thresholds are fractions of step_limit, not hardcoded step numbers.
# V (verification cycle cost) is estimated from the agent's own pace or railed.
#
# Research basis: SWE-Next (87.4% budget exhaustion), BATS (budget tracker
# Pareto-dominates budget-blind), TestPrune (+8-13% from targeted selection),
# Zilberstein 1996 (contract-algorithm metareasoning).
# ---------------------------------------------------------------------------

# H0: Budget sensor — reads GT_STEP_LIMIT from env (set by the harness from the
# pier config step_limit). Absent -> class disabled (fail-quiet).
_GT_STEP_LIMIT: int | None = None
_GT_VERIFICATION_CYCLE_COST: int = 25  # default V, railed [8, 40]
_V_MIN = 8
_V_MAX = 40


def _henv(name: str, dflt: float) -> float:
    """Env-overridable horizon threshold (calibration channel, never a secret)."""
    try:
        return float(os.environ.get(name, "") or dflt)
    except (TypeError, ValueError):
        return dflt


def _load_horizon_calibration_defaults() -> dict[str, float]:
    """P0-13: load shipped corpus thresholds; env still wins via _henv."""
    path = os.environ.get(
        "GT_HORIZON_CALIBRATION",
        os.path.join(
            os.path.dirname(__file__), "..", ".claude", "calibration", "horizon_v1.json"
        ),
    )
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        th = data.get("thresholds") or {}
        return {str(k): float(v) for k, v in th.items()}
    except (OSError, ValueError, TypeError):
        return {}


_HORIZON_DEFAULTS = _load_horizon_calibration_defaults()


# H3-CALIBRATION CHANNEL (delivery-engine Stage 4, 2026-06-11 — replaces the
# static B>=0.5/0.8 + 1.5xV bands).  The band edges are functions of the
# BEHAVIORAL signals (test/edit coverage from Stage 1) with the budget as the
# urgency multiplier (Zilberstein 1996; BATS arXiv 2511.17006; SWE-Next:
# 87.4% of failures end by budget exhaustion).  Env-overridable channel.
#
# FIX 5 (2026-06-11 measurement-gate closeout): the defaults are now
# DATA-DERIVED from the 9 frozen oracle trajectories of run 27321848581
# (offline Stage-0 sensor replay, scripts/calibrate_esc_bands.py; receipt
# .claude/reports/metrics/esc_calibration_20260611T071654Z.json), replacing
# the invented 0.4/0.5/0.3/0.7/2.0. n=9 and 0/9 resolved, so the design's
# outcome-stratified quantiles are impossible — per the fallback rule the
# edges are corpus medians/quartiles: **n=9, calibration-quality
# placeholder** (Stage 6, gt_gt §15.4, refines on a grown corpus).
#   ADV_TCOV 0.125       tcov@first-review = 0.0 on 9/9 (universal under-
#                        verification); median floored at 1/8 — a 0.0 edge
#                        makes the strict `tcov < edge` clause a DEAD band
#   ADV_B    0.11166666  p25 budget@first-review — armable by the time most
#                        agents first reach review (corpus B = 0.09-0.20)
#   URG_TCOV 0.125       p25 tcov@first-review, same floor (monotone clamp)
#   URG_B    0.35333333  median budget@LAST-review — the typical final-
#                        review zone
#   GATE_TCOV 1.0        median tcov@last-review: 7/9 reach FULL coverage by
#                        their last review; the gate arms only for below-
#                        typical (still-uncovered) edits
#   GATE_KV  7.48        median (remaining-at-SUBMISSION / V): agents submit
#                        with ~7.5 verification cycles UNUSED — the gate must
#                        arm before the typical submission point (at the old
#                        2.0 the band was unreachable: 8/9 trajectories ended
#                        before R ever dropped to 2V)
_ESC_ADV_TCOV = _henv(
    "GT_ESC_ADV_TCOV", _HORIZON_DEFAULTS.get("GT_ESC_ADV_TCOV", 0.125)
)
_ESC_ADV_B = _henv("GT_ESC_ADV_B", _HORIZON_DEFAULTS.get("GT_ESC_ADV_B", 0.11166666))
_ESC_URG_TCOV = _henv(
    "GT_ESC_URG_TCOV", _HORIZON_DEFAULTS.get("GT_ESC_URG_TCOV", 0.125)
)
_ESC_URG_B = _henv("GT_ESC_URG_B", _HORIZON_DEFAULTS.get("GT_ESC_URG_B", 0.35333333))
_ESC_GATE_TCOV = _henv(
    "GT_ESC_GATE_TCOV", _HORIZON_DEFAULTS.get("GT_ESC_GATE_TCOV", 1.0)
)
_ESC_GATE_KV = _henv("GT_ESC_GATE_KV", _HORIZON_DEFAULTS.get("GT_ESC_GATE_KV", 7.48))

try:
    _raw_step_limit = os.environ.get("GT_STEP_LIMIT", "").strip()
    if _raw_step_limit:
        _GT_STEP_LIMIT = int(_raw_step_limit)
        if _GT_STEP_LIMIT <= 0:
            _GT_STEP_LIMIT = None
except (TypeError, ValueError):
    _GT_STEP_LIMIT = None

# STRUCTURAL EDIT-RISK verification trigger (flag-gated; default OFF so the existing
# horizon behavior is byte-identical until a live witness validates the new path).
# ON: a high-blast-radius edited-but-untested symbol earns a verification nudge from
# WHAT was edited — budget-free, the research white space (see runtime/edit_risk.py).
_STRUCTURAL_RISK_ON = (
    os.environ.get("GT_VERIFY_STRUCTURAL_RISK", "").strip().lower()
    not in ("", "0", "false", "off", "no")
)
try:
    # Fire threshold on the 0..1 risk score. 0.5 = the saturation midpoint, i.e. the
    # edited symbol's verified fan-in is at/above the repo's own notable-dependents
    # baseline (repo-relative, not a caller-count magic number). Env-overridable.
    _RISK_TRIGGER = float(os.environ.get("GT_VERIFY_RISK_TRIGGER", "0.5"))
except (TypeError, ValueError):
    _RISK_TRIGGER = 0.5

try:
    _raw_vcc = os.environ.get("GT_VERIFICATION_CYCLE_COST", "").strip()
    if _raw_vcc:
        _v = int(_raw_vcc)
        _GT_VERIFICATION_CYCLE_COST = max(_V_MIN, min(_V_MAX, _v))
except (TypeError, ValueError):
    pass

# Horizon state (module-global, reset per interpreter = per attempt)
_horizon_advisory_fired = False
# Per-band once-latches (2026-06-11 LIPI replay receipt: fd turns 209-218 got
# TEN consecutive urgent emissions — the band held while R counted down, the
# changing step-count gave every render a new content hash, and urgent/pivot
# had no dose latch. Wink (arXiv 2602.17037): one intervention recovers 90.9%,
# multi-intervention drops to 79% — the ESCALATION LADDER (advisory->urgent->
# gate) is the re-fire mechanism, never within-band repetition. Gate-loss
# re-arm preserved (deferred, not destroyed), same law as advisory.
_horizon_urgent_fired = False
_horizon_pivot_fired = False
_horizon_gate_fire_count = 0
_HORIZON_GATE_CAP = 3  # max fires in GATE band (persistent_until_met cap)
# Observed EDIT->TEST cycle spans (for dynamic V estimation — Stage 4: V is
# the agent's OWN observed pace, "25 is the DEFAULT, not the RULE"). A cycle
# opens at the first source edit after the last observed test result and
# closes at the next observed result.
_cycle_edit_start: int | None = None
_test_cycle_spans: list[int] = []
# FIX 6 (2026-06-11): action indices of every observed source edit — the
# EDIT->EDIT pace proxy for the NEVER-TEST agent (the escalation ladder's
# target population, 2/9 of the frozen corpus), whose empty _test_cycle_spans
# previously made V silently fall back to the static 25.
_edit_action_steps: list[int] = []
# kept for back-compat with older telemetry readers (no longer drives V):
_last_test_step: int | None = None
# RECENCY (design §1.3 pivot: "the MOST RECENT sensed test evidence is a FAIL")
# — _test_fail_history is append-only/never cleared on a later PASS, so it must
# never drive the pivot. This flag tracks the LAST observed test outcome only.
_last_test_outcome_failed: bool = False


def _estimate_v() -> int:
    """Verification cycle cost estimate (in steps).

    1. >= 1 observed EDIT->TEST cycle -> median observed span (the agent's
       own verification pace — always wins when it exists).
    2. FIX 6 (2026-06-11): NEVER-TEST agent with >= 2 source edits -> median
       EDIT->EDIT span (the agent's own working cadence stands in for the
       cost of a verification cycle it has never run). Without this the
       exact population the escalation ladder targets (edits, never tests)
       got the STATIC default — V was inert where it mattered most. Still
       dynamic (the agent's own data), still railed.
    3. Otherwise (no pace signal yet) -> V_DEFAULT.
    Always railed to [V_MIN, V_MAX]."""
    import statistics as _hstats
    if _test_cycle_spans:
        v = int(_hstats.median(_test_cycle_spans))
    elif len(_edit_action_steps) >= 2:
        _paces = [b - a for a, b in
                  zip(_edit_action_steps, _edit_action_steps[1:]) if b > a]
        v = int(_hstats.median(_paces)) if _paces else _GT_VERIFICATION_CYCLE_COST
    else:
        v = _GT_VERIFICATION_CYCLE_COST
    return max(_V_MIN, min(_V_MAX, v))


def verify_horizon_band(action_count: int, step_limit: int | None,
                        v: int, edit_coverage: float | None,
                        test_coverage: float | None, has_edits: bool,
                        last_test_failed: bool = False) -> str | None:
    """The band decision function - delivery-engine STAGE 4 (2026-06-11):
    band edges are functions of the BEHAVIORAL signals, not budget constants.

    Inputs (all Stage-1 sensed, stateless per turn):
      edit_coverage  - obligation symbols edited / total (None = no
                       obligations -> the clause degrades to edit-presence;
                       the obligation-status class owns the obligation story)
      test_coverage  - edited files with test evidence / edited files
                       (None = nothing edited)
      has_edits      - >=1 source edit observed
      last_test_failed - the MOST RECENT observed outcome was a failure

    Bands (each predicate composites >=3 signals - hybrid pillar; thresholds
    are the env-overridable GT_ESC_* calibration channel - dynamic pillar):
      pivot    : last_test_failed + critical zone (B >= urg_B or R < KV*V)
      gate     : R < KV*V + test_coverage < gate_tcov + has_edits
      urgent   : B > urg_B + test_coverage < urg_tcov + has_edits
      advisory : B > adv_B + test_coverage < adv_tcov + edit_coverage > 0
    Returns: "gate" | "urgent" | "advisory" | "pivot" | None (dormant).
    Pure function - no side effects."""
    return _product_verify_horizon_band(
        action_count,
        step_limit,
        v,
        edit_coverage,
        test_coverage,
        has_edits,
        last_test_failed=last_test_failed,
        thresholds=_ProductHorizonThresholds(
            advisory_test_coverage=_ESC_ADV_TCOV,
            advisory_budget=_ESC_ADV_B,
            urgent_test_coverage=_ESC_URG_TCOV,
            urgent_budget=_ESC_URG_B,
            gate_test_coverage=_ESC_GATE_TCOV,
            gate_cycles=_ESC_GATE_KV,
        ),
    )

def _render_verify_emission(band: str, action_count: int, step_limit: int,
                            edited_rels: set, covering_tests: list,
                            risk_note: str = "") -> str:
    """Render an agent-visible verification horizon emission.

    Exact test names, file paths, and single-test commands are intentionally
    not rendered. The graph query may prove that a covering test exists, but
    benchmark-valid guidance must stay at the targeted-verification level.
    ``risk_note`` (optional) names the highest-blast-radius unverified change.
    """
    return _product_render_verify_emission(
        band, action_count, step_limit, edited_rels, covering_tests,
        risk_note=risk_note)

# Obligation SYMBOLS only (the edit_coverage_ratio numerator domain) — from
# the per-task anchors artifact's obligations[], never the anchor superset
# (anchors include file stems/keywords that would dilute the ratio).
_oblig_syms_cache: set[str] | None = None


def _obligation_symbol_set() -> set[str]:
    global _oblig_syms_cache
    if _oblig_syms_cache is None:
        syms: set[str] = set()
        try:
            import json as _j
            with open(_anchors_path(), encoding="utf-8") as f:
                data = _j.load(f)
            for o in data.get("obligations") or []:
                if not isinstance(o, dict):
                    continue
                for s in o.get("symbols") or []:
                    if isinstance(s, str) and len(s) >= 3:
                        syms.add(s)
                        syms.update(p for p in s.split(".") if len(p) >= 3)
        except Exception:  # noqa: BLE001 -- absent artifact -> dormant clause
            pass
        _oblig_syms_cache = syms
    return _oblig_syms_cache


def _structural_risk_note() -> tuple[str, bool]:
    """(risk_note, should_trigger) from the structural edit-risk of the edited-but-
    untested obligation symbols. ('', False) when the flag is off, the import is
    unavailable, or the risk is quiet (correct-or-quiet). The note NAMES the highest-
    blast-radius unverified change; should_trigger is True when that risk is at/above
    the repo's notable-dependents baseline (score >= _RISK_TRIGGER)."""
    if not _STRUCTURAL_RISK_ON or _structural_edit_risk is None:
        return ("", False)
    try:
        er = _structural_edit_risk(_db_path(), _obligation_symbol_set())
    except Exception:  # noqa: BLE001 — risk scoring must never break the producer
        return ("", False)
    if er is None or er.is_quiet():
        return ("", False)
    tr = er.top_reason()
    if tr is None:
        return ("", False)
    note = (f"{tr.name} ({tr.dependents} verified dependent(s) in the graph) — no test "
            f"has exercised your change to it")
    return (note, er.score >= _RISK_TRIGGER)


def _verification_horizon_candidate() -> tuple[float, str, str, bool] | None:
    """Produce the Verification Horizon candidate for this turn — delivery-
    engine STAGE 4: bands from the Stage-1 behavioral signals; V from the
    agent's OWN observed edit->test pace; severity a COMPUTED composite
    (base + 2*budget_fraction + 1*unmet_ratio), never a class constant.

    Returns (severity, kind, block_text, edit_bound) or None.
    Respects: dose caps (advisory/urgent/pivot once each, gate cap-3, all
    gate-loss re-armed)."""
    global _horizon_advisory_fired, _horizon_urgent_fired, \
        _horizon_pivot_fired, _horizon_gate_fire_count

    if _GT_BASELINE:
        return None

    # Stage-1 signals, live mirrors of DerivedState (sensed, never assumed).
    tc = test_coverage_ratio(_oracle_edited_tokens_by_file,
                             _oracle_tested_tokens)
    ec = edit_coverage_ratio(_obligation_symbol_set(), _oracle_edited_tokens)

    # STRUCTURAL EDIT-RISK (flag-gated, budget-free): a high-blast-radius edited-but-
    # untested symbol earns ONE verification advisory from WHAT was edited, regardless
    # of budget/path — the research white space. Shares the advisory latch (once per
    # task; re-armed on gate loss). risk_note also enriches the budget bands below.
    _risk_note, _risk_trigger = _structural_risk_note()
    if _risk_trigger and not _horizon_advisory_fired and _oracle_edited_rels:
        _horizon_advisory_fired = True
        _rk_cov = _covering_tests_for_symbols(_oracle_edited_tokens)
        _rk_block = _render_verify_emission(
            "advisory", _action_count, max(int(_GT_STEP_LIMIT or 0), _action_count, 1),
            _oracle_edited_rels, _rk_cov, risk_note=_risk_note)
        if _rk_block:
            return (float(_SEV_NUDGE_VERIFY), "verify.horizon.advisory", _rk_block, True)

    if _GT_STEP_LIMIT is None:
        # H0 absent (interactive surface — Cursor/Claude Code, no step
        # budget): the COVERAGE GAP alone earns ONE budget-agnostic advisory
        # at the review transition. No budget -> no escalation, ever.
        if (_horizon_advisory_fired or not _oracle_edited_rels
                or _oracle_nonedit_streak < 3
                or tc is None or tc >= _ESC_ADV_TCOV):
            return None
        _horizon_advisory_fired = True
        covering = _covering_tests_for_symbols(_oracle_edited_tokens)
        block = _render_verify_emission(
            "advisory", _action_count, max(_action_count, 1),
            _oracle_edited_rels, covering, risk_note=_risk_note)
        if not block:
            return None
        return (float(_SEV_NUDGE_VERIFY), "verify.horizon.advisory", block, True)

    v = _estimate_v()
    band = verify_horizon_band(
        action_count=_action_count,
        step_limit=_GT_STEP_LIMIT,
        v=v,
        edit_coverage=ec,
        test_coverage=tc,
        has_edits=bool(_oracle_edited_rels),
        # RECENCY: the MOST RECENT sensed test outcome (never "any fail
        # ever" — a recovered-then-passing agent must not get a false pivot).
        last_test_failed=_last_test_outcome_failed,
    )

    if band is None:
        return None

    # Dose caps — once per band; the LADDER escalates, the band never repeats.
    if band == "advisory":
        if _horizon_advisory_fired:
            return None
        _horizon_advisory_fired = True
    elif band == "urgent":
        if _horizon_urgent_fired:
            return None
        _horizon_urgent_fired = True
    elif band == "pivot":
        if _horizon_pivot_fired:
            return None
        _horizon_pivot_fired = True
    elif band == "gate":
        if _horizon_gate_fire_count >= _HORIZON_GATE_CAP:
            return None
        _horizon_gate_fire_count += 1

    # H1: covering-test query for targeting
    covering = _covering_tests_for_symbols(_oracle_edited_tokens)

    # Severity: COMPUTED composite over (base, budget position, unmet mass).
    if band == "gate":
        base = _SEV_GATE
    elif band in ("urgent", "pivot"):
        base = _SEV_OBLIGATION  # 5 — outranks contract
    else:
        base = _SEV_NUDGE_VERIFY  # 4
    budget_b = _action_count / _GT_STEP_LIMIT
    unmet_ratio = (1.0 - float(ec)) if ec is not None else 0.0
    sev = composite_severity(base, budget_b, unmet_ratio)

    block = _render_verify_emission(
        band, _action_count, _GT_STEP_LIMIT, _oracle_edited_rels, covering,
        risk_note=_risk_note)

    if not block:
        return None

    return (sev, f"verify.horizon.{band}", block, True)


def _oracle_focus() -> set[str]:
    """The agent's current focus: issue anchors + obligation symbols (from the
    per-task gt_issue_anchors.json artifact, loaded once) + the stems of files
    the agent has edited.  Empty anchors -> edit-set-only relevance
    (fail-quiet, never fail-loud)."""
    global _oracle_focus_cache
    if _oracle_focus_cache is None:
        toks: set[str] = set()
        try:
            import json as _j
            with open(_anchors_path(), encoding="utf-8") as f:
                data = _j.load(f)
            for key in ("symbols", "title_symbols", "code_symbols",
                        "unresolved_code_symbols"):
                for s in data.get(key) or []:
                    if isinstance(s, str) and len(s) >= 3:
                        toks.add(s)
            for o in data.get("obligations") or []:
                for s in (o.get("symbols") or []) if isinstance(o, dict) else []:
                    if isinstance(s, str):
                        toks.add(s)
                        toks.update(p for p in s.split(".") if len(p) >= 3)
        except Exception:  # noqa: BLE001 -- absent artifact -> edit-set only
            pass
        _oracle_focus_cache = toks
    stems = set()
    for r in _oracle_edited_rels:
        stem = os.path.splitext(os.path.basename(r))[0]
        if len(stem) >= 3:
            stems.add(stem)
    return _oracle_focus_cache | stems


def _oracle_telemetry_write(suppressed, winner) -> None:
    """8-dp suppression telemetry (plan §1.4) — file/stderr side, NEVER the
    agent channel."""
    try:
        import hashlib as _hl
        import json as _j
        if not suppressed and winner is None:
            return
        emitted = None
        if winner is not None:
            _text = str(winner[4] or "")
            _next_action = bool(
                re.search(
                    r"\b(run|open|inspect|check|confirm|verify|test|edit)\b",
                    _text,
                    re.I,
                )
            )
            emitted = {
                "kind": winner[3],
                "confidence": float(f"{float(winner[1]):.8f}"),
                "payload_hash": _hl.sha256(
                    _text.encode("utf-8", errors="replace")
                ).hexdigest()[:16],
                "payload_chars": len(_text),
                "actionable": _next_action,
                "surface": "agent_observation",
            }
        rec = {
            "schema": "gt.oracle_event.v2",
            "emitted": None if winner is None else {
                "kind": winner[3],
                "confidence": float(f"{float(winner[1]):.8f}"),
            },
            "emission": emitted,
            "suppressed": [
                {"kind": k, "reason": r, "confidence": float(f"{float(c):.8f}")}
                for k, r, c in suppressed
            ],
        }
        path = os.environ.get("GT_ORACLE_EVENTS", "/tmp/gt_oracle_events.jsonl")
        with open(path, "a", encoding="utf-8") as f:
            f.write(_j.dumps(rec) + "\n")
    except Exception:  # noqa: BLE001 -- telemetry must never break the loop
        pass


def _oracle_bstate() -> str:
    """The STATE-AWARE dedup key shared by BOTH delivery lanes.

    Hoisted out of _oracle_gate_blocks (LANE-SPLIT 2026-06-13) so Lane A (the
    always-on data plane: l3.contract / l3.cochange / l3b.evidence) and Lane B
    (the oracle steer gate) hash content against the IDENTICAL behavioral-state
    key.  Same text in a different agent state (more edits, more tests, deeper
    into the budget) is a DIFFERENT delivery context -> a new hash -> it
    re-competes.  Cross-lane dedup is correct ONLY if both lanes compute this
    byte-identically; that is the entire point of the single helper."""
    return f"{len(_oracle_edited_rels)}:{len(_oracle_tested_tokens)}:{_action_count // 30}"


def _oracle_content_hash(text: str) -> str:
    """The 8-char content+state hash both lanes register into
    _oracle_delivered_hashes.  Parity with the gate's :3690 computation."""
    import hashlib as _hl
    return _hl.sha256((text + _oracle_bstate()).encode("utf-8")).hexdigest()[:8]


def _oracle_gate_blocks(cands) -> str:
    """The live decision gate over this turn's candidate blocks.

    `cands`: list of (severity_rank, kind, block_text, edit_bound).  Returns
    the SINGLE winning block ('' = silence).  Gates, in order: DEDUP
    (content-hash delivered-set; rearm_on_change falls out naturally — changed
    content has a new hash), RELEVANCE (block tokens ∩ focus, waived for
    edit-bound candidates whose trigger IS the edit), DISTRIBUTION FLOOR
    (median+1*MAD over the triggered pool — parity with gt_oracle.gate_pool;
    a singleton always passes; singletons excluded from suppression),
    RANK/BUDGET (severity desc, then confidence 8dp desc, then kind asc;
    <=1 emission per turn, losers dropped — no queue).

    LIVE/REPLAY PARITY FIX (2026-06-11): the replay oracle applies
    gt_oracle.distribution_floor(); the live gate previously applied NO floor,
    meaning a low-confidence candidate in a multi-candidate pool could win live
    but be suppressed in replay.  Now parity: identical median+MAD floor."""
    import hashlib as _hl
    import statistics as _ostats
    global _oracle_last_losers, _last_gate_winner_kind
    _oracle_last_losers = set()
    focus = _oracle_focus()
    passing: list[tuple[float, float, str, str, str]] = []
    suppressed: list[tuple[str, str, float]] = []
    # Behavioral state key: the dedup is STATE-AWARE, not just content-aware.
    # Same text in a different agent state (more edits, more tests, more files
    # opened, deeper into the budget) is a DIFFERENT delivery context — the
    # agent may now act on it when it previously ignored it.
    # LANE-SPLIT 2026-06-13: computed via the shared _oracle_bstate() helper so
    # Lane A (early data-plane delivery) and Lane B (this gate) hash against the
    # byte-identical state key — cross-lane content-hash dedup depends on it.
    _bstate = _oracle_bstate()
    for sev, kind, text, edit_bound in cands:
        if not text or _ledger_should_skip_kind(kind):
            continue
        sev = _ledger_boost_severity(kind, sev)
        h = _hl.sha256((text + _bstate).encode("utf-8")).hexdigest()[:8]
        if h in _oracle_delivered_hashes:
            suppressed.append((kind, "delivered", 0.0))
            continue
        keys = {t for t in _BLOCK_TOKEN_RE.findall(text) if len(t) >= 3}
        matched = keys & focus
        if not edit_bound and not matched:
            suppressed.append((kind, "irrelevant", 0.0))
            _oracle_last_losers.add(kind)
            continue
        conf = (len(matched) / len(focus)) if focus else (
            1.0 if edit_bound else 0.0)
        passing.append((sev, conf, h, kind, text))
    # ── DISTRIBUTION FLOOR (parity with gt_oracle.distribution_floor) ────
    # median + k*MAD over the triggered pool's confidence values.  A lone
    # candidate always passes (MAD=0, floor=own value, >= passes).  Multi-
    # candidate pools suppress the low tail — the gate scales with the data.
    # k is GT_C1_CONFIDENCE_FLOOR_MAD_MULTIPLIER (default 1.0, parity with
    # gt_oracle.distribution_floor — LIPI 2026-06-11).
    if len(passing) > 1:
        confs = [p[1] for p in passing]
        med = _ostats.median(confs)
        mad = _ostats.median(abs(v - med) for v in confs)
        try:
            _k = float(os.environ.get("GT_C1_CONFIDENCE_FLOOR_MAD_MULTIPLIER", "") or 1.0)
        except (TypeError, ValueError):
            _k = 1.0
        floor = med + (_k * mad)
        floored = []
        for p in passing:
            if p[1] < floor:
                suppressed.append((p[3], "below_floor", p[1]))
                _oracle_last_losers.add(p[3])
            else:
                floored.append(p)
        passing = floored
    winner = None
    if passing:
        passing.sort(key=lambda x: (-x[0], -round(x[1], 8), x[3]))
        winner = passing[0]
        for p in passing[1:]:
            suppressed.append((p[3], "outranked", p[1]))
            _oracle_last_losers.add(p[3])
        _oracle_delivered_hashes.add(winner[2])
        _last_gate_winner_kind = winner[3]
    _oracle_telemetry_write(suppressed, winner)
    return winner[4] if winner else ""


def _consensus_collect(rel: str) -> None:
    """Stage-4 consensus producer: build the scope MEMBERSHIP on first view
    (same `_query_scope` facts) WITHOUT emitting — the per-view scope dump is
    retired; delivery happens at the review transition."""
    global _consensus_fired
    if _consensus_fired:
        return
    _consensus_fired = True
    try:
        _consensus_scope.add(_norm_rel(rel))
        for s in _query_scope(rel):
            _consensus_scope.add(_norm_rel(s))
    except Exception:  # noqa: BLE001
        pass


def _scope_completeness_block() -> str:
    """The K-of-N completeness check at the review transition (§11.4 re-route):
    emit ONLY when the sensed edit set ∩ scope is a STRICT subset of scope AND
    the un-edited members are focus-anchored (correct-or-quiet otherwise)."""
    try:
        if not _consensus_scope:
            return ""
        edited = {_norm_rel(r) for r in _oracle_edited_rels}
        scope = set(_consensus_scope)
        if not (scope & edited):
            return ""  # nothing edited in scope -> no completeness claim
        unedited = sorted(scope - edited)
        if not unedited:
            return ""  # not a strict subset -> scope fully covered
        focus = _oracle_focus()
        anchored = []
        for m in unedited:
            toks = {t for t in _BLOCK_TOKEN_RE.findall(os.path.basename(m))
                    if len(t) >= 3}
            if toks & focus:
                anchored.append(m)
        if not anchored:
            return ""

        def _short(p: str) -> str:
            r = (p or "").replace("\\", "/")
            return "/".join(r.split("/")[-2:]) if "/" in r else r

        lines = [f"- {_short(m)} — in GT scope, not yet edited" for m in anchored[:4]]
        return (
            '\n<gt-scope reason="completeness">\n'
            f"You edited {len(scope & edited)} of {len(scope)} graph-connected "
            "in-scope files. Issue-relevant scope members you have NOT touched:\n"
            + "\n".join(lines)
            + "\nConfirm whether the fix is complete without them before "
            "submitting.\n</gt-scope>"
        )
    except Exception:  # noqa: BLE001
        return ""


# ---------------------------------------------------------------------------
# LANE-SPLIT (2026-06-13) — the data-plane / control-plane bulkhead.
#
# THE HYBRID (Nygard, *Release It!* — bulkhead pattern; data-plane / control-
# plane separation): _augment_output's oracle route used to be ONE monolithic
# collect-then-gate block with a SINGLE delivery site DOWNSTREAM of the gate.
# A crash anywhere upstream of, or inside, the gate returned the function having
# delivered NOTHING — the 0/8 stub-crash failure: the contract was computed and
# queued, but the gate died, so it never reached the agent.
#
# LANE A (data plane, ALWAYS-ON): the contract / consistency / completeness
# producers (l3.contract, l3b.evidence, l3.cochange).  Delivers via its OWN
# correct-or-quiet gate = (content non-empty) AND (content-hash NOT already in
# the shared ledger).  Appends to out['output'] + records to the ledger EARLY,
# each producer in its OWN try/except — a Lane A bug is isolated, never darkens
# the next Lane A block or Lane B.  Does NOT go through _oracle_gate_blocks.
#
# LANE B (control plane, SITUATIONAL): the steers (verify.horizon,
# spec.obligation, detect.loop, detect.coherence, consensus.scope, l5.*).  Goes
# through the oracle stateless gate AFTER Lane A, wrapped in ONE try/except so a
# crash here CANNOT undo Lane A's already-committed delivery.
#
# SHARED LEDGER (coordination point): both lanes write the SAME
# _oracle_delivered_hashes (content+state hash) and the SAME
# _ledger_note_delivery / _runtime_ledger_record.  Cross-lane dedup = a
# content-hash lookup: a contract block (Lane A) and a steer ABOUT the same
# function (Lane B) are DIFFERENT content -> both deliver; a byte-identical
# re-send -> suppressed.
# ---------------------------------------------------------------------------
def _lane_a_deliver(out, cmd, lane_a, *, krel, event) -> None:
    """Deliver the always-on data-plane producers EARLY, before any control-
    plane (Lane B / oracle gate) logic that could raise.

    `lane_a`: list of (kind, text) — the contract/evidence/cochange blocks.
    For EACH, in its OWN try/except (a Lane A bug is isolated):
      * skip if text is empty (correct-or-quiet),
      * compute the SHARED content+state hash (_oracle_content_hash),
      * skip if that hash is already in _oracle_delivered_hashes (cross-lane
        dedup — Lane A vs Lane B byte-identical re-send is suppressed),
      * else append to out['output'], register the hash, and record the
        delivery in BOTH ledgers (_ledger_note_delivery + _runtime_ledger_record).

    NO _oracle_gate_blocks here: Lane A's gate is non-empty AND not-already-in-
    ledger — exactly the spec.  Because these producers are edit_bound, the gate
    waived their relevance anyway; moving them out of the gate only removes their
    rank-competition against higher-severity steers (they no longer LOSE the
    turn to a steer)."""
    for kind, text in lane_a:
        _record_hook_fire(kind)  # count the FIRE before any correct-or-quiet skip
        try:
            if not text:
                continue  # correct-or-quiet: empty producer stays silent
            h = _oracle_content_hash(text)
            if h in _oracle_delivered_hashes:
                # cross-lane / re-send dedup: this exact block in this exact
                # state already reached the agent this run.
                _runtime_ledger_record(
                    kind=kind,
                    outcome=_ProductSignalOutcome.SUPPRESSED_DUPLICATE,
                    reason="delivered",
                    file_path=krel or "",
                    event=event,
                )
                continue
            out["output"] = (out.get("output") or "") + text
            _oracle_delivered_hashes.add(h)
            _ledger_note_delivery(kind, cmd)
            _runtime_ledger_record(
                kind=kind,
                outcome=_ProductSignalOutcome.DELIVERED,
                chars=len(text),
                file_path=krel or "",
                event=event,
            )
            # D1 budget-commit re-wire (risk note #5): l3b.evidence's text was
            # budget-trimmed by _budget_trim, which staged the trimmed lines in
            # _last_budget_pending.  In the old monolith the commit fired only on
            # a gate win; now that l3b.evidence delivers via Lane A (never the
            # gate, so never setting _last_gate_winner_kind), commit the pending
            # budget HERE on the real evidence delivery so the cross-turn budget
            # dedup stays live.  (Lane B later resets _last_budget_pending=[].)
            if kind == "l3b.evidence" and _last_budget_pending:
                try:
                    _PRODUCT_BUDGETER.commit_delivered(_last_budget_pending)
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001 — a Lane A bug is isolated, never
            # blocks the next Lane A block OR Lane B.
            try:
                print(f"[GT_META] lane_a_exception kind={kind}",
                      file=sys.stderr, flush=True)
            except Exception:  # noqa: BLE001
                pass


def _augment_output(action, out) -> None:
    """Append GT evidence to a command's output dict."""
    global _marker_sent, _action_count, _source_edit_count, _cycle_edit_start
    if not isinstance(out, dict):
        return
    try:
        if not _marker_sent:
            # 2026-06-10 (PATH B audit): loader telemetry, NOT agent content —
            # stderr only. It leaked into agent-visible stdout on 10/10 tasks.
            print("[gt-patch:loaded]", file=sys.stderr, flush=True)
            _marker_sent = True
        # STRUCTURED-FIRST (F3): an OH/CodeAct/Anthropic structured editor action
        # carries an editor VERB (create/str_replace/insert) in `command` + the
        # target/content in explicit fields — invisible to the bash parsers. Read
        # the structured channel and normalize to a parser-faithful bash-equivalent
        # so EVERY downstream cmd-consumer (classification, RC5 Signal-1/3, evidence,
        # loop signature) works unchanged. A bash action passes through untouched.
        cmd = _effective_cmd(action)
        _orig_out = out.get("output") or ""  # the command's own output (for failure detect)

        if not _GT_BASELINE and _ORACLE_ROUTE:
            # ---- STAGE-4 ORACLE ROUTING: producers -> candidates -> ONE gate ----
            global _oracle_nonedit_streak, _oracle_review_fired, \
                _oracle_obligation_fired, _last_test_step
            _action_count += 1
            # D7: judge the PREVIOUS delivery from THIS turn's command.
            _ledger_judge_pending(cmd or "")
            # severities are COMPUTED floats (composite_severity) — int-base
            # constants coexist; the gate sorts numerically either way.
            cands: list[tuple[float, str, str, bool]] = []
            # LANE A (data plane, ALWAYS-ON) — the contract / consistency /
            # completeness producers collect HERE (as (kind, text)) and deliver
            # EARLY (pre-gate, pre any Lane B logic that could raise).  Lane B's
            # `cands` stays the steer pool that goes through the oracle gate.
            lane_a: list[tuple[str, str]] = []
            _krel = ""  # bound on post_edit; hardening for the guarded uses below
            _kkind, _kf = _classify(cmd)
            if _kkind == "post_edit" and _kf:
                _source_edit_count += 1
                _kroot = _root()
                _krel = _to_repo_rel(_kf, _kroot)
                _oracle_edited_rels.add(_krel)
                _oracle_nonedit_streak = 0
                # edit EVIDENCE tokens (plan §5.2 "edited?"): the edit command
                # carries the code it writes (sed pattern / heredoc body) —
                # mirrors gt_oracle_sense.DerivedState.edited_tokens exactly.
                # D4 obligation-credit gate: tokens credit an obligation as
                # "edited" ONLY for a real repo-source edit (not /tmp/ staging).
                # The source_edit_count / oracle_edited_rels above already counted
                # the edit ACTION (sensor/governor parity); this gates only CREDIT.
                _edit_toks = {t for t in _BLOCK_TOKEN_RE.findall(cmd or "")
                              if len(t) >= 3}
                # RC5 hybrid edit-credit evidence: Signal 1 (CONTENT body tokens,
                # broadened beyond the command verb) + Signal 3 (touched line
                # ranges from diff hunks / sed addresses). Gated by the SAME
                # repo-source guard as the legacy tokens — scratch/temp/vendor
                # writes never feed obligation credit.
                _body_toks = _edit_body_tokens(cmd or "")
                _line_ranges = _edited_line_ranges(cmd or "")
                if _is_repo_source_path(_kf):
                    _oracle_edited_tokens.update(_edit_toks)
                    _oracle_edited_tokens_by_file.setdefault(
                        _krel, set()).update(_edit_toks)
                    _oracle_edit_content_tokens.update(_body_toks)
                    if _line_ranges:
                        _oracle_edited_lines_by_file.setdefault(
                            _krel, []).extend(_line_ranges)
                _edit_churn[_krel] = _edit_churn.get(_krel, 0) + 1
                # H0 (Stage 4): open an edit->test cycle at the FIRST source
                # edit after the last observed test result.
                if _cycle_edit_start is None:
                    _cycle_edit_start = _action_count
                # FIX 6: record the edit's action index — the EDIT->EDIT pace
                # proxy that keeps V dynamic for the never-test agent.
                _edit_action_steps.append(_action_count)
                _invalidate_on_edit(_krel, _kroot)  # L6 (freshness, not delivery)
                # L3: EDIT-BOUND contract candidates (rearm_on_change via hash).
                # LIVE/REPLAY PARITY FIX (2026-06-11): rearm_on_change — a
                # subsequent edit to the SAME file clears the production latch so
                # a fresh contract can compete. The content-hash dedup in the gate
                # still suppresses an IDENTICAL contract; a CHANGED contract
                # (new file state) has a new hash and re-arms naturally.
                _contract_seen.discard(_krel)
                # LANE A: the edit-bound contract + co-change blocks.  Computed
                # in their OWN try/except inside _lane_a_deliver-adjacent guards
                # so a producer crash can't kill the data plane.  They deliver
                # EARLY (below), NOT through the oracle gate.
                try:
                    _la_contract = _graph_contract_block(_krel)
                except Exception:  # noqa: BLE001 — Lane A producer isolated
                    print("[GT_META] producer_exception kind=l3.contract",
                          file=sys.stderr, flush=True)
                    _la_contract = ""
                try:
                    _la_cochange = _cochange_block(_krel)
                except Exception:  # noqa: BLE001 — Lane A producer isolated
                    print("[GT_META] producer_exception kind=l3.cochange",
                          file=sys.stderr, flush=True)
                    _la_cochange = ""
                lane_a.append(("l3.contract", _la_contract))
                lane_a.append(("l3.cochange", _la_cochange))
            else:
                if _oracle_edited_rels:
                    _oracle_nonedit_streak += 1
            if _kkind == "post_view" and _kf:
                _croot = _root()
                _crel = _to_repo_rel(_kf, _croot)
                # consensus: collect scope membership QUIETLY (re-routed).
                _consensus_collect(_crel)
            # tested EVIDENCE tokens (plan §5.2 "tested?"): a real test-runner
            # invocation whose output carries an observed pass/fail result —
            # mirrors gt_oracle_sense.DerivedState.tested_tokens (tokens of the
            # observed output AND the test command itself).
            if _TEST_RUNNER_RE.search(cmd or "") and (
                    _TEST_FAIL_RE.search(_orig_out)
                    or _TEST_PASS_RE.search(_orig_out)):
                _oracle_tested_tokens.update(
                    t for t in _BLOCK_TOKEN_RE.findall(_orig_out) if len(t) >= 3)
                _oracle_tested_tokens.update(
                    t for t in _BLOCK_TOKEN_RE.findall(cmd or "") if len(t) >= 3)
                # H0 (Stage 4): V = the agent's OWN observed EDIT->TEST pace.
                # A cycle opened at the first source edit after the last
                # result closes here; its span feeds the V median.
                global _last_test_step, _last_test_outcome_failed
                if _cycle_edit_start is not None:
                    span = _action_count - _cycle_edit_start
                    if span > 0:
                        _test_cycle_spans.append(span)
                    _cycle_edit_start = None
                _last_test_step = _action_count
                # RECENCY: pivot keys on the MOST RECENT outcome (env-shaped
                # failures never count — the failure_persisted discipline).
                # Stage 5: zero-count-safe via _failure_lines — a green
                # `… 0 failed …` line is a PASS, never a pivot trigger.
                _last_test_outcome_failed = bool(
                    _failure_lines(_orig_out)
                    and not _ENV_FAIL_RE.search(_orig_out))
                # Stage-1 coherence reset (mirrors the sensor): an observed
                # PASSING result clears per-file churn — re-edits after this
                # are a new cycle, not thrash.
                if _TEST_PASS_RE.search(_orig_out):
                    _edit_churn.clear()
            # L3b: evidence candidate (view/edit keyed) — RELEVANCE-gated, but
            # the view/edit event bounds relevance (§15.3 VIEW policy): when the
            # trigger IS a resolved post_view/post_edit, waive the empty-focus
            # irrelevant suppression — same contract as edit-bound contract/cochange.
            try:
                _ev_text = _evidence(cmd)
            except Exception:  # noqa: BLE001 — one producer must not kill the gate
                print("[GT_META] producer_exception kind=l3b.evidence",
                      file=sys.stderr, flush=True)
                _ev_text = ""
            _ev_event_bound = bool(
                _kkind in ("post_view", "post_edit") and _kf and _ev_text)
            # LANE A: l3b.evidence (the resolved-witness / caller-contract /
            # sibling code-map) is always-needed consistency context.  It joins
            # the data plane, NOT the steer gate.  (event_bound is irrelevant
            # for Lane A — its gate is non-empty AND not-already-in-ledger; an
            # empty _ev_text is dropped by the correct-or-quiet check.)
            lane_a.append(("l3b.evidence", _ev_text))
            # ── DELIVER LANE A NOW — EARLY, isolated, BEFORE any Lane B logic ──
            # THE NON-NEGOTIABLE ORDERING (the entire point of the bulkhead):
            # the contract/evidence/cochange reach the agent here, each in its
            # own try/except, using the SHARED ledger (_oracle_delivered_hashes
            # content+state dedup + _ledger_note_delivery + _runtime_ledger_record).
            # So a later Lane B / gate crash loses ONLY the steer — never the
            # data-plane context (reproduces+fixes the 0/8 stub-crash).
            _event = _current_event(_kkind)
            _lane_a_deliver(out, cmd, lane_a, krel=(_krel or _kf or ""),
                            event=_event)
            # ── LANE B (control plane, SITUATIONAL) — the steer pool ──────
            # THE ROBUSTNESS GUARANTEE: this whole section (steer producers +
            # phase filter + oracle gate + latch re-arm + winner append) is
            # wrapped in ONE try/except so a crash in _filter_candidates_by_phase
            # or _oracle_gate_blocks (the 0/8 stub-crash failure mode) CANNOT undo
            # Lane A's already-committed data-plane delivery above.  Each steer
            # producer keeps its OWN inner try/except for graceful degradation;
            # this outer guard protects the gate/filter/latch glue between them.
            try:
                # consensus K-of-N completeness at the LIVE review transition.
                # The latch is consumed only on a NON-EMPTY production (LIPI
                # 2026-06-10: an empty block at the first transition permanently
                # blocked a later, non-empty completeness check).
                if (_oracle_edited_rels and _oracle_nonedit_streak >= 3
                        and not _oracle_review_fired):
                    try:
                        _scb = _scope_completeness_block()
                    except Exception:  # noqa: BLE001 — one producer must not kill the gate
                        print("[GT_META] producer_exception kind=consensus.scope",
                              file=sys.stderr, flush=True)
                        _scb = None
                    if _scb:
                        _oracle_review_fired = True
                        cands.append((_SEV_SCOPE, "consensus.scope", _scb, True))
                # SPEC obligations at the SAME review-transition predicate (the
                # proven GT_VERIFY shape).  Delivery-engine STAGE 2: no once-per-
                # task latch — the producer's status-VECTOR dedup is the dose
                # governor (same vector suppressed, changed vector re-fires; the
                # DEEP_TRAJECTORY "review transition silent in 9/9" gap closed).
                # edit_bound=True: the trigger IS the review event and the
                # candidate requires >=1 edited-untested obligation by construction.
                # D3 fix: at >90% budget, produce obligation on ANY turn with edits
                # (drop nonedit_streak requirement — agent may edit up to submit).
                _budget_now = (_action_count / _GT_STEP_LIMIT) if _GT_STEP_LIMIT else 0.0
                _oblig_gate = (_oracle_edited_rels and (
                    _oracle_nonedit_streak >= 3 or _budget_now > 0.90))
                if _oblig_gate:
                    try:
                        _ob = _obligation_nudge_block()
                    except Exception:  # noqa: BLE001 — one producer must not kill the gate
                        print("[GT_META] producer_exception kind=spec.obligation",
                              file=sys.stderr, flush=True)
                        _ob = None
                    if _ob is not None:
                        cands.append((_ob[0], "spec.obligation", _ob[1], True))
                # L5 nudges: premise-sensed event candidates (latches unchanged).
                # Stage 3: loop_arm=False — detect.loop owns loops on this route.
                # FIX 1 (2026-06-11): scaffold_arm=False — scaffold_trap RETIRED on
                # the oracle route (early-patch-intensity rho=-0.78: never penalize
                # exploration volume; 7/9 fires, 0 consumed, 1 wrong steer).
                _maybe_persist_obligation_status()
                try:
                    _l5s = _l5_nudge(cmd, _orig_out, loop_arm=False,
                                     scaffold_arm=False)
                except Exception:  # noqa: BLE001 — one producer must not kill the gate
                    print("[GT_META] producer_exception kind=l5.stuck",
                          file=sys.stderr, flush=True)
                    _l5s = ""
                cands.append((_SEV_STUCK, "l5.stuck", _l5s, True))
                try:
                    _l5f = _l5_failure_nudge(cmd, _orig_out)
                except Exception:  # noqa: BLE001 — one producer must not kill the gate
                    print("[GT_META] producer_exception kind=l5.failure",
                          file=sys.stderr, flush=True)
                    _l5f = ""
                cands.append((_SEV_STUCK, "l5.failure", _l5f, True))
                try:
                    _l5nt = _l5_no_test_evidence_nudge(cmd, _orig_out)
                except Exception:  # noqa: BLE001 — one producer must not kill the gate
                    print("[GT_META] producer_exception kind=l5.no_test",
                          file=sys.stderr, flush=True)
                    _l5nt = ""
                cands.append((_SEV_NUDGE_VERIFY, "l5.no_test", _l5nt, True))
                # DELIVERY-ENGINE STAGE 3 — behavioral detectors over the Stage-1
                # signals (TIDE degenerate loop; TRAJEVAL coherence collapse).
                try:
                    _dl = _degenerate_loop_candidate(cmd, _orig_out)
                except Exception:  # noqa: BLE001 — one producer must not kill the gate
                    print("[GT_META] producer_exception kind=detect.loop",
                          file=sys.stderr, flush=True)
                    _dl = None
                if _dl is not None:
                    cands.append((_dl[0], "detect.loop", _dl[1], True))
                if _kkind == "post_edit" and _kf:
                    try:
                        _cc = _coherence_collapse_candidate(_krel)
                    except Exception:  # noqa: BLE001 — one producer must not kill the gate
                        print("[GT_META] producer_exception kind=detect.coherence",
                              file=sys.stderr, flush=True)
                        _cc = None
                    if _cc is not None:
                        cands.append((_cc[0], "detect.coherence", _cc[1], True))
                # VERIFICATION HORIZON (Stage C H2): budget-aware self-verify candidate
                try:
                    _vh = _verification_horizon_candidate()
                except Exception:  # noqa: BLE001 — one producer must not kill the gate
                    print("[GT_META] producer_exception kind=verify.horizon",
                          file=sys.stderr, flush=True)
                    _vh = None
                if _vh is not None:
                    cands.append(_vh)
                _phase = _detect_phase()
                # Event-bound candidates bypass phase filter — the trigger IS the
                # event (post_view / post_edit / review transition); phase policy
                # only narrows ambient producers (P5 symbol narrowing).
                _event = _current_event(_kkind)
                cands = _filter_candidates_by_phase(
                    cands, _phase, _event, file_path=_krel or _kf or ""
                )
                _win = _oracle_gate_blocks(cands)
                # D1 fix: commit budget dedup ONLY when l3b.evidence wins the gate.
                global _last_budget_pending
                if _win and _last_budget_pending and _last_gate_winner_kind == "l3b.evidence":
                    _PRODUCT_BUDGETER.commit_delivered(_last_budget_pending)
                _last_budget_pending = []
                # Latch re-arm (LIPI 2026-06-10): a produced-but-not-emitted
                # candidate is DEFERRED, not destroyed — gate losers release the
                # production-time latches their producers consumed, so the class
                # re-competes at a later turn ("consume the one-shot only on a
                # REAL emit" — the cochange producer's own stated contract).
                _lost = _oracle_last_losers
                if _lost:
                    global _cochange_fired, _l5_fired, _l5_failure_fired, \
                        _l5_notest_fired, _horizon_advisory_fired, \
                        _horizon_urgent_fired, _horizon_pivot_fired, \
                        _horizon_gate_fire_count, _detect_loop_fired
                    if "l3.contract" in _lost and _kkind == "post_edit" and _kf:
                        _contract_seen.discard(_krel)
                    if "l3.cochange" in _lost:
                        _cochange_fired = False
                    if "l3b.evidence" in _lost and _kkind and _kf:
                        _seen.discard((_kkind, _to_repo_rel(_kf, _root())))
                    if "consensus.scope" in _lost:
                        _oracle_review_fired = False
                    if "spec.obligation" in _lost:
                        # release THIS vector so the same status re-competes at a
                        # later review turn (deferred, not destroyed).
                        _oracle_obligation_fired = False
                        if _oblig_status_last_hash:
                            _oblig_status_emitted.discard(_oblig_status_last_hash)
                    if "l5.stuck" in _lost:
                        _l5_fired = False
                    if "l5.failure" in _lost:
                        _l5_failure_fired = False
                    if "l5.no_test" in _lost:
                        _l5_notest_fired = False
                    # Stage-3 detector latches: deferred, never destroyed.
                    if "detect.loop" in _lost:
                        _detect_loop_fired = False
                    if "detect.coherence" in _lost and _coherence_last_rel:
                        _coherence_fired_files.discard(_coherence_last_rel)
                    # Horizon latches consumed at PRODUCTION re-arm on a gate loss
                    # (deferred, not destroyed — same law as every class above).
                    if "verify.horizon.advisory" in _lost:
                        _horizon_advisory_fired = False
                    if "verify.horizon.urgent" in _lost:
                        _horizon_urgent_fired = False
                    if "verify.horizon.pivot" in _lost:
                        _horizon_pivot_fired = False
                    if "verify.horizon.gate" in _lost:
                        _horizon_gate_fire_count = max(
                            0, _horizon_gate_fire_count - 1)
                if _win:
                    out["output"] = (out.get("output") or "") + _win
                    _ledger_note_delivery(_last_gate_winner_kind, cmd)
                    _runtime_ledger_record(
                        kind=_last_gate_winner_kind,
                        outcome=_ProductSignalOutcome.DELIVERED,
                        chars=len(_win),
                        file_path=_krel or _kf or "",
                        event=_event,
                    )
            except Exception:  # noqa: BLE001 — Lane B (control plane) is fully
                # isolated: a steer/gate/filter crash here loses ONLY the steer,
                # never Lane A's data plane (already delivered above).
                try:
                    import traceback as _tb_b
                    print("[GT_META] lane_b_exception=true\n" + _tb_b.format_exc(),
                          file=sys.stderr, flush=True)
                except Exception:  # noqa: BLE001
                    pass
            return

        # ---- LEGACY PATH (GT_ORACLE_ROUTE=0): unconditional appends ----
        # L5/L6 bookkeeping: count actions, track source edits, refresh on edit.
        if not _GT_BASELINE:
            _action_count += 1
            _kkind, _kf = _classify(cmd)
            if _kkind == "post_edit" and _kf:
                _source_edit_count += 1
                _kroot = _root()
                _krel = _to_repo_rel(_kf, _kroot)
                _invalidate_on_edit(_krel, _kroot)  # L6
                _gc = _graph_contract_block(_krel)  # cross-language [SIGNATURE]/[CALLERS]
                if _gc:
                    out["output"] = (out.get("output") or "") + _gc
                _cc = _cochange_block(_krel)  # COMPLETENESS / co-change
                if _cc:
                    out["output"] = (out.get("output") or "") + _cc
        # CONSENSUS (Layer-A first-view + Layer-B progressive/override): same role as
        # the OH wrapper's <gt-scope> — first view builds scope; later views reinforce
        # in-scope or re-anchor on divergence.
        if not _GT_BASELINE:
            _ckind, _cf = _classify(cmd)
            if _ckind == "post_view" and _cf:
                _croot = _root()
                _crel = _to_repo_rel(_cf, _croot)
                _cons = _consensus_block(_crel, _croot) if not _consensus_fired \
                    else _consensus_progressive(_crel)
                if _cons:
                    out["output"] = (out.get("output") or "") + _cons
        # L5 stuck-detection: scaffold/loop (once) + hypothesis-falsified (once).
        if not _GT_BASELINE:
            _nudge = _l5_nudge(cmd, _orig_out)
            if _nudge:
                out["output"] = (out.get("output") or "") + _nudge
            _fn = _l5_failure_nudge(cmd, _orig_out)
            if _fn:
                out["output"] = (out.get("output") or "") + _fn
            _nt = _l5_no_test_evidence_nudge(cmd, _orig_out)
            if _nt:
                out["output"] = (out.get("output") or "") + _nt
        ev = _evidence(cmd)
        if ev:
            out["output"] = (out.get("output") or "") + ev
    except Exception:  # noqa: BLE001 -- never break the agent loop
        # LOUD-but-safe: surface the swallowed augment exception to stderr (the
        # [gt-patch:loaded] discipline) so a silently-dying gate/producer is
        # diagnosable, while NEVER re-raising (the agent loop must not break).
        try:
            import sys as _sys, traceback as _tb
            print("[GT_META] augment_output_exception=true\n" + _tb.format_exc(),
                  file=_sys.stderr, flush=True)
        except Exception:
            pass


def _wrap_execute(orig):
    def execute(self, action, *args, **kwargs):
        out = orig(self, action, *args, **kwargs)
        _augment_output(action, out)
        return out

    return execute


# Patch the ENVIRONMENT classes, not agent classes.  Every agent type
# (DefaultAgent, InteractiveAgent, ProgressTrackingAgent) calls
# self.env.execute(action), so wrapping env.execute is agent-class-agnostic.
_ENV_CLASSES = [
    ("minisweagent.environments.local", "LocalEnvironment"),
    ("minisweagent.environments.docker", "DockerEnvironment"),
    ("minisweagent.environments.singularity", "SingularityEnvironment"),
]


def _install() -> None:
    if _GT_BASELINE:
        return
    import importlib

    for modname, clsname in _ENV_CLASSES:
        try:
            cls = getattr(importlib.import_module(modname), clsname)
        except Exception:  # noqa: BLE001 -- env class not in this install
            continue
        if getattr(cls, "_gt_patched", False):
            continue
        try:
            cls.execute = _wrap_execute(cls.execute)
            cls._gt_patched = True
        except Exception:  # noqa: BLE001
            pass


_install()
