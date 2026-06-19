"""Prove vNext surfaces are active and produce correct behavioral output.

Uses the project_py fixture with a real SymbolStore to verify:
1. task_map emits Finding[] with file, symbol, confidence
2. event_brief returns empty when no diff (silent)
3. review_patch returns empty when no diff (silent)
4. Novelty filter suppresses repeated findings across surfaces
5. No [OK] noise, no empty <gt-evidence> wrappers
6. No reasoning_guidance footer in surface output
7. Format matches surface-tagged text specification
"""

from __future__ import annotations

import os
import time

import pytest

from groundtruth.index.graph import ImportGraph
from groundtruth.index.store import SymbolStore
from groundtruth.schema.novelty import NoveltyFilter
from groundtruth.utils.result import Ok


def _build_store() -> tuple[SymbolStore, str]:
    """Build a SymbolStore from the project_py fixture."""
    root = os.path.join(os.path.dirname(__file__), "..", "fixtures", "project_py")
    root = os.path.abspath(root)
    store = SymbolStore(":memory:")
    store.initialize()
    now = int(time.time())

    symbols = [
        ("get_user_by_id", "function", "src/users/queries.py", 9, 14, True,
         "(user_id: int) -> User", "user_id: int", "User"),
        ("create_user", "function", "src/users/queries.py", 17, 25, True,
         "(data: CreateUserInput) -> User", "data: CreateUserInput", "User"),
        ("update_user", "function", "src/users/queries.py", 28, 50, True,
         "(user_id: int, data: UpdateUserInput) -> User", "user_id: int, data: UpdateUserInput", "User"),
        ("delete_user", "function", "src/users/queries.py", 53, 56, True,
         "(user_id: int) -> None", "user_id: int", "None"),
        ("login", "function", "src/auth/login.py", 21, 37, True,
         "(email: str, password: str) -> LoginResult", "email: str, password: str", "LoginResult"),
        ("hash_password", "function", "src/utils/crypto.py", 1, 10, True,
         "(password: str) -> tuple[str, bytes]", "password: str", "tuple[str, bytes]"),
        ("validate_email", "function", "src/utils/validation.py", 1, 10, True,
         "(email: str) -> str", "email: str", "str"),
        ("sign_token", "function", "src/auth/jwt.py", 1, 10, True,
         "(payload: dict) -> str", "payload: dict", "str"),
        ("test_login_success", "function", "tests/test_auth.py", 5, 15, False,
         "()", None, None),
    ]

    sym_ids: dict[str, int] = {}
    for name, kind, fp, sl, el, exp, sig, params, ret in symbols:
        r = store.insert_symbol(
            name=name, kind=kind, language="python", file_path=fp,
            line_number=sl, end_line=el, is_exported=exp,
            signature=sig, params=params, return_type=ret,
            documentation=None, last_indexed_at=now,
        )
        if isinstance(r, Ok):
            sym_ids[name] = r.value

    # Add cross-file references
    refs = [
        ("get_user_by_id", "src/auth/login.py", 27),
        ("get_user_by_id", "src/users/queries.py", 30),
        ("get_user_by_id", "src/users/queries.py", 55),
        ("hash_password", "src/users/queries.py", 19),
        ("hash_password", "src/users/queries.py", 38),
        ("validate_email", "src/auth/login.py", 26),
        ("sign_token", "src/auth/login.py", 36),
        ("login", "tests/test_auth.py", 6),
    ]
    for sym_name, ref_file, ref_line in refs:
        if sym_name in sym_ids:
            store.insert_ref(sym_ids[sym_name], ref_file, ref_line, "import")

    for sid in sym_ids.values():
        store.update_usage_count(sid, 3)

    return store, root


@pytest.fixture()
def env():
    store, root = _build_store()
    graph = ImportGraph(store)
    return {"store": store, "graph": graph, "root": root}


# ── 1. task_map emits structured findings ────────────────────────────────


@pytest.mark.asyncio
async def test_task_map_emits_findings(env):
    from groundtruth.mcp.endpoints.task_map import handle_task_map

    result = await handle_task_map(
        issue_text="Bug in `get_user_by_id` — returns wrong user when ID is negative",
        store=env["store"],
        graph=env["graph"],
        root_path=env["root"],
    )
    findings = result["findings"]
    text = result["text"]

    assert len(findings) >= 1, "task_map must emit at least one finding"
    for f in findings:
        assert "kind" in f, "finding must have kind"
        assert "confidence" in f, "finding must have confidence"
        assert "location" in f, "finding must have location"
        assert f["location"]["file"], "location must have file"
        assert f["location"]["symbol"] or f["location"]["line"], "location must have symbol or line"

    assert text.startswith('<gt-evidence surface="task_map">'), "must use surface tag"
    assert text.endswith("</gt-evidence>"), "must close tag"
    assert "[OK]" not in text, "no [OK] noise in surface output"
    assert "reasoning_guidance" not in text, "no reasoning_guidance in surface output"
    assert "groundtruth_" not in text, "no cross-tool pointers in surface output"


# ── 2. event_brief is silent when no diff ────────────────────────────────


@pytest.mark.asyncio
async def test_event_brief_silent_no_diff(env):
    from groundtruth.mcp.endpoints.event_brief import handle_event_brief

    result = await handle_event_brief(
        file_path="src/users/queries.py",
        store=env["store"],
        graph=env["graph"],
        root_path="/tmp/nonexistent_no_git",
    )
    assert result["text"] == "", "event_brief must be silent when no diff"
    assert result["findings"] == [], "no findings when no diff"


# ── 3. review_patch is silent when no diff ───────────────────────────────


@pytest.mark.asyncio
async def test_review_patch_silent_no_diff(env):
    from groundtruth.mcp.endpoints.review_patch import handle_review_patch

    result = await handle_review_patch(
        store=env["store"],
        graph=env["graph"],
        root_path="/tmp/nonexistent_no_git",
    )
    assert result["text"] == "", "review_patch must be silent when no diff"
    assert result["findings"] == [], "no findings when no diff"


# ── 4. Novelty suppresses repeated findings across surfaces ──────────────


@pytest.mark.asyncio
async def test_novelty_suppresses_repeats(env):
    from groundtruth.mcp.endpoints.task_map import handle_task_map

    nf = NoveltyFilter()

    r1 = await handle_task_map(
        issue_text="Fix `get_user_by_id`",
        store=env["store"],
        graph=env["graph"],
        root_path=env["root"],
        novelty_filter=nf,
    )
    first_count = len(r1["findings"])
    assert first_count >= 1

    r2 = await handle_task_map(
        issue_text="Fix `get_user_by_id`",
        store=env["store"],
        graph=env["graph"],
        root_path=env["root"],
        novelty_filter=nf,
    )
    assert len(r2["findings"]) == 0, "repeated call must produce zero findings"
    assert r2["text"] == "", "repeated call must produce empty text"


# ── 5. No empty <gt-evidence> wrappers ───────────────────────────────────


@pytest.mark.asyncio
async def test_no_empty_wrapper(env):
    from groundtruth.mcp.endpoints.task_map import handle_task_map

    result = await handle_task_map(
        issue_text="something with no matching symbols at all xyzzy999",
        store=env["store"],
        graph=env["graph"],
        root_path=env["root"],
    )
    text = result["text"]
    if not result["findings"]:
        assert text == "", "empty findings must produce empty text, not empty wrapper"
    else:
        assert "<gt-evidence" in text


# ── 6. task_map findings have required fields ────────────────────────────


@pytest.mark.asyncio
async def test_task_map_finding_fields(env):
    from groundtruth.mcp.endpoints.task_map import handle_task_map

    result = await handle_task_map(
        issue_text="Bug in `login` function",
        store=env["store"],
        graph=env["graph"],
        root_path=env["root"],
    )
    for f in result["findings"]:
        assert f["kind"] in (
            "file_relevance", "import_path", "caller_expectation",
            "test_assertion", "caller_contract",
        ), f"unexpected kind: {f['kind']}"
        assert 0.0 <= f["confidence"] <= 1.0
        assert f["location"]["file"]


# ── 7. MCP server registers all 3 surfaces ──────────────────────────────


def test_server_registers_vnext_tools():
    """Verify the 3 vNext tools are registered in the actual MCP server."""
    import inspect
    from groundtruth.mcp.server import create_server

    source = inspect.getsource(create_server)
    assert "groundtruth_task_map" in source
    assert "groundtruth_event_brief" in source
    assert "groundtruth_review_patch" in source


# ── 8. Hook harness uses surface tags ────────────────────────────────────


def test_harness_uses_surface_tags():
    """Verify the hook harness emits surface-tagged output, not plain <gt-evidence>."""
    harness_path = os.path.join(
        os.path.dirname(__file__), "..", "..", "benchmarks", "swebench", "run_mini_gt_hooked.py"
    )
    with open(os.path.abspath(harness_path)) as f:
        source = f.read()

    assert 'surface="task_map"' in source, "harness must tag task_map output"
    assert 'surface="event_brief"' in source, "harness must tag event_brief output"
    assert 'surface="review_patch"' in source, "harness must tag review_patch output"
    assert "--findings-json" in source, "harness must use --findings-json flag"
    assert "_filter_novel_findings" in source, "harness must use host-side novelty"
    assert "_run_review_patch" in source, "harness must have review_patch function"


# ── 9. gt_intel.py supports --findings-json for both briefing and file modes


def test_gt_intel_findings_json_flag():
    """Verify gt_intel.py has --findings-json and --surface flags."""
    intel_path = os.path.join(
        os.path.dirname(__file__), "..", "..", "benchmarks", "swebench", "gt_intel.py"
    )
    with open(os.path.abspath(intel_path)) as f:
        source = f.read()

    assert "--findings-json" in source
    assert "--surface" in source
    assert "compute_findings_json" in source
    assert "format_findings_text" in source
    assert "enhanced_briefing" in source and "findings_json" in source, \
        "enhanced-briefing must have findings-json branch"
