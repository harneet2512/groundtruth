"""Tests for signal threshold constants — values and logging."""
from groundtruth.config.signal_thresholds import (
    SCOPE_MIN_CALLER_FILES,
    SCOPE_MIN_EDGE_CONFIDENCE,
    SCOPE_HIGH_RESOLUTION_METHODS,
    SPARSE_GRAPH_THRESHOLD,
    SIGNATURE_HIGH_CONFIDENCE_METHODS,
    SIGNATURE_MEDIUM_CONFIDENCE_METHODS,
    VERIFY_MIN_EDGE_CONFIDENCE,
    VERIFY_LABEL_HIGH_METHODS,
    VERIFY_LABEL_MEDIUM_METHODS,
    COCHANGE_HIGH_THRESHOLD,
    COCHANGE_MEDIUM_THRESHOLD,
    COCHANGE_WINDOW_COMMITS,
    log_threshold_use,
)


def test_scope_constants_sane():
    assert SCOPE_MIN_CALLER_FILES >= 2
    assert 0.0 < SCOPE_MIN_EDGE_CONFIDENCE <= 1.0
    assert SPARSE_GRAPH_THRESHOLD > 0
    assert "same_file" in SCOPE_HIGH_RESOLUTION_METHODS
    assert "import" in SCOPE_HIGH_RESOLUTION_METHODS


def test_signature_methods_disjoint():
    high = set(SIGNATURE_HIGH_CONFIDENCE_METHODS)
    medium = set(SIGNATURE_MEDIUM_CONFIDENCE_METHODS)
    assert high.isdisjoint(medium)


def test_verify_constants_sane():
    assert 0.0 < VERIFY_MIN_EDGE_CONFIDENCE <= 1.0
    assert "same_file" in VERIFY_LABEL_HIGH_METHODS


def test_cochange_thresholds_ordered():
    assert COCHANGE_MEDIUM_THRESHOLD < COCHANGE_HIGH_THRESHOLD
    assert COCHANGE_WINDOW_COMMITS >= 10


def test_log_threshold_use(capsys):
    log_threshold_use("TEST_THRESHOLD", 42, "unit_test")
    captured = capsys.readouterr()
    assert "[GT_CONFIG] TEST_THRESHOLD=42 context=unit_test" in captured.err
