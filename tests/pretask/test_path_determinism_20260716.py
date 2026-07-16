"""Process-level determinism pin for the localization PATH component (D6).

Real defect (run 29534714080, task beeware__briefcase-2075): the Stage-1 brief
determinism gate reported "independent same-input brief acquisition produced
different canonical identities" with exactly three float-valued diffs, all on the
rank-1 candidate:
    /metrics/localization_proof/0/components/path
    /metrics/localization_proof/0/path_component
    /metrics/localization_proof/0/score
The two acquisitions run in SEPARATE python processes, so ``PYTHONHASHSEED``
differs and set/dict iteration order changes. The path component is produced by
``v7_4_brief._path_prior_scores`` (formerly an inline block in ``run_v74``). Its
directory-match pass reduced over the ``_issue_words`` SET but ``break``-ed on the
FIRST set-matching word and used THAT word's idf, so two processes could select a
different word -> a different ``0.4 * idf`` -> a different path component -> a
different ``score`` (path is a weighted term of score) -> a different canonical
brief identity.

The fix takes ``max`` over ALL set-matching words per directory part (no break):
``max`` is commutative + associative + EXACT for IEEE-754 floats, so the emitted
score is a pure function of the input SET {all_files, issue_text}, independent of
set-iteration order. This aligns the directory pass with the basename pass above it
(already a ``max`` reduction).

These tests exercise the REAL producer (``_path_prior_scores``) in fresh processes
across differing hash seeds and assert BYTE-identical emitted floats. Reliably RED
on the pre-fix ``break`` (empirically 3 distinct values across seeds), GREEN on the
``max``-over-set fix. Generic paths/words only -- the invariant must hold for ANY
input (no repo/task names in the logic).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"

# A rank-1-shaped candidate whose BASENAME matches no issue word (so the directory
# pass is the decisive contributor) and whose directory part ``netlocparser`` matches
# SEVERAL issue words with DISTINCT idf: 'net' (df high -> low idf), 'netloc'/'parser'
# (df low -> high idf), 'loc', 'parse'. Pre-fix, WHICH word the break selects (hence
# WHICH idf) depends on set-iteration order -> hash-seed-dependent score. The extra
# generic files inflate the document-frequency spread so the idf values differ.
_FILES = [
    "pkg/netlocparser/core.py",
    "a/net.py", "b/net2.py", "c/net3.py",
    "d/parser.py", "e/parserx.py", "f/loc.py",
    "g/one.py", "h/two.py", "i/three.py", "j/four.py", "k/five.py",
]
_ISSUE = "the net netloc parser loc parse handling is broken"
_TARGET = "pkg/netlocparser/core.py"

# Post-fix expected: directory pass takes max over matching words; 'netloc' is the
# highest-idf directory match (df==1 -> idf==1.0), so score == 0.4 * 1.0 == 0.4.
_EXPECTED_TARGET = 0.4


def _run_path_scores(hash_seed: int) -> dict:
    """Run the REAL producer ``_path_prior_scores`` in a fresh process at ``hash_seed``.

    Emits the full path-score map as canonically ordered JSON so the returned bytes
    encode the FULL-PRECISION floats (json float formatting is repr-faithful). Any
    ulp / order divergence in ANY entry changes the bytes.
    """
    script = r"""
import json, os, sys
sys.path.insert(0, os.environ["GT_TEST_SRC"])
from groundtruth.pretask.v7_4_brief import _path_prior_scores

files = json.loads(os.environ["GT_TEST_FILES"])
issue = os.environ["GT_TEST_ISSUE"]
scores = _path_prior_scores(files, issue)
sys.stdout.write(json.dumps(scores, sort_keys=True))
"""
    env = os.environ.copy()
    env.update({
        "PYTHONHASHSEED": str(hash_seed),
        "GT_TEST_SRC": str(SRC),
        "GT_TEST_FILES": json.dumps(_FILES),
        "GT_TEST_ISSUE": _ISSUE,
    })
    out = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        timeout=30,
    ).stdout
    return json.loads(out.decode("utf-8"))


def _run_path_scores_bytes(hash_seed: int) -> bytes:
    script = r"""
import json, os, sys
sys.path.insert(0, os.environ["GT_TEST_SRC"])
from groundtruth.pretask.v7_4_brief import _path_prior_scores

files = json.loads(os.environ["GT_TEST_FILES"])
issue = os.environ["GT_TEST_ISSUE"]
scores = _path_prior_scores(files, issue)
sys.stdout.write(json.dumps(scores, sort_keys=True))
"""
    env = os.environ.copy()
    env.update({
        "PYTHONHASHSEED": str(hash_seed),
        "GT_TEST_SRC": str(SRC),
        "GT_TEST_FILES": json.dumps(_FILES),
        "GT_TEST_ISSUE": _ISSUE,
    })
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        timeout=30,
    ).stdout


def test_path_component_identical_across_two_hash_seeds() -> None:
    """The defect, exactly: two processes, two hash seeds, one input set.

    Pre-fix (directory ``break``) the rank-1 directory score diverges across seeds
    -> RED. Post-fix (``max`` over the matching set) the FULL emitted-float bytes are
    identical -> GREEN.
    """
    bytes_a = _run_path_scores_bytes(hash_seed=0)
    bytes_b = _run_path_scores_bytes(hash_seed=1)
    assert bytes_a == bytes_b, (
        "path component diverged across processes (set-iteration-order leak): "
        f"{bytes_a!r} != {bytes_b!r}"
    )


def test_path_component_stable_across_many_hash_seeds() -> None:
    """Stronger pin: EVERY hash seed yields byte-identical full-precision floats."""
    baseline = _run_path_scores_bytes(hash_seed=0)
    for seed in (1, 2, 3, 7, 11, 42, 1000, 65535):
        got = _run_path_scores_bytes(hash_seed=seed)
        assert got == baseline, (
            f"path scores at PYTHONHASHSEED={seed} differ from seed 0 "
            f"(byte-level): {got!r} != {baseline!r}"
        )


def test_path_component_value_is_max_over_matching_dir_words() -> None:
    """Pin the SEMANTICS the fix chose: the directory pass takes the MAX-idf match.

    'netloc' is the highest-idf directory match (df==1 -> idf==1.0), so the target's
    score is 0.4*1.0. This proves the fix is not merely deterministic-by-truncation:
    it selects the strongest matching term (same policy as the basename pass), which
    also guarantees order-freeness (max is exact + commutative for floats).
    """
    scores = _run_path_scores(hash_seed=0)
    assert _TARGET in scores
    assert scores[_TARGET] == _EXPECTED_TARGET, (
        f"expected max-over-matching-words dir score {_EXPECTED_TARGET}, "
        f"got {scores[_TARGET]}"
    )
