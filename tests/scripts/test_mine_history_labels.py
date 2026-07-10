"""TTD for scripts/mine_history_labels.py — the FREE history-mined evaluation labels.

Every fixture is a real, scripted tmp git repo (never a mock): a clean
signature-change commit, a multi-intent commit that MUST be skipped, a sibling-add
with a registry companion, and a syntax broken->fixed pair. The tests pin the three
label families, the noise controls (merge/>20-file skips, path policy), and the
patch_delta scoring accounting (TP/FP/FN + 8dp P/R). One MUTATION check proves the
multi-intent skip is load-bearing: disable it and the noise-control test bites.

LLM-free, deterministic, stdlib + git subprocess only.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")


def _load_miner():
    path = _REPO_ROOT / "scripts" / "mine_history_labels.py"
    spec = importlib.util.spec_from_file_location("mine_history_labels_under_test", str(path))
    assert spec and spec.loader, f"cannot build spec for {path}"
    mod = importlib.util.module_from_spec(spec)
    sys.modules["mine_history_labels_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load_miner()


# ---------------------------------------------------------------------------
# tiny git-repo scripting helpers (real git, no mocks)
# ---------------------------------------------------------------------------
def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "PYTHONIOENCODING": "utf-8",
           "GIT_AUTHOR_DATE": "2020-01-01T00:00:00", "GIT_COMMITTER_DATE": "2020-01-01T00:00:00"}
    return subprocess.run(["git", "-C", str(repo), *args], check=check,
                          capture_output=True, text=True, encoding="utf-8", env=env)


def _init(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "T")
    _git(repo, "config", "core.autocrlf", "false")
    _git(repo, "config", "commit.gpgsign", "false")


def _write(repo: Path, rel: str, content: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8", newline="\n")


def _commit(repo: Path, msg: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


_NODES_SCHEMA = (
    "CREATE TABLE nodes (id INTEGER PRIMARY KEY, label TEXT, name TEXT, "
    "qualified_name TEXT, file_path TEXT, start_line INT, end_line INT, "
    "signature TEXT, return_type TEXT, is_exported INT, is_test INT, "
    "language TEXT, parent_id INT)"
)
_EDGES_SCHEMA = (
    "CREATE TABLE edges (id INTEGER PRIMARY KEY, source_id INT, target_id INT, "
    "type TEXT, source_line INT, source_file TEXT, resolution_method TEXT, "
    "confidence REAL, metadata TEXT)"
)


def _make_graph(path, nodes, edges) -> None:
    """nodes: (id,name,file,is_test); edges: (src,tgt,type,method,conf,src_file,src_line)."""
    con = sqlite3.connect(str(path))
    con.execute(_NODES_SCHEMA)
    con.execute(_EDGES_SCHEMA)
    for nid, name, fpath, is_test in nodes:
        con.execute(
            "INSERT INTO nodes (id,label,name,file_path,is_test,language) VALUES (?,?,?,?,?,?)",
            (nid, "Function", name, fpath, is_test, "python"),
        )
    for src, tgt, etype, method, conf, sfile, sline in edges:
        con.execute(
            "INSERT INTO edges (source_id,target_id,type,resolution_method,confidence,"
            "source_file,source_line) VALUES (?,?,?,?,?,?,?)",
            (src, tgt, etype, method, conf, sfile, sline),
        )
    con.commit()
    con.close()


# ---------------------------------------------------------------------------
# 1. SIGNATURE — clean single-intent commit
# ---------------------------------------------------------------------------
def test_signature_clean_commit_labels_caller_fix(mod, tmp_path):
    repo = tmp_path / "sig"
    _init(repo)
    _write(repo, "src/api.py", "def get_user(uid):\n    return uid\n")
    _write(repo, "src/caller.py", "def use():\n    return get_user(5)\n")
    _commit(repo, "init")
    _write(repo, "src/api.py", "def get_user(uid, name):\n    return uid\n")
    _write(repo, "src/caller.py", "def use():\n    return get_user(5, 'x')\n")
    _commit(repo, "add required param + fix caller")

    res = mod.mine_repo("sig", str(repo))
    assert len(res.signature) == 1, res.signature
    s = res.signature[0]
    assert s["symbol"] == "get_user"
    assert s["file"] == "src/api.py"
    assert s["callers_fixed"] == ["src/caller.py"]
    assert s["old_bounds"] == [1, 1] and s["new_bounds"] == [2, 2]
    assert s["confidence"] == "clean_single_intent"
    assert s["repo"] == "sig"


def test_signature_test_caller_never_in_callers_fixed(mod, tmp_path):
    """Leak-law parity: a caller-fix that lives in a test file goes to
    test_callers_fixed, NEVER callers_fixed (patch_delta can never predict it)."""
    repo = tmp_path / "sigt"
    _init(repo)
    _write(repo, "src/api.py", "def get_user(uid):\n    return uid\n")
    _write(repo, "tests/test_api.py", "def test_use():\n    return get_user(5)\n")
    _commit(repo, "init")
    _write(repo, "src/api.py", "def get_user(uid, name):\n    return uid\n")
    _write(repo, "tests/test_api.py", "def test_use():\n    return get_user(5, 'x')\n")
    _commit(repo, "sig change + test caller fix")

    res = mod.mine_repo("sigt", str(repo))
    assert len(res.signature) == 1, res.signature
    s = res.signature[0]
    assert s["callers_fixed"] == []
    assert "tests/test_api.py" in s["test_callers_fixed"]


# ---------------------------------------------------------------------------
# 2. MULTI-INTENT — >20-file commit must be skipped (+ mutation)
# ---------------------------------------------------------------------------
def test_multi_intent_commit_skipped(mod, tmp_path):
    repo = tmp_path / "multi"
    _init(repo)
    _write(repo, "src/api.py", "def get_user(uid):\n    return uid\n")
    _write(repo, "src/caller.py", "def use():\n    return get_user(5)\n")
    _commit(repo, "init")
    _write(repo, "src/api.py", "def get_user(uid, name):\n    return uid\n")
    _write(repo, "src/caller.py", "def use():\n    return get_user(5, 'x')\n")
    for i in range(20):
        _write(repo, f"noise/n{i}.py", f"X{i} = 1\n")
    _commit(repo, "multi-intent: sig change buried in 22 files")

    res = mod.mine_repo("multi", str(repo))
    assert res.signature == [], "a >20-file commit is multi-intent and must be skipped"
    assert res.stats["multi_intent_skipped"] >= 1


def test_mutation_disabling_multi_intent_skip_leaks_label(mod, tmp_path, monkeypatch):
    """MUTATION: force too_many_files -> False and the buried signature label leaks;
    with the real guard it is quiet. This is the red-on-break proof the skip is
    load-bearing, not decoration."""
    repo = tmp_path / "mut"
    _init(repo)
    _write(repo, "src/api.py", "def get_user(uid):\n    return uid\n")
    _write(repo, "src/caller.py", "def use():\n    return get_user(5)\n")
    _commit(repo, "init")
    _write(repo, "src/api.py", "def get_user(uid, name):\n    return uid\n")
    _write(repo, "src/caller.py", "def use():\n    return get_user(5, 'x')\n")
    for i in range(20):
        _write(repo, f"noise/n{i}.py", f"X{i} = 1\n")
    _commit(repo, "multi-intent")

    assert mod.mine_repo("mut", str(repo)).signature == []  # guard intact -> quiet

    monkeypatch.setattr(mod, "too_many_files", lambda n: False)  # break the guard
    leaked = mod.mine_repo("mut", str(repo)).signature
    assert any(s["symbol"] == "get_user" for s in leaked), (
        "removing the multi-intent skip must reintroduce the buried label")


# ---------------------------------------------------------------------------
# 3. COMPANION — sibling-add with a registry companion
# ---------------------------------------------------------------------------
def test_companion_sibling_add_with_registry(mod, tmp_path):
    repo = tmp_path / "comp"
    _init(repo)
    _write(repo, "src/providers/aws.py", "def aws():\n    pass\n")
    _write(repo, "src/providers/gcp.py", "def gcp():\n    pass\n")
    _write(repo, "src/registry.py", "PROVIDERS = {'aws': 1, 'gcp': 2}\n")
    _commit(repo, "init providers")
    _write(repo, "src/providers/azure.py", "def azure():\n    pass\n")
    _write(repo, "src/registry.py", "PROVIDERS = {'aws': 1, 'gcp': 2, 'azure': 3}\n")
    _commit(repo, "add azure provider + register")

    res = mod.mine_repo("comp", str(repo))
    assert len(res.companion) == 1, res.companion
    c = res.companion[0]
    assert c["new_file"] == "src/providers/azure.py"
    assert c["entity"] == "src/providers"
    assert set(c["siblings"]) >= {"aws.py", "gcp.py"}
    assert "src/registry.py" in c["companions"]


def test_companion_single_sibling_abstains(mod, tmp_path):
    """<2 pre-existing siblings is not a family -> no companion label."""
    repo = tmp_path / "comp1"
    _init(repo)
    _write(repo, "src/providers/aws.py", "def aws():\n    pass\n")
    _write(repo, "src/registry.py", "PROVIDERS = {'aws': 1}\n")
    _commit(repo, "init one provider")
    _write(repo, "src/providers/azure.py", "def azure():\n    pass\n")
    _write(repo, "src/registry.py", "PROVIDERS = {'aws': 1, 'azure': 3}\n")
    _commit(repo, "add azure (only one prior sibling)")

    res = mod.mine_repo("comp1", str(repo))
    assert res.companion == []


# ---------------------------------------------------------------------------
# 4. SYNTAX — broken -> fixed pair
# ---------------------------------------------------------------------------
def test_syntax_broken_to_fixed_pair(mod, tmp_path):
    repo = tmp_path / "syn"
    _init(repo)
    _write(repo, "src/mod.py", "def broken(:\n    pass\n")   # invalid python
    _write(repo, "src/other.py", "x = 1\n")
    _commit(repo, "add broken module")
    _write(repo, "src/mod.py", "def fixed():\n    pass\n")   # now parses
    _commit(repo, "fix the syntax error")

    res = mod.mine_repo("syn", str(repo))
    assert len(res.syntax) == 1, res.syntax
    y = res.syntax[0]
    assert y["file"] == "src/mod.py"
    assert y["error_class"] == "SyntaxError"


def test_clean_edit_is_not_a_syntax_label(mod, tmp_path):
    """A parent that already parses (a normal edit) is NOT a broken->fixed pair."""
    repo = tmp_path / "syn2"
    _init(repo)
    _write(repo, "src/mod.py", "def ok():\n    return 1\n")
    _commit(repo, "init ok")
    _write(repo, "src/mod.py", "def ok():\n    return 2\n")
    _commit(repo, "edit ok")

    res = mod.mine_repo("syn2", str(repo))
    assert res.syntax == []


# ---------------------------------------------------------------------------
# noise-control units + determinism
# ---------------------------------------------------------------------------
def test_noise_control_helpers(mod):
    assert mod.too_many_files(21) is True
    assert mod.too_many_files(20) is False
    assert mod.commit_confidence(2) == "clean_single_intent"
    assert mod.commit_confidence(21) == "multi_file_commit"


def test_mining_is_deterministic(mod, tmp_path):
    repo = tmp_path / "det"
    _init(repo)
    _write(repo, "src/api.py", "def get_user(uid):\n    return uid\n")
    _write(repo, "src/caller.py", "def use():\n    return get_user(5)\n")
    _commit(repo, "init")
    _write(repo, "src/api.py", "def get_user(uid, name):\n    return uid\n")
    _write(repo, "src/caller.py", "def use():\n    return get_user(5, 'x')\n")
    _commit(repo, "sig")
    a = mod.mine_repo("det", str(repo))
    b = mod.mine_repo("det", str(repo))
    assert a.signature == b.signature
    assert a.companion == b.companion
    assert a.syntax == b.syntax


# ---------------------------------------------------------------------------
# 5. SCORING — replay a signature label through patch_delta (TP/FP/FN + P/R)
# ---------------------------------------------------------------------------
def test_score_patch_delta_true_positive(mod, tmp_path):
    """A real signature change whose parent caller is unfixed + a FACT-tier graph
    edge => patch_delta predicts the caller-fixed file => TP, P=R=1.0."""
    repo = tmp_path / "score"
    _init(repo)
    _write(repo, "src/api.py", "def get_user(uid):\n    return uid\n")
    _write(repo, "src/caller.py", "def use():\n    return get_user(5)\n")
    _commit(repo, "init")
    _write(repo, "src/api.py", "def get_user(uid, name):\n    return uid\n")
    _write(repo, "src/caller.py", "def use():\n    return get_user(5, 'x')\n")
    _commit(repo, "sig change + caller fix")

    res = mod.mine_repo("score", str(repo))
    assert len(res.signature) == 1

    graph = repo / "graph.db"
    _make_graph(
        graph,
        nodes=[(1, "get_user", "src/api.py", 0), (2, "use", "src/caller.py", 0)],
        edges=[(2, 1, "CALLS", "import", 0.9, "src/caller.py", 2)],
    )
    score = mod.score_patch_delta(res.signature, str(repo), graph_db=str(graph),
                                  gt_index=None, limit=10)
    assert score["n_scored"] == 1, score
    assert score["tp"] == 1 and score["fp"] == 0 and score["fn"] == 0, score
    assert score["precision"] == 1.0 and score["recall"] == 1.0, score
    # F1: BOTH truth bases are reported; a non-widening label counts in both.
    ec = score["engine_contract"]
    assert ec["tp"] == 1 and ec["fp"] == 0 and ec["fn"] == 0
    assert ec["excluded_widening"] == 0 and ec["n_scored"] == 1
    assert ec["precision"] == 1.0 and ec["recall"] == 1.0
    assert score["unit"] == "caller-file pairs"
    # F3: the full-population block exists even with zero silence labels.
    fullp = score["full_population"]
    assert fullp["silence_scored"] == 0
    assert fullp["as_designed_precision"] == 1.0


# ---------------------------------------------------------------------------
# F1 — WIDENING labels: engine-contract truth excludes them (silence is the contract)
# ---------------------------------------------------------------------------
def test_is_widening_predicate(mod):
    """new range containing the old range makes valid_before AND NOT valid_after
    unsatisfiable -> the engine can never fire -> engine-contract excludes it."""
    assert mod.is_widening([2, 2], [2, 3]) is True    # added defaulted tail
    assert mod.is_widening([4, 4], [3, 4]) is True    # a param gained a default
    assert mod.is_widening([1, 5], [1, 4]) is False   # narrowed max -> fireable
    assert mod.is_widening([2, 3], [1, 2]) is False   # shifted range -> fireable


def test_widening_label_flagged_and_excluded_from_engine_contract(mod, tmp_path):
    """A pure widening ([1,1]->[1,2]) appears in the as-designed base (scored FN,
    engine silent) and is EXCLUDED from the engine-contract base."""
    repo = tmp_path / "wide"
    _init(repo)
    _write(repo, "src/api.py", "def f(a):\n    return a\n")
    _write(repo, "src/caller.py", "def use():\n    return f(1)\n")
    _commit(repo, "init")
    _write(repo, "src/api.py", "def f(a, b=None):\n    return a\n")
    _write(repo, "src/caller.py", "def use():\n    return f(1)  # touched\n")
    _commit(repo, "widen f")

    res = mod.mine_repo("wide", str(repo))
    assert len(res.signature) == 1, res.signature
    s = res.signature[0]
    assert s["old_bounds"] == [1, 1] and s["new_bounds"] == [1, 2]
    assert s["is_widening"] is True

    graph = repo / "graph.db"
    _make_graph(
        graph,
        nodes=[(1, "f", "src/api.py", 0), (2, "use", "src/caller.py", 0)],
        edges=[(2, 1, "CALLS", "import", 0.9, "src/caller.py", 2)],
    )
    score = mod.score_patch_delta(res.signature, str(repo), graph_db=str(graph),
                                  gt_index=None, limit=10)
    # as-designed: the engine is silent on a widening -> the pair is an FN
    assert score["n_scored"] == 1 and score["tp"] == 0 and score["fn"] == 1, score
    # engine-contract: definitionally unscoreable -> excluded entirely
    ec = score["engine_contract"]
    assert ec["excluded_widening"] == 1 and ec["n_scored"] == 0
    assert ec["tp"] == 0 and ec["fp"] == 0 and ec["fn"] == 0, ec


# ---------------------------------------------------------------------------
# F2 — __init__ caller-truth must be CLASS-AWARE (the 0c227e94 false-caller shape)
# ---------------------------------------------------------------------------
def test_init_truth_is_class_aware(mod, tmp_path):
    """Library.__init__ gains a required param. models.py (own-super-init to a
    DIFFERENT base) must NOT be truth; ui.py (Library(...) constructor call) MUST."""
    repo = tmp_path / "initc"
    _init(repo)
    _write(repo, "src/lib.py",
           "class Library:\n    def __init__(self, path):\n        self.path = path\n")
    _write(repo, "src/models.py",
           "class Model(Base):\n    def __init__(self):\n        super().__init__()\n")
    _write(repo, "src/ui.py", "def make(p):\n    return Library(p)\n")
    _commit(repo, "init")
    _write(repo, "src/lib.py",
           "class Library:\n    def __init__(self, path, timeout):\n        self.path = path\n")
    _write(repo, "src/models.py",
           "class Model(Base):\n    def __init__(self):\n        super().__init__()\n\n\nX = 1\n")
    _write(repo, "src/ui.py", "def make(p):\n    return Library(p, 1)\n")
    _commit(repo, "require timeout + fix ui")

    res = mod.mine_repo("initc", str(repo))
    inits = [s for s in res.signature if s["symbol"] == "__init__"]
    assert len(inits) == 1, res.signature
    s = inits[0]
    assert s["class_name"] == "Library"
    assert s["callers_fixed"] == ["src/ui.py"], s  # models.py is NOT a Library.__init__ caller


def test_init_subclass_super_call_is_truth(mod, tmp_path):
    """POSITIVE control for the class-aware rule: a file whose class SUBCLASSES the
    changed class and calls super().__init__ IS a genuine caller-fix."""
    repo = tmp_path / "inits"
    _init(repo)
    _write(repo, "src/lib.py",
           "class Library:\n    def __init__(self, path):\n        self.path = path\n")
    _write(repo, "src/sub.py",
           "class Cached(Library):\n    def __init__(self, path):\n"
           "        super().__init__(path)\n")
    _commit(repo, "init")
    _write(repo, "src/lib.py",
           "class Library:\n    def __init__(self, path, timeout):\n        self.path = path\n")
    _write(repo, "src/sub.py",
           "class Cached(Library):\n    def __init__(self, path):\n"
           "        super().__init__(path, 30)\n")
    _commit(repo, "require timeout + fix subclass")

    res = mod.mine_repo("inits", str(repo))
    inits = [s for s in res.signature if s["symbol"] == "__init__"]
    assert len(inits) == 1, res.signature
    assert inits[0]["callers_fixed"] == ["src/sub.py"], inits[0]


# ---------------------------------------------------------------------------
# F3 — SILENCE-truth labels are replayed; a prediction there is a full-population FP
# ---------------------------------------------------------------------------
def test_silence_label_engine_prediction_is_full_population_fp(mod, tmp_path):
    """A signature change whose commit fixed NO caller (silence truth) + a graph FACT
    edge to a stale caller -> the engine fires -> counted as a full-population FP."""
    repo = tmp_path / "sil"
    _init(repo)
    _write(repo, "src/api.py", "def f(a):\n    return a\n")
    _write(repo, "src/caller.py", "def use():\n    return f(1)\n")
    _commit(repo, "init")
    _write(repo, "src/api.py", "def f(a, b):\n    return a\n")
    _commit(repo, "sig change WITHOUT touching the caller")

    res = mod.mine_repo("sil", str(repo))
    assert len(res.signature) == 1 and res.signature[0]["callers_fixed"] == []

    graph = repo / "graph.db"
    _make_graph(
        graph,
        nodes=[(1, "f", "src/api.py", 0), (2, "use", "src/caller.py", 0)],
        edges=[(2, 1, "CALLS", "import", 0.9, "src/caller.py", 2)],
    )
    score = mod.score_patch_delta(res.signature, str(repo), graph_db=str(graph),
                                  gt_index=None, limit=10)
    assert score["n_scored"] == 0  # no positive labels
    fullp = score["full_population"]
    assert fullp["silence_scored"] == 1, fullp
    assert fullp["silence_fp_as_designed"] == 1, fullp  # named the untouched caller
    assert fullp["as_designed_precision"] == 0.0


# ---------------------------------------------------------------------------
# F5 — per-family test-path filtering (syntax + companion)
# ---------------------------------------------------------------------------
def test_syntax_label_in_test_path_filtered(mod, tmp_path):
    """A broken->fixed pair in a TEST file is not a syntax-floor label (the floor
    guards agent edits to source); it is skipped and counted."""
    repo = tmp_path / "synt"
    _init(repo)
    _write(repo, "tests/test_mod.py", "def broken(:\n    pass\n")
    _commit(repo, "broken test file")
    _write(repo, "tests/test_mod.py", "def fixed():\n    pass\n")
    _commit(repo, "fix test file syntax")

    res = mod.mine_repo("synt", str(repo))
    assert res.syntax == []
    assert res.stats["syntax_test_paths_skipped"] >= 1


def test_companion_new_test_file_skipped_and_companions_split(mod, tmp_path):
    """A new TEST-dir file is never a companion label; and a label's changed files
    are split into companions (source) vs test_companions."""
    repo = tmp_path / "compt"
    _init(repo)
    _write(repo, "src/providers/aws.py", "def aws():\n    pass\n")
    _write(repo, "src/providers/gcp.py", "def gcp():\n    pass\n")
    _write(repo, "tests/plugins/test_aws.py", "def t():\n    pass\n")
    _write(repo, "tests/plugins/test_gcp.py", "def t():\n    pass\n")
    _write(repo, "src/registry.py", "P = {'aws': 1}\n")
    _commit(repo, "init families")
    _write(repo, "src/providers/azure.py", "def azure():\n    pass\n")
    _write(repo, "tests/plugins/test_azure.py", "def t():\n    pass\n")
    _write(repo, "src/registry.py", "P = {'aws': 1, 'azure': 3}\n")
    _commit(repo, "add azure + its test + register")

    res = mod.mine_repo("compt", str(repo))
    assert [c["new_file"] for c in res.companion] == ["src/providers/azure.py"], res.companion
    c = res.companion[0]
    assert c["companions"] == ["src/registry.py"]
    assert c["test_companions"] == ["tests/plugins/test_azure.py"]
    assert res.stats["companion_test_new_file_skipped"] >= 1


# ---------------------------------------------------------------------------
# F6 — _BatchCat mid-blob EOF must be an ERROR, never ("ok", partial)
# ---------------------------------------------------------------------------
def test_batchcat_truncated_stream_is_error(mod):
    import io
    from types import SimpleNamespace

    b = object.__new__(mod._BatchCat)
    b.repo = "unused"
    b.proc = SimpleNamespace(
        stdin=io.BytesIO(),
        stdout=io.BytesIO(b"deadbeef blob 100\npartial-then-eof"),
    )
    status, content = b.read("deadbeef", "x.py")
    assert status == "error" and content is None


# ---------------------------------------------------------------------------
# F4 — PROVENANCE records the run parameters + newest_mined_sha
# ---------------------------------------------------------------------------
def test_provenance_records_run_parameters(mod, tmp_path):
    import json

    repo = tmp_path / "prov"
    _init(repo)
    _write(repo, "src/a.py", "def a():\n    pass\n")
    _commit(repo, "init")
    out = tmp_path / "out"
    rc = mod.main(["--repo", str(repo), "--rev", "HEAD", "--max-commits", "50",
                   "--out", str(out)])
    assert rc == 0
    prov = json.loads((out / "PROVENANCE.json").read_text(encoding="utf-8"))
    rp = prov["run_parameters"]
    assert rp["max_commits"] == 50 and rp["rev"] == "HEAD"
    assert "score_limit" in rp and "silence_sample" in rp
    assert "--rev=--all" in rp["cli_note"]
    assert "newest_mined_sha" in prov["repos"][0]
    assert "head_sha" not in prov["repos"][0]
