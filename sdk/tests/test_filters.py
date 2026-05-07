from __future__ import annotations

from groundtruth.filters import DETERMINISTIC_METHODS, filter_edges, is_deterministic


def test_is_deterministic() -> None:
    assert is_deterministic("import")
    assert is_deterministic("same_file")
    assert not is_deterministic("name_match")


def test_filter_edges_deterministic_only() -> None:
    edges = [
        {"resolution_method": "name_match"},
        {"resolution_method": "import"},
    ]
    out = filter_edges(edges, deterministic_only=True)
    assert len(out) == 1
    assert out[0]["resolution_method"] == "import"


def test_filter_edges_prefer_deterministic_subset() -> None:
    edges = [
        {"resolution_method": "name_match"},
        {"resolution_method": "import"},
    ]
    out = filter_edges(edges, deterministic_only=False)
    assert [e["resolution_method"] for e in out] == ["import"]


def test_filter_edges_fallback_all_name_match() -> None:
    edges = [
        {"resolution_method": "name_match"},
        {"resolution_method": "name_match"},
    ]
    out = filter_edges(edges, deterministic_only=False)
    assert len(out) == 2


def test_deterministic_methods_frozen() -> None:
    assert "fqn" in DETERMINISTIC_METHODS


def test_filter_edges_normalizes_blank_method() -> None:
    edges = [{"resolution_method": None}, {"resolution_method": ""}]
    out = filter_edges(edges, deterministic_only=False)
    assert len(out) == 2
