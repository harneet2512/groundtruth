"""Tests for the judgment parity interface.

Verifies:
- JudgmentObligation normalization (paths, confidence rounding)
- ProductJudgmentAdapter wrapping of ObligationEngine
- Empty input handling
- Path normalization edge cases
- Parity: same input through adapter produces consistent normalized output
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock

from groundtruth.core.judgment import (
    JudgmentObligation,
    ProductJudgmentAdapter,
    normalize_obligation,
)


# ---------------------------------------------------------------------------
# Helpers — lightweight mock obligation to simulate ObligationEngine output
# ---------------------------------------------------------------------------


@dataclass
class _FakeObligation:
    """Mimics groundtruth.validators.obligations.Obligation fields."""

    kind: str
    source: str
    target: str
    target_file: str
    target_line: int | None
    reason: str
    confidence: float


def _fake_engine(
    infer_result: list[_FakeObligation] | None = None,
    patch_result: list[_FakeObligation] | None = None,
) -> MagicMock:
    engine = MagicMock()
    engine.infer.return_value = infer_result or []
    engine.infer_from_patch.return_value = patch_result or []
    return engine


# ---------------------------------------------------------------------------
# normalize_obligation tests
# ---------------------------------------------------------------------------


class TestNormalizeObligation:
    def test_basic_normalization(self) -> None:
        ob = normalize_obligation(
            kind="caller_contract",
            source="do_thing",
            target="call site in routes.py",
            target_file="src/routes.py",
            confidence=0.7,
        )
        assert ob.kind == "caller_contract"
        assert ob.source == "do_thing"
        assert ob.target == "call site in routes.py"
        assert ob.target_file == "src/routes.py"
        assert ob.confidence == 0.7

    def test_backslash_normalization(self) -> None:
        ob = normalize_obligation(
            kind="shared_state",
            source="Foo.bar",
            target="Foo.baz",
            target_file="src\\models\\foo.py",
            confidence=0.6,
        )
        assert ob.target_file == "src/models/foo.py"

    def test_leading_dot_slash_stripped(self) -> None:
        ob = normalize_obligation(
            kind="override_contract",
            source="Base.run",
            target="Sub.run",
            target_file="./src/sub.py",
            confidence=0.9,
        )
        assert ob.target_file == "src/sub.py"

    def test_leading_slash_stripped(self) -> None:
        ob = normalize_obligation(
            kind="override_contract",
            source="Base.run",
            target="Sub.run",
            target_file="/src/sub.py",
            confidence=0.9,
        )
        assert ob.target_file == "src/sub.py"

    def test_confidence_rounding(self) -> None:
        ob = normalize_obligation(
            kind="constructor_symmetry",
            source="Cls.__init__",
            target="Cls.__eq__",
            target_file="models.py",
            confidence=0.8549999999,
        )
        assert ob.confidence == 0.85

    def test_confidence_already_rounded(self) -> None:
        ob = normalize_obligation(
            kind="caller_contract",
            source="fn",
            target="caller",
            target_file="a.py",
            confidence=0.70,
        )
        assert ob.confidence == 0.7

    def test_mixed_path_separators(self) -> None:
        ob = normalize_obligation(
            kind="shared_state",
            source="X.a",
            target="X.b",
            target_file=".\\src\\utils/helpers.py",
            confidence=0.6,
        )
        assert ob.target_file == "src/utils/helpers.py"


# ---------------------------------------------------------------------------
# JudgmentObligation frozen dataclass tests
# ---------------------------------------------------------------------------


class TestJudgmentObligation:
    def test_equality(self) -> None:
        a = JudgmentObligation("caller_contract", "fn", "caller", "a.py", 0.7)
        b = JudgmentObligation("caller_contract", "fn", "caller", "a.py", 0.7)
        assert a == b

    def test_hashable(self) -> None:
        ob = JudgmentObligation("caller_contract", "fn", "caller", "a.py", 0.7)
        s = {ob}
        assert ob in s


# ---------------------------------------------------------------------------
# ProductJudgmentAdapter tests
# ---------------------------------------------------------------------------


class TestProductJudgmentAdapter:
    def test_infer_obligations_empty(self) -> None:
        adapter = ProductJudgmentAdapter(_fake_engine())
        result = adapter.infer_obligations("nonexistent")
        assert result == []

    def test_infer_from_diff_empty(self) -> None:
        adapter = ProductJudgmentAdapter(_fake_engine())
        result = adapter.infer_from_diff("")
        assert result == []

    def test_infer_obligations_wraps_correctly(self) -> None:
        fake_ob = _FakeObligation(
            kind="caller_contract",
            source="getUserById",
            target="call site in routes.py",
            target_file="src\\routes.py",
            target_line=47,
            reason="calls getUserById",
            confidence=0.7333,
        )
        engine = _fake_engine(infer_result=[fake_ob])
        adapter = ProductJudgmentAdapter(engine)

        result = adapter.infer_obligations("getUserById", file_context="src/users.py")

        engine.infer.assert_called_once_with("getUserById", file_context="src/users.py")
        assert len(result) == 1
        ob = result[0]
        assert isinstance(ob, JudgmentObligation)
        assert ob.kind == "caller_contract"
        assert ob.source == "getUserById"
        assert ob.target == "call site in routes.py"
        assert ob.target_file == "src/routes.py"
        assert ob.confidence == 0.73

    def test_infer_from_diff_wraps_correctly(self) -> None:
        fake_ob = _FakeObligation(
            kind="override_contract",
            source="Base.process",
            target="Sub.process",
            target_file="./lib/sub.py",
            target_line=10,
            reason="overrides Base.process",
            confidence=0.9,
        )
        engine = _fake_engine(patch_result=[fake_ob])
        adapter = ProductJudgmentAdapter(engine)

        diff = "--- a/lib/base.py\n+++ b/lib/base.py\n@@ -1 +1 @@\n-def process():\n+def process(x):"
        result = adapter.infer_from_diff(diff)

        engine.infer_from_patch.assert_called_once_with(diff)
        assert len(result) == 1
        assert result[0].target_file == "lib/sub.py"
        assert result[0].confidence == 0.9

    def test_multiple_obligations_normalized(self) -> None:
        fakes = [
            _FakeObligation(
                kind="constructor_symmetry",
                source="Cls.__init__",
                target="Cls.__eq__",
                target_file="src\\models.py",
                target_line=5,
                reason="missing attrs",
                confidence=0.856,
            ),
            _FakeObligation(
                kind="shared_state",
                source="Cls.x",
                target="Cls.update",
                target_file="./src/models.py",
                target_line=20,
                reason="shared attr",
                confidence=0.6001,
            ),
        ]
        adapter = ProductJudgmentAdapter(_fake_engine(infer_result=fakes))

        result = adapter.infer_obligations("Cls")

        assert len(result) == 2
        assert result[0].target_file == "src/models.py"
        assert result[0].confidence == 0.86
        assert result[1].target_file == "src/models.py"
        assert result[1].confidence == 0.6


# ---------------------------------------------------------------------------
# Parity consistency: same data in → same normalized output
# ---------------------------------------------------------------------------


class TestParityConcistency:
    """Verifies that repeated normalization produces identical results."""

    def test_same_input_same_output(self) -> None:
        """Adapter produces identical JudgmentObligations for identical input."""
        fake_ob = _FakeObligation(
            kind="caller_contract",
            source="save",
            target="caller in views.py",
            target_file="src\\views.py",
            target_line=99,
            reason="calls save",
            confidence=0.7,
        )
        engine1 = _fake_engine(infer_result=[fake_ob])
        engine2 = _fake_engine(infer_result=[fake_ob])

        r1 = ProductJudgmentAdapter(engine1).infer_obligations("save")
        r2 = ProductJudgmentAdapter(engine2).infer_obligations("save")

        assert r1 == r2

    def test_normalize_idempotent(self) -> None:
        """Normalizing an already-normalized path is a no-op."""
        ob1 = normalize_obligation("x", "a", "b", "src/foo.py", 0.5)
        ob2 = normalize_obligation("x", "a", "b", ob1.target_file, ob1.confidence)
        assert ob1 == ob2
