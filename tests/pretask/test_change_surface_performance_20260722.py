"""Performance regressions for issue-bounded change-surface derivation.

These tests pin work, rather than a generous wall-clock timeout: repository
conventions unrelated to the issue must never trigger content scans.  That is
both deterministic in CI and directly guards the multiplicative operation that
made large untouched repositories exceed the live observation budget.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from groundtruth.pretask import change_surface


def _write(root: Path, rel: str, body: str = "pass\n") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def test_registry_content_scan_is_bounded_by_issue_matched_groups(
    tmp_path: Path, monkeypatch,
) -> None:
    """Many unrelated sibling families cost zero registry-content scans.

    The one requested entity selects the provider family.  It is scanned once,
    proving that adding unrelated repository conventions cannot multiply
    content I/O.

    The former eager implementation called ``_detect_registry`` once for every
    group before reading the issue, so this test is RED under that algorithm.
    """
    monkeypatch.setenv("GT_CHANGE_SURFACE", "1")
    _write(tmp_path, "providers/aws.py", "class AwsProvider:\n    pass\n")
    _write(tmp_path, "providers/gcp.py", "class GcpProvider:\n    pass\n")

    # Independent member names prevent the union-find grouping these unrelated
    # conventions into the requested provider family.
    for idx in range(48):
        _write(tmp_path, f"families/kind{idx}/left{idx}.py")
        _write(tmp_path, f"families/kind{idx}/right{idx}.py")

    scanned_group_ids: list[int] = []

    def record_scan(group, _files, _repo_root) -> None:
        scanned_group_ids.append(id(group))

    monkeypatch.setattr(change_surface, "_detect_registry", record_scan)
    result = change_surface.detect_change_surface(
        "Add azure provider support.",
        str(tmp_path),
        None,
    )

    assert result.entities == ["azure"]
    assert len(result.sibling_groups) > 20  # fixture genuinely has irrelevant work
    assert len(scanned_group_ids) == 1
    assert len(set(scanned_group_ids)) == 1


def test_registry_exact_regexes_run_only_for_line_member_candidates(
    tmp_path: Path, monkeypatch,
) -> None:
    """Unrelated code lines do not pay one exact regex search per member."""
    members = {f"member{idx}" for idx in range(96)}
    group = change_surface._Group(slots=[], members=members)
    _write(
        tmp_path,
        "module.py",
        "\n".join(f"value_{idx} = compute({idx})" for idx in range(160)) + "\n",
    )
    exact_searches = 0

    def counting_pattern(_member: str):
        def search(_line: str):
            nonlocal exact_searches
            exact_searches += 1
            return None

        return SimpleNamespace(search=search)

    monkeypatch.setattr(change_surface, "_ref_pattern", counting_pattern)
    change_surface._detect_registry(group, ["module.py"], str(tmp_path))

    assert exact_searches == 0
    assert group.registry_file is None


def test_registry_content_has_an_aggregate_live_hook_budget(monkeypatch) -> None:
    """The file-count cap cannot expand into hundreds of MiB of synchronous work."""
    group = change_surface._Group(slots=[], members={"aws", "gcp"})
    reads: list[str] = []

    def fixed_read(_root: str, rel: str) -> str:
        reads.append(rel)
        return "x" * 60

    monkeypatch.setattr(change_surface, "_MAX_REGISTRY_SCAN_CHARS", 100)
    monkeypatch.setattr(change_surface, "_read_text", fixed_read)
    change_surface._detect_registry(
        group,
        [f"module_{idx}.py" for idx in range(25)],
        "/unused",
    )

    # The second read discovers that accepting it would exceed the aggregate
    # budget; no later file is touched or allowed to mint evidence.
    assert reads == ["module_0.py", "module_1.py"]
    assert group.registry_file is None
