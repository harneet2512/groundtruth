"""Pin structured obligation extraction at the live brief-generation boundary."""

from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]
_PROOF = _ROOT / "scripts" / "ci" / "substrate_proof.sh"


def test_substrate_proof_defaults_structured_obligations_on_in_both_branches():
    script = _PROOF.read_text(encoding="utf-8")

    assert script.count('GT_OBLIGATIONS_V2="${GT_OBLIGATIONS_V2:-1}"') == 2
    assert 'GT_OBLIGATIONS_V2="${GT_OBLIGATIONS_V2:-0}"' not in script
