"""A TEST file. Its path/name must NEVER appear in any delivered GT payload
(the leak invariant, S9). Present so the gate can prove the seam never surfaces
test identifiers."""
from pkg.mod_a import run


def test_run():
    assert run() == "a"
