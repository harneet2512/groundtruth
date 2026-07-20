"""GT Gateway kernel (G1 + G2-pure) — Stage-1 deterministic acceptance.

TTD: every test names the invariant it pins. The golden search-normalization set
is a byte-for-byte re-derivation of ``artifact_deepswe/tests/test_post_search.py``
(the semantics the kernel must replicate WITHOUT importing gt_mini_patch — importing
it executes monkeypatches). Classifier-state and producer tests run on a real sqlite
graph + a real repo tree (never a mock), assert the EXACT conservative behaviour, and
pin the leak law (a test file path/name never surfaces) + the need gate (dedup).

Corpus: tests/fixtures/gateway_corpus/*.jsonl (real mini-swe-agent observations).
"""
from __future__ import annotations

import glob
import json
import os
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from groundtruth.runtime.gateway import (
    CoveringResult,
    GatewayState,
    ToolEvent,
    augment,
    classify_command,
    classify_outcome,
    normalize_search,
    search_pattern,
    # outcome-state constants
    AMBIGUOUS_HIT,
    EXACT_HIT,
    FLOOD,
    TRACE_HIT,
    WRONG_SURFACE,
    ZERO_ABSENT,
    ZERO_BEHAVIOR,
    ZERO_NAME,
)

_CORPUS = Path(__file__).resolve().parents[1] / "fixtures" / "gateway_corpus"


# ---------------------------------------------------------------------------
# GOLDEN: search-command normalization (re-derived from test_post_search.py).
# Reverting the bare-symbol gate or a value-flag entry reddens a row here.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("cmd,want", [
    ("grep -rn set_fields .", "set_fields"),
    ("grep -rIn --include=*.py set_fields src/", "set_fields"),
    ("rg parse_config", "parse_config"),
    ("grep -e Eval .", "Eval"),
    ("grep -A 3 handle_request .", "handle_request"),   # -A consumes '3'
    ("rg -t rust Widget", "Widget"),                    # -t consumes 'rust'
    ("grep -r --exclude-dir tests MyFunc .", "MyFunc"),  # --exclude-dir consumes 'tests'
    ("grep --regexp=verify src", "verify"),             # --regexp=VALUE carries the pattern
    ("grep --include=*.py set_fields src", "set_fields"),
    ("grep -rn -- foo .", "foo"),                        # after --, next token is the pattern
    ("grep -rn -- -foo utils", None),                    # after --, '-foo' not a bare symbol
    ("grep -E TimeoutError utils", "TimeoutError"),      # -E NOT value-taking
    ("grep -rn -E get_user src", "get_user"),            # -E must not promote 'src'
    ("grep -T bar .", "bar"),                            # -T (--initial-tab) valueless
    ('rg "def foo"', None),          # multi-word regex -> abstain
    ("grep -rn a .", None),          # <3 chars -> abstain
    ("grep -rn ab .", None),         # 2 chars -> abstain
    ("cat foo.py", None),            # not a grep
    ("ls -la src/", None),           # not a search
    ("grep -rn 'a.*b' .", None),     # regex metachars -> abstain
    ("grep -rn user/config .", None),  # path -> abstain
])
def test_golden_search_pattern(cmd, want):
    assert search_pattern(cmd) == want


def test_normalize_search_returns_scope_and_pattern():
    q = normalize_search("grep -rn set_fields src/")
    assert q is not None
    assert q.pattern == "set_fields"
    assert "src/" in q.scope


def test_normalize_search_none_on_non_grep():
    assert normalize_search("ls -la") is None


# ---------------------------------------------------------------------------
# Command-kind classifier (search / view / edit / test / submit / other).
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("cmd,kind", [
    ("cd /app && grep -n get_data aiogram/fsm/scene.py", "search"),
    ("find . -name context.py", "search"),
    ("cat -n aiogram/fsm/context.py", "view"),
    ("sed -n '1,40p' foo.py", "view"),
    ("sed -i 's/a/b/' evaluator/functions.go", "edit"),
    ("cat <<'EOF' > foo.py\nx=1\nEOF", "edit"),
    ("cd /app && go test ./evaluator/ -v 2>&1 | tail -20", "test"),
    ("python -m pytest tests/test_x.py -q", "test"),
    ('echo "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT" && cat /tmp/patch.txt', "submit"),
    ("cd /app && ls -la", "other"),
    # precedence: a test-run piped to grep is a TEST, not a search
    ("go test ./... 2>&1 | grep -E 'PASS|FAIL'", "test"),
])
def test_classify_command(cmd, kind):
    assert classify_command(cmd) == kind


def test_corpus_kinds_match_recorded_labels():
    """The kernel's classifier agrees with the corpus provenance labels on the
    core kinds (search/view/edit/test/submit) — the corpus is the ground truth."""
    seen = set()
    checked = 0
    for fp in sorted(glob.glob(str(_CORPUS / "*.jsonl"))):
        for ln in open(fp, encoding="utf-8"):
            rec = json.loads(ln)
            if "__meta__" in rec:
                continue
            seen.add(rec["kind"])
            if rec["kind"] in ("search", "view", "test", "submit"):
                # edit/other have looser boundaries (a heredoc write vs a plain cat);
                # the four here are unambiguous shapes.
                assert classify_command(rec["command"]) == rec["kind"], rec["command"]
                checked += 1
    assert {"search", "view", "edit", "test", "submit"} <= seen
    assert checked >= 20


# ---------------------------------------------------------------------------
# Synthetic graph + repo fixtures (hermetic; no ONNX, no checkout).
# ---------------------------------------------------------------------------
def _mk_graph(tmp_path, nodes, edges, *, content_fts=None):
    db = str(tmp_path / "graph.db")
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE nodes(id INTEGER PRIMARY KEY, label TEXT, name TEXT,"
        " qualified_name TEXT, file_path TEXT, start_line INTEGER, end_line INTEGER,"
        " signature TEXT, return_type TEXT, is_exported INTEGER, is_test INTEGER,"
        " language TEXT, parent_id INTEGER);"
        "CREATE TABLE edges(id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER,"
        " type TEXT, source_line INTEGER, source_file TEXT, resolution_method TEXT,"
        " confidence REAL, metadata TEXT);"
    )
    for n in nodes:
        con.execute(
            "INSERT INTO nodes(id,label,name,file_path,start_line,end_line,is_test,language)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (n["id"], n.get("label", "Function"), n["name"], n["file_path"],
             n.get("start_line", 1), n.get("end_line", 5),
             n.get("is_test", 0), n.get("language", "python")))
    for e in edges:
        con.execute(
            "INSERT INTO edges(id,source_id,target_id,type,source_line,resolution_method,"
            "confidence,metadata) VALUES(?,?,?,?,?,?,?,?)",
            (e["id"], e["source_id"], e["target_id"], e.get("type", "CALLS"),
             e.get("source_line", 1), e.get("resolution_method", "import"),
             e.get("confidence", 1.0), e.get("metadata")))
    if content_fts:
        con.execute("CREATE VIRTUAL TABLE symbol_content_fts USING fts5(body)")
        for rowid, body in content_fts:
            con.execute("INSERT INTO symbol_content_fts(rowid, body) VALUES(?,?)",
                        (rowid, body))
    con.commit()
    con.close()
    return db


def _state(tmp_path, db, **kw):
    return GatewayState(graph_db=db, repo_root=str(tmp_path), **kw)


def _ev(kind, command="", output="", **kw):
    return ToolEvent(kind=kind, command=command, output=output, **kw)


@pytest.fixture(autouse=True)
def _gateway_on(monkeypatch):
    monkeypatch.setenv("GT_GATEWAY", "1")
    yield


# ---------------------------------------------------------------------------
# classify_outcome — one fixture per state.
# ---------------------------------------------------------------------------
def test_exact_hit_is_silent(tmp_path):
    """A bare-symbol grep with hits that resolve to the ONE def -> EXACT_HIT ->
    augment stays silent (non-re-acquisition: the agent already has it)."""
    db = _mk_graph(tmp_path,
                   [{"id": 1, "name": "set_fields", "file_path": "pkg/importer.py",
                     "start_line": 42}],
                   [])
    ev = _ev("search", "grep -rn set_fields .", "pkg/importer.py:42:    def set_fields(self):")
    st = _state(tmp_path, db)
    assert classify_outcome(ev, st) == EXACT_HIT
    assert augment(ev, _state(tmp_path, db)) == []


def test_ambiguous_hit_partitions_def_ref(tmp_path):
    """Same name defined in 2 files + a hit -> AMBIGUOUS_HIT -> a def/ref partition
    Addition (VERIFIED) whose target is a NON-test def path."""
    db = _mk_graph(tmp_path, [
        {"id": 1, "name": "run", "file_path": "a/x.py", "start_line": 10},
        {"id": 2, "name": "run", "file_path": "b/y.py", "start_line": 20},
    ], [])
    ev = _ev("search", "grep -rn run .", "a/x.py:10: def run():\nb/y.py:20: def run():")
    st = _state(tmp_path, db)
    assert classify_outcome(ev, st) == AMBIGUOUS_HIT
    adds = augment(ev, _state(tmp_path, db))
    assert adds and adds[0].evidence_type == "def_ref_partition"
    assert adds[0].tier == "VERIFIED"
    assert any(t.endswith(".py") for t in [adds[0].target])


def test_ambiguous_hit_carries_receiver_type_provenance(tmp_path):
    """When a FACT-tier caller edge carries `receiver_type=` in metadata (W-B),
    the partition Addition surfaces it as provenance; absent -> silent degrade."""
    db = _mk_graph(tmp_path, [
        {"id": 1, "name": "handle", "file_path": "a/svc.py", "start_line": 10},
        {"id": 2, "name": "handle", "file_path": "b/svc.py", "start_line": 20},
        {"id": 3, "name": "caller", "file_path": "app/main.py", "start_line": 5},
    ], [
        {"id": 1, "source_id": 3, "target_id": 1, "resolution_method": "type_flow",
         "confidence": 0.95, "metadata": "receiver_type=Service;dataflow=x"},
    ])
    ev = _ev("search", "grep -rn handle .", "a/svc.py:10: def handle():\nb/svc.py:20: def handle():")
    adds = augment(ev, _state(tmp_path, db))
    body = "\n".join(adds[0].payload)
    assert "Service" in body  # receiver_type provenance surfaced


def test_wrong_surface_when_all_hits_are_tests(tmp_path):
    """grep succeeded but every hit path is test-shaped; the real def is elsewhere
    -> WRONG_SURFACE -> point at the non-test def."""
    db = _mk_graph(tmp_path, [
        {"id": 1, "name": "widget", "file_path": "src/widget.py", "start_line": 7},
    ], [])
    out = "tests/test_widget.py:3: widget()\ntests/test_more.py:9: widget()"
    ev = _ev("search", "grep -rn widget .", out)
    st = _state(tmp_path, db)
    assert classify_outcome(ev, st) == WRONG_SURFACE
    adds = augment(ev, _state(tmp_path, db))
    assert adds and "src/widget.py" == adds[0].target
    assert "test_widget" not in "\n".join(adds[0].payload)


def test_zero_name_fold_variant(tmp_path):
    """Empty grep for getUser, indexed as get_user -> ZERO_NAME -> the fold variant
    def surfaces with an honest 'indexed as' note."""
    db = _mk_graph(tmp_path, [
        {"id": 1, "name": "get_user", "file_path": "app/models.py", "start_line": 11},
    ], [])
    ev = _ev("search", "grep -rn getUser .", "")
    st = _state(tmp_path, db)
    assert classify_outcome(ev, st) == ZERO_NAME
    adds = augment(ev, _state(tmp_path, db))
    assert adds and adds[0].target == "app/models.py"
    assert "get_user" in "\n".join(adds[0].payload)


def test_zero_behavior_body_only(tmp_path):
    """Empty grep, no name/path match, but the concept is in a function BODY
    (content FTS) -> ZERO_BEHAVIOR -> enclosing-symbol facts."""
    db = _mk_graph(tmp_path, [
        {"id": 1, "name": "process", "file_path": "core/engine.py", "start_line": 30},
    ], [], content_fts=[(1, "def process(): retry the redis tls handshake here")])
    ev = _ev("search", "grep -rn handshake .", "")
    st = _state(tmp_path, db)
    assert classify_outcome(ev, st) == ZERO_BEHAVIOR
    adds = augment(ev, _state(tmp_path, db))
    assert adds and "core/engine.py" in "\n".join(adds[0].payload)


def test_zero_absent_repeat_fires_change_surface(tmp_path, monkeypatch):
    """True absence: name+path+body all miss. SILENT on the first probe; on a
    REPEAT with no intervening edit -> ZERO_ABSENT -> W-A change_surface (HYPOTHESIS).
    Honors BOTH GT_GATEWAY and GT_CHANGE_SURFACE."""
    monkeypatch.setenv("GT_CHANGE_SURFACE", "1")
    # a sibling convention: providers/aws.py + providers/gcp.py + a registry
    (tmp_path / "providers").mkdir()
    (tmp_path / "providers" / "aws.py").write_text("class AwsProvider:\n    pass\n")
    (tmp_path / "providers" / "gcp.py").write_text("class GcpProvider:\n    pass\n")
    (tmp_path / "providers" / "__init__.py").write_text(
        "from .aws import AwsProvider\nfrom .gcp import GcpProvider\n"
        "REGISTRY = {'aws': AwsProvider, 'gcp': GcpProvider}\n")
    db = _mk_graph(tmp_path, [
        {"id": 1, "name": "AwsProvider", "label": "Class",
         "file_path": "providers/aws.py", "start_line": 1},
        {"id": 2, "name": "GcpProvider", "label": "Class",
         "file_path": "providers/gcp.py", "start_line": 1},
    ], [])
    issue = "Add an azure provider analogous to the existing aws and gcp providers."
    st = _state(tmp_path, db, issue_text=issue)
    ev1 = _ev("search", "grep -rn azure .", "", action_index=3)
    ev2 = _ev("search", "grep -rn azure .", "", action_index=7)
    first = augment(ev1, st)     # first probe: silent
    assert first == []
    assert classify_outcome(ev2, st) == ZERO_ABSENT
    second = augment(ev2, st)    # repeat: change_surface may speak
    assert any(a.tier == "HYPOTHESIS" and a.producer == "change_surface" for a in second)


def test_trace_hit_from_test_output(tmp_path):
    """A failing test whose output contains an in-repo traceback -> TRACE_HIT ->
    the deepest in-repo frame is surfaced; test-path frames are dropped."""
    db = _mk_graph(tmp_path, [], [])
    # D-2 (2026-07-20): an in-repo trace frame must exist under repo_root — a
    # relative shape alone no longer qualifies (that let third-party dep frames
    # through). Materialize the repo file this trace claims.
    (tmp_path / "patroni").mkdir(exist_ok=True)
    (tmp_path / "patroni" / "watchdog.py").write_text(
        "def check():\n    raise ValueError('boom')\n", encoding="utf-8")
    out = textwrap.dedent('''\
        Traceback (most recent call last):
          File "tests/test_watch.py", line 5, in test_it
            watchdog.check()
          File "patroni/watchdog.py", line 88, in check
            raise ValueError("boom")
        ValueError: boom
    ''')
    ev = _ev("test", "python -m pytest -q", out)
    st = _state(tmp_path, db)
    assert classify_outcome(ev, st) == TRACE_HIT
    adds = augment(ev, _state(tmp_path, db))
    body = "\n".join(a.target + " " + " ".join(a.payload) for a in adds)
    assert "patroni/watchdog.py" in body
    assert "test_watch" not in body


def test_flood_many_hits(tmp_path):
    """A bare symbol grep with a very large hit set -> FLOOD -> the def partition
    cuts through the flood (conservative INFO/VERIFIED, never a blast of lines)."""
    db = _mk_graph(tmp_path, [
        {"id": 1, "name": "log", "file_path": "core/logutil.py", "start_line": 3},
    ], [])
    out = "\n".join(f"src/mod{i}/f.py:{i}: log()" for i in range(60))
    ev = _ev("search", "grep -rn log .", out)
    st = _state(tmp_path, db)
    assert classify_outcome(ev, st) == FLOOD


# ---------------------------------------------------------------------------
# Edit / Test producers.
# ---------------------------------------------------------------------------
def test_edit_event_wraps_patch_delta(tmp_path, monkeypatch):
    """An edit that changes positional arity, with a FACT-tier non-test caller in
    the graph, yields a WARNING signature-mismatch Addition (patch_delta)."""
    monkeypatch.setenv("GT_PATCH_DELTA", "1")
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("from svc import f\n\nf(1)\n")
    db = _mk_graph(tmp_path, [
        {"id": 1, "name": "f", "file_path": "svc.py", "start_line": 1},
        {"id": 2, "name": "caller", "file_path": "app/main.py", "start_line": 3},
    ], [
        {"id": 1, "source_id": 2, "target_id": 1, "resolution_method": "import",
         "confidence": 1.0, "source_line": 3},
    ])
    before = "def f(a):\n    return a\n"
    after = "def f(a, b):\n    return a + b\n"
    ev = _ev("edit", "sed -i ... svc.py", "", changed_files=("svc.py",),
             edit_before_after={"svc.py": (before, after)})
    adds = augment(ev, _state(tmp_path, db))
    assert any(a.evidence_type == "signature_mismatch" and a.tier == "WARNING" for a in adds)


def test_edit_event_off_without_patch_delta_flag(tmp_path):
    """GT_GATEWAY on but GT_PATCH_DELTA off -> no patch_delta advisories."""
    db = _mk_graph(tmp_path, [{"id": 1, "name": "f", "file_path": "svc.py"}], [])
    ev = _ev("edit", "sed -i ... svc.py", "", changed_files=("svc.py",),
             edit_before_after={"svc.py": ("def f(a): pass\n", "def f(a,b): pass\n")})
    assert augment(ev, _state(tmp_path, db)) == []


_RAW_PYTEST_RED = "\n".join([
    "$ pytest tests/test_pay.py::test_refund -q",
    "============================= FAILURES =============================",
    "____________________________ test_refund ___________________________",
    "tests/test_pay.py:12: in test_refund",
    "    assert refund(100) == 90",
    "E   AssertionError: assert 100 == 90",
    "src/pay.py:33: in refund",
    "    return amount",
    "FAILED tests/test_pay.py::test_refund - AssertionError",
])


def test_test_event_consumes_injected_covering(tmp_path):
    """Test events do NOT execute anything; a pre-computed CoveringResult injected
    on the event is wrapped into an Addition preserving its tier + evidence.
    EXPECTATION CHANGED (bounce F2): the body is routed through native_render's
    identity firewall (Format D), so the fixture is realistic runner output and
    the shipped body carries the error class + value delta, never raw stdout."""
    db = _mk_graph(tmp_path, [], [])
    cov = CoveringResult(target="src/pay.py", verdict="RED", tier="WARNING",
                         body_lines=_RAW_PYTEST_RED.splitlines(),
                         evidence=[("src/pay.py", 12)])
    ev = _ev("test", "python -m pytest -q", "", covering=cov)
    adds = augment(ev, _state(tmp_path, db))
    assert adds and adds[0].evidence_type == "covering_verdict"
    assert adds[0].target == "src/pay.py" and adds[0].tier == "WARNING"
    body = "\n".join(adds[0].payload)
    assert "AssertionError" in body and "100 == 90" in body  # the fix signal survives


def test_test_event_no_injection_is_silent(tmp_path):
    db = _mk_graph(tmp_path, [], [])
    ev = _ev("test", "python -m pytest -q", "all passed")
    assert augment(ev, _state(tmp_path, db)) == []


# ---------------------------------------------------------------------------
# LEAK LAW — a test file def/path never surfaces as a fact target or evidence.
# ---------------------------------------------------------------------------
def test_leak_law_test_path_def_never_surfaces(tmp_path):
    """A symbol defined ONLY in a test-shaped path must never surface (target or
    evidence). Reverting the leak predicate reddens this."""
    db = _mk_graph(tmp_path, [
        {"id": 1, "name": "helper_fn", "file_path": "conftest.py", "start_line": 3},
        {"id": 2, "name": "helper_fn", "file_path": "app/tests.py", "start_line": 4},
        {"id": 3, "name": "helper_fn", "file_path": "pkg/util.py", "start_line": 9},
    ], [])
    ev = _ev("search", "grep -rn helper_fn .", "")  # empty -> name-fold path
    adds = augment(ev, _state(tmp_path, db))
    blob = json.dumps([(a.target, a.payload, a.provenance) for a in adds])
    assert "pkg/util.py" in blob             # the real def surfaces
    assert "conftest.py" not in blob and "tests.py" not in blob  # test paths never do


def test_leak_law_test_ref_is_count_only(tmp_path):
    """A test caller contributes a COUNT, never its name/path."""
    db = _mk_graph(tmp_path, [
        {"id": 1, "name": "run", "file_path": "a/x.py", "start_line": 10},
        {"id": 2, "name": "run", "file_path": "b/y.py", "start_line": 20},
        {"id": 3, "name": "test_run", "file_path": "tests/test_x.py",
         "start_line": 5, "is_test": 1},
    ], [
        {"id": 1, "source_id": 3, "target_id": 1, "resolution_method": "import",
         "confidence": 1.0},
    ])
    ev = _ev("search", "grep -rn run .", "a/x.py:10: run\nb/y.py:20: run")
    adds = augment(ev, _state(tmp_path, db))
    blob = "\n".join(a.target + "\n" + "\n".join(a.payload) for a in adds)
    assert "test_run" not in blob and "test_x.py" not in blob


# ---------------------------------------------------------------------------
# Need gate (dedup) + master flag.
# ---------------------------------------------------------------------------
def test_dedup_repeated_event_is_silent(tmp_path):
    """The SAME event twice against ONE state -> the second yields [] AFTER the
    delivery commit. ADJUSTED (stamp-at-seal, bounce 2026-07-10): augment READS
    the chain and never stamps it — the kernel's seal step stamps on the actual
    append. Pre-commit, the repeat is RE-OFFERED (deferred, not destroyed)."""
    db = _mk_graph(tmp_path, [
        {"id": 1, "name": "run", "file_path": "a/x.py", "start_line": 10},
        {"id": 2, "name": "run", "file_path": "b/y.py", "start_line": 20},
    ], [])
    st = _state(tmp_path, db)
    ev = _ev("search", "grep -rn run .", "a/x.py:10: run\nb/y.py:20: run")
    first = augment(ev, st)
    assert first
    assert st.delivered_keys == set()            # production never stamps (F1)
    assert augment(ev, st)                       # undelivered -> re-offered
    st.delivered_keys.add(first[0].dedup_key)    # the delivery commit (seal)
    assert augment(ev, st) == []                 # latched only once DELIVERED
    assert st.delivered_keys                     # the key was recorded at commit


def test_master_flag_off_is_silent(tmp_path, monkeypatch):
    monkeypatch.delenv("GT_GATEWAY", raising=False)
    db = _mk_graph(tmp_path, [
        {"id": 1, "name": "run", "file_path": "a/x.py", "start_line": 10},
        {"id": 2, "name": "run", "file_path": "b/y.py", "start_line": 20},
    ], [])
    ev = _ev("search", "grep -rn run .", "a/x.py:10: run\nb/y.py:20: run")
    assert augment(ev, _state(tmp_path, db)) == []


# ---------------------------------------------------------------------------
# DETERMINISM — same corpus input -> byte-identical augment output across two
# SEPARATE python processes with DIFFERENT PYTHONHASHSEED.
# ---------------------------------------------------------------------------
def test_determinism_two_processes(tmp_path):
    db = _mk_graph(tmp_path, [
        {"id": 1, "name": "run", "file_path": "a/x.py", "start_line": 10},
        {"id": 2, "name": "run", "file_path": "b/y.py", "start_line": 20},
        {"id": 3, "name": "get_user", "file_path": "app/models.py", "start_line": 11},
    ], [
        {"id": 1, "source_id": 2, "target_id": 1, "resolution_method": "type_flow",
         "confidence": 0.95, "metadata": "receiver_type=Runner"},
    ])
    runner = tmp_path / "runner.py"
    runner.write_text(textwrap.dedent(f'''\
        import json, sys
        sys.path.insert(0, {json.dumps(_repo_src())})
        import os
        os.environ["GT_GATEWAY"] = "1"
        from groundtruth.runtime.gateway import ToolEvent, GatewayState, augment
        db = {json.dumps(db)}
        root = {json.dumps(str(tmp_path))}
        events = [
            ToolEvent("search", "grep -rn run .", "a/x.py:10: run\\nb/y.py:20: run"),
            ToolEvent("search", "grep -rn getUser .", ""),
        ]
        out = []
        st = GatewayState(graph_db=db, repo_root=root)
        for ev in events:
            for a in augment(ev, st):
                out.append([a.evidence_type, a.target, a.payload, a.provenance,
                            a.tier, a.producer, a.dedup_key, a.graph_revision])
        print(json.dumps(out, sort_keys=True))
    '''))
    env0 = {**os.environ, "PYTHONHASHSEED": "0"}
    env1 = {**os.environ, "PYTHONHASHSEED": "1"}
    r0 = subprocess.run([sys.executable, str(runner)], capture_output=True, text=True, env=env0)
    r1 = subprocess.run([sys.executable, str(runner)], capture_output=True, text=True, env=env1)
    assert r0.returncode == 0, r0.stderr
    assert r1.returncode == 0, r1.stderr
    assert r0.stdout == r1.stdout
    assert json.loads(r0.stdout)  # non-empty: it actually produced facts


def _repo_src() -> str:
    # tests/runtime/test_gateway.py -> repo/src
    return str(Path(__file__).resolve().parents[2] / "src")


# ===========================================================================
# BOUNCE FIXES (adversarial LIPI review, 2026-07-10) — F1/F2/F3/F4 pins.
# ===========================================================================

# ---- F1: dedup key must key on the SYMBOL + content, not just (producer,target,kind)
def test_f1_distinct_symbols_same_first_def_file_both_fire(tmp_path):
    """Two DIFFERENT symbols whose alphabetically-first def file coincides must BOTH
    deliver their partitions (the confirmed run/handle collision)."""
    db = _mk_graph(tmp_path, [
        {"id": 1, "name": "run", "file_path": "core/base.py", "start_line": 10},
        {"id": 2, "name": "run", "file_path": "m_other.py", "start_line": 20},
        {"id": 3, "name": "handle", "file_path": "core/base.py", "start_line": 30},
        {"id": 4, "name": "handle", "file_path": "n_other.py", "start_line": 40},
    ], [])
    st = _state(tmp_path, db)
    a_run = augment(_ev("search", "grep -rn run .",
                        "core/base.py:10: def run():\nm_other.py:20: def run():",
                        action_index=1), st)
    a_handle = augment(_ev("search", "grep -rn handle .",
                           "core/base.py:30: def handle():\nn_other.py:40: def handle():",
                           action_index=2), st)
    assert a_run, "run partition must fire"
    assert a_handle, "handle partition must fire (dedup collision = the F1 bug)"
    assert a_run[0].dedup_key != a_handle[0].dedup_key


def test_f1_same_symbol_refires_after_def_set_changes(tmp_path):
    """The SAME symbol re-probed after its def-set CHANGED (an edit moved the def)
    must fire again — the content component keys freshness (closes F5).
    ADJUSTED (stamp-at-seal, bounce 2026-07-10): the test performs the delivery
    commit after the first fire (augment no longer stamps at production)."""
    db = _mk_graph(tmp_path, [
        {"id": 1, "name": "run", "file_path": "a/x.py", "start_line": 10},
        {"id": 2, "name": "run", "file_path": "b/y.py", "start_line": 20},
    ], [])
    st = _state(tmp_path, db)
    ev = _ev("search", "grep -rn run .", "a/x.py:10: run\nb/y.py:20: run")
    first = augment(ev, st)
    assert first                               # first: fires
    st.delivered_keys.add(first[0].dedup_key)  # the delivery commit (seal)
    assert augment(ev, st) == []               # unchanged + delivered: suppressed
    con = sqlite3.connect(db)
    con.execute("UPDATE nodes SET start_line=99 WHERE id=1")  # the def moved
    con.commit()
    con.close()
    again = augment(ev, st)
    assert again, "changed def-set must re-fire (freshness)"
    assert any("99" in ln for a in again for ln in a.payload)


# ---- F2: body_lines are leak-guarded; covering bodies go through Format D ----
def test_f2_covering_body_carries_no_test_identity(tmp_path):
    """Raw pytest output injected as a CoveringResult: the shipped body carries the
    error class + value delta + the agent's OWN frame, and NO test path / ::nodeid /
    def test_ echo (the cardinal leak invariant, via native_render Format D)."""
    db = _mk_graph(tmp_path, [], [])
    cov = CoveringResult(target="src/pay.py", verdict="RED", tier="WARNING",
                         body_lines=_RAW_PYTEST_RED.splitlines(),
                         evidence=[("src/pay.py", 12)])
    ev = _ev("test", "python -m pytest -q", "", covering=cov)
    adds = augment(ev, _state(tmp_path, db))
    assert adds
    body = "\n".join(adds[0].payload)
    assert "AssertionError" in body            # error class
    assert "100 == 90" in body                 # value delta
    assert "src/pay.py:33" in body             # the agent's own frame (where to fix)
    assert "test_pay" not in body              # NO test file token
    assert "::" not in body                    # NO nodeid
    assert "def test_" not in body             # NO test source echo
    assert "$ pytest" not in body              # NO command echo
    assert "FAILED" not in body                # NO summary line


def test_f2_covering_nothing_signal_bearing_abstains(tmp_path):
    """When the firewall keeps nothing signal-bearing, the covering producer
    abstains entirely (correct-or-quiet) instead of shipping raw stdout."""
    db = _mk_graph(tmp_path, [], [])
    cov = CoveringResult(target="src/pay.py", verdict="RED", tier="WARNING",
                         body_lines=["$ pytest -q", "1 failed"],
                         evidence=[("src/pay.py", 12)])
    ev = _ev("test", "python -m pytest -q", "", covering=cov)
    assert augment(ev, _state(tmp_path, db)) == []


def test_f2_change_surface_leaky_template_row_dropped(tmp_path, monkeypatch):
    """A test-shaped template_file row is dropped from a change_surface Addition;
    the addition itself survives when its core (suggested_path) is clean."""
    import groundtruth.runtime.gateway as gw
    from groundtruth.pretask.change_surface import ChangeSurfaceResult, NewFileDestination
    monkeypatch.setenv("GT_CHANGE_SURFACE", "1")
    fake = ChangeSurfaceResult(
        entities=["azure"],
        destinations=[NewFileDestination(
            entity="azure", suggested_path="providers/azure.py",
            directory="providers", template_file="tests/template_spec.py",
            registration_file="providers/__init__.py",
            sibling_files=["providers/aws.py"], issue_span="azure provider",
            evidence=["nearest template: tests/template_spec.py"],
        )],
        abstained=False,
    )
    monkeypatch.setattr(gw, "detect_change_surface", lambda *a, **k: fake)
    db = _mk_graph(tmp_path, [], [])
    st = _state(tmp_path, db, issue_text="Add an azure provider")
    # two zero probes so the honest-negative repeat gate opens
    augment(_ev("search", "grep -rn azure .", "", action_index=3), st)
    adds = augment(_ev("search", "grep -rn azure .", "", action_index=7), st)
    assert adds, "the addition survives (its core is clean)"
    blob = "\n".join(ln for a in adds for ln in a.payload)
    assert "providers/azure.py" in blob
    assert "template_spec" not in blob         # the leaky row is dropped


def test_f2_mk_add_belt_drops_leaky_body_line(tmp_path):
    """Belt-and-braces: ANY body line carrying a test-shaped path token is dropped
    at envelope-construction time, regardless of producer."""
    from groundtruth.runtime.gateway import _mk_add
    db = _mk_graph(tmp_path, [], [])
    st = _state(tmp_path, db)
    a = _mk_add(st, _ev("search", "grep -rn ok ."), fact_kind="x", target="src/ok.py",
                body_lines=["def: src/ok.py:5",
                            "see tests/test_x.py:9 for details",
                            "also conftest.py mentions it"],
                evidence=[], tier="INFO", producer="p")
    body = "\n".join(a.payload)
    assert "src/ok.py:5" in body
    assert "test_x.py" not in body and "conftest.py" not in body


# ---- F3: classify_command — segment HEAD rule, not raw substring --------------
@pytest.mark.parametrize("cmd,kind", [
    ("grep -rn pytest .", "search"),           # TEST keyword in the OPERAND
    ("grep -rn unittest src/", "search"),
    ("grep -rn 'go test' .", "search"),
    ("grep -rn tox .", "search"),
    ("grep -rn jest .", "search"),
    ("grep -rn 'sed -i' src/", "search"),      # EDIT keyword in the OPERAND
    ("rg 'cargo test' .", "search"),
    ("pytest -q", "test"),                     # plain runner stays test
    ("cd /app && pytest tests/ -q", "test"),   # compound: highest-precedence segment
    ("git apply fix.patch", "edit"),           # patch-application forms are edits
    ("git checkout -- src/x.py", "edit"),
    ("patch -p1 < fix.patch", "edit"),
])
def test_f3_classify_segment_head_rule(cmd, kind):
    assert classify_command(cmd) == kind


# ---- F4: honest-negative gate scoped to RELATED intervening edits -------------
def _absent_fixture(tmp_path, monkeypatch):
    monkeypatch.setenv("GT_CHANGE_SURFACE", "1")
    (tmp_path / "providers").mkdir()
    (tmp_path / "providers" / "aws.py").write_text("class AwsProvider:\n    pass\n")
    (tmp_path / "providers" / "gcp.py").write_text("class GcpProvider:\n    pass\n")
    (tmp_path / "providers" / "__init__.py").write_text(
        "from .aws import AwsProvider\nfrom .gcp import GcpProvider\n"
        "REGISTRY = {'aws': AwsProvider, 'gcp': GcpProvider}\n")
    (tmp_path / "unrelated.py").write_text("x = 1\n")
    db = _mk_graph(tmp_path, [
        {"id": 1, "name": "AwsProvider", "label": "Class",
         "file_path": "providers/aws.py", "start_line": 1},
        {"id": 2, "name": "GcpProvider", "label": "Class",
         "file_path": "providers/gcp.py", "start_line": 1},
    ], [])
    issue = "Add an azure provider analogous to the existing aws and gcp providers."
    return _state(tmp_path, db, issue_text=issue)


def test_f4_unrelated_edit_does_not_mute_zero_absent(tmp_path, monkeypatch):
    """An intervening edit to an UNRELATED file must NOT reset the honest-negative
    repeat gate — the azure repeat still fires change_surface."""
    st = _absent_fixture(tmp_path, monkeypatch)
    augment(_ev("search", "grep -rn azure .", "", action_index=3), st)
    augment(_ev("edit", "sed -i 's/x/y/' unrelated.py", "",
                changed_files=("unrelated.py",), action_index=5), st)
    out = augment(_ev("search", "grep -rn azure .", "", action_index=7), st)
    assert any(a.producer == "change_surface" for a in out), \
        "unrelated edit must not suppress the honest-negative"


def test_f4_related_edit_creating_entity_mutes_zero_absent(tmp_path, monkeypatch):
    """An intervening edit that plausibly CREATES the probed entity (path/content
    mentions the stem) keeps the gate silent — the agent may have just added it."""
    st = _absent_fixture(tmp_path, monkeypatch)
    augment(_ev("search", "grep -rn azure .", "", action_index=3), st)
    augment(_ev("edit", "cat > providers/azure.py <<'EOF'\nclass AzureProvider: pass\nEOF",
                "", changed_files=("providers/azure.py",), action_index=5), st)
    out = augment(_ev("search", "grep -rn azure .", "", action_index=7), st)
    assert not any(a.producer == "change_surface" for a in out), \
        "a related edit (creates azure.py) must keep the honest-negative silent"


# ===========================================================================
# RECONCILIATION — the Gateway emits the CANONICAL EvidenceEnvelope contract.
# ===========================================================================
def test_augment_emits_evidence_envelopes(tmp_path):
    """ONE payload contract: augment() returns EvidenceEnvelope objects with the
    Gateway-filled fields (preferred_event from the triggering kind, advisory,
    unmeasured, graph_revision-scoped freshness, deterministic token estimate)."""
    from groundtruth.runtime.evidence_envelope import EvidenceEnvelope
    db = _mk_graph(tmp_path, [
        {"id": 1, "name": "run", "file_path": "a/x.py", "start_line": 10},
        {"id": 2, "name": "run", "file_path": "b/y.py", "start_line": 20},
    ], [])
    adds = augment(_ev("search", "grep -rn run .",
                       "a/x.py:10: run\nb/y.py:20: run"), _state(tmp_path, db))
    assert adds and all(isinstance(a, EvidenceEnvelope) for a in adds)
    a = adds[0]
    assert a.preferred_event == "search"          # the triggering event kind
    assert a.blocking_eligibility == "advisory"   # nothing unmeasured enforces
    assert a.measured is False
    assert a.valid_until == a.graph_revision      # graph-revision-scoped freshness
    assert a.estimated_cost_tokens > 0            # deterministic len-based estimate
    assert a.fact_id == "run"                     # the F1 symbol rides fact_id


def test_augment_output_validates_clean_over_corpus(tmp_path, monkeypatch):
    """EVERY envelope augment() ships over the WHOLE D0 corpus (flag on, graph seeded
    from the corpus's own search symbols so producers actually fire) passes
    evidence_envelope.validate() with zero violations — plus the seam-injected
    producers (covering, patch_delta) the raw corpus cannot carry."""
    from groundtruth.runtime.evidence_envelope import validate as env_validate
    monkeypatch.setenv("GT_CHANGE_SURFACE", "1")
    monkeypatch.setenv("GT_PATCH_DELTA", "1")
    records: list[dict] = []
    syms: set[str] = set()
    for fp in sorted(glob.glob(str(_CORPUS / "*.jsonl"))):
        for ln in open(fp, encoding="utf-8"):
            rec = json.loads(ln)
            if "__meta__" in rec:
                continue
            records.append(rec)
            s = search_pattern(rec["command"])
            if s:
                syms.add(s)
    nodes, nid = [], 1
    for s in sorted(syms):  # 2 def files per symbol -> the AMBIGUOUS partition fires
        for fdir in ("alpha", "beta"):
            nodes.append({"id": nid, "name": s, "file_path": f"{fdir}/{s}_mod.py",
                          "start_line": nid})
            nid += 1
    db = _mk_graph(tmp_path, nodes, [])
    st = _state(tmp_path, db)
    envs = []
    for i, rec in enumerate(records):
        ev = ToolEvent(kind=classify_command(rec["command"]), command=rec["command"],
                       output=rec["output"], action_index=i + 1)
        envs += augment(ev, st)
    # seam-injected producers not representable in raw corpus records:
    cov = CoveringResult(target="src/pay.py", verdict="RED", tier="WARNING",
                         body_lines=_RAW_PYTEST_RED.splitlines(),
                         evidence=[("src/pay.py", 12)])
    envs += augment(ToolEvent("test", "pytest -q", "", covering=cov,
                              action_index=len(records) + 1), st)
    (tmp_path / "app").mkdir(exist_ok=True)
    (tmp_path / "app" / "main.py").write_text("from svc import f\n\nf(1)\n")
    db2_nodes = [
        {"id": 9001, "name": "f", "file_path": "svc.py", "start_line": 1},
        {"id": 9002, "name": "caller", "file_path": "app/main.py", "start_line": 3},
    ]
    # reuse the same state/graph: patch_delta reads its own graph_db argument
    envs += augment(ToolEvent(
        "edit", "sed -i ... svc.py", "", action_index=len(records) + 2,
        changed_files=("svc.py",),
        edit_before_after={"svc.py": ("def f(a):\n    return a\n",
                                      "def f(a, b):\n    return a + b\n")},
    ), _state(tmp_path, _mk_graph_named(tmp_path, "g2.db", db2_nodes, [
        {"id": 1, "source_id": 9002, "target_id": 9001, "resolution_method": "import",
         "confidence": 1.0, "source_line": 3},
    ])))
    assert len(envs) >= 5, "non-vacuous: multiple producers fired over the corpus"
    for e in envs:
        assert env_validate(e) == [], (e.producer, e.evidence_type, env_validate(e))


def _mk_graph_named(tmp_path, name, nodes, edges):
    """_mk_graph variant with a caller-chosen filename (second graph in one tmp dir)."""
    db = str(tmp_path / name)
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE nodes(id INTEGER PRIMARY KEY, label TEXT, name TEXT,"
        " qualified_name TEXT, file_path TEXT, start_line INTEGER, end_line INTEGER,"
        " signature TEXT, return_type TEXT, is_exported INTEGER, is_test INTEGER,"
        " language TEXT, parent_id INTEGER);"
        "CREATE TABLE edges(id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER,"
        " type TEXT, source_line INTEGER, source_file TEXT, resolution_method TEXT,"
        " confidence REAL, metadata TEXT);"
    )
    for n in nodes:
        con.execute(
            "INSERT INTO nodes(id,label,name,file_path,start_line,end_line,is_test,language)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (n["id"], n.get("label", "Function"), n["name"], n["file_path"],
             n.get("start_line", 1), n.get("end_line", 5),
             n.get("is_test", 0), n.get("language", "python")))
    for e in edges:
        con.execute(
            "INSERT INTO edges(id,source_id,target_id,type,source_line,resolution_method,"
            "confidence,metadata) VALUES(?,?,?,?,?,?,?,?)",
            (e["id"], e["source_id"], e["target_id"], e.get("type", "CALLS"),
             e.get("source_line", 1), e.get("resolution_method", "import"),
             e.get("confidence", 1.0), e.get("metadata")))
    con.commit()
    con.close()
    return db
