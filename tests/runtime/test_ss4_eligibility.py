"""SS-4 (2026-07-13) — the starved-producer ELIGIBILITY widening, flag-gated by
``GT_SS_ELIGIBILITY``.

MEASUREMENT (29-task audit of run 29236533134): 16/29 tasks reached the mini-seam
with the harness prefix ``cd $(cat /tmp/gt_root.txt) && <cmd>`` (command-substitution
path), which the W13 bare-token ``_CD_PREFIX_RE`` does NOT strip. So the isolation gate
(``_search_command_isolated``) rejected ~all real searches on those 16 tasks and
post_search / def_partition / loc_reslot / content_leg / sem_body were structurally
mute. The other 13 tasks used ``cd /testbed && …`` (bare-token), already handled.

ADMISSION: this is a STRUCTURAL widening (recognize the ``cd $(...)`` / ``cd "$(...)"``
/ ``cd `...` `` / ``cd $VAR`` command-substitution cd — a pure single-target cd, a
no-op for the grep's OWN zero/hit signal, exactly like the bare-token cd), NOT a
confidence drop. Measured over the 29 recorded trajectories with the real seam
predicates: +27 bare-symbol def_partition triggers + 35 broad loc_reslot triggers,
ALL from the ``cd $(...)`` prefix, 0 divergence on any non-widened command.

DEFAULT-OFF (byte-identical): flag unset -> the current W13 bare-token strip only.
Correct-or-quiet: a non-cd prefix / a ``;``/``||``/single-``&`` separator / a
nested-paren cd that cannot be cleanly parsed is NEVER stripped.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ARTIFACT = str(Path(__file__).resolve().parents[2] / "artifact_deepswe")
if _ARTIFACT not in sys.path:
    sys.path.insert(0, _ARTIFACT)

gmp = pytest.importorskip("gt_mini_patch")


# The universal arm-4 harness prefix (command substitution) + a bare-symbol grep.
_CD_DOLLAR = 'cd $(cat /tmp/gt_root.txt) && grep -n "default_cppstd" conans/detect.py'
_CD_DOLLAR_Q = 'cd "$(cat /tmp/gt_root.txt)" && grep -n default_cppstd conans/detect.py'
_CD_BARE = 'cd /testbed && grep -n "default_cppstd" conans/detect.py'


# ── ADMIT: the cd-$() prefix is stripped ONLY when the flag is ON ──────────────
def test_cd_dollar_isolated_and_pattern_when_flag_on(monkeypatch):
    # MUTATION 1 (revert the widened regex to bare-token only): this RED-flips.
    monkeypatch.setenv("GT_SS_ELIGIBILITY", "1")
    assert gmp._search_command_isolated(_CD_DOLLAR) is True
    assert gmp._search_pattern(_CD_DOLLAR) == "default_cppstd"


def test_cd_dollar_quoted_isolated_when_flag_on(monkeypatch):
    monkeypatch.setenv("GT_SS_ELIGIBILITY", "1")
    assert gmp._search_command_isolated(_CD_DOLLAR_Q) is True
    assert gmp._search_pattern(_CD_DOLLAR_Q) == "default_cppstd"


# ── byte-identical OFF: the cd-$() prefix is NOT stripped when the flag is unset ─
def test_cd_dollar_not_isolated_when_flag_off(monkeypatch):
    # MUTATION 2 (make the widened strip unconditional / ignore the flag): RED-flips.
    monkeypatch.delenv("GT_SS_ELIGIBILITY", raising=False)
    assert gmp._search_command_isolated(_CD_DOLLAR) is False
    # the strip helper is a strict no-op on the $() prefix when off (byte-identical)
    assert gmp._strip_leading_cd_prefix(_CD_DOLLAR) == _CD_DOLLAR


def test_cd_dollar_explicit_off_value_not_stripped(monkeypatch):
    monkeypatch.setenv("GT_SS_ELIGIBILITY", "0")
    assert gmp._search_command_isolated(_CD_DOLLAR) is False


# ── backward-compat: the bare-token cd (W13) works in BOTH flag states ──────────
def test_bare_cd_prefix_isolated_in_both_states(monkeypatch):
    # MUTATION 3 (drop the bare-token branch from the widened regex): the flag-ON
    # half RED-flips (cd /testbed stops being recognised when the flag is on).
    monkeypatch.delenv("GT_SS_ELIGIBILITY", raising=False)
    assert gmp._search_command_isolated(_CD_BARE) is True
    assert gmp._strip_leading_cd_prefix(_CD_BARE).startswith("grep")
    monkeypatch.setenv("GT_SS_ELIGIBILITY", "1")
    assert gmp._search_command_isolated(_CD_BARE) is True
    assert gmp._strip_leading_cd_prefix(_CD_BARE).startswith("grep")


# ── correct-or-quiet: only a PURE cd glued by `&&` is stripped, even with flag on ─
@pytest.mark.parametrize(
    "cmd",
    [
        'make && grep -n foo bar.py',                    # non-cd prefix
        'cd $(cat /tmp/gt_root.txt); grep -n foo bar.py',  # `;` separator
        'cd $(cat /tmp/gt_root.txt) || grep -n foo bar.py',  # `||` separator
        'pytest | grep -n foo',                          # pipe-fed grep (not first stage)
        'cd $(dirname $(pwd)) && grep -n foo bar.py',    # nested parens: unparseable -> keep reject
    ],
)
def test_correct_or_quiet_non_pure_cd_not_stripped(monkeypatch, cmd):
    # MUTATION 4 (widen the separator to also eat `;`/`||`/`&`, or accept a non-cd
    # prefix, or strip nested parens): one of these RED-flips.
    monkeypatch.setenv("GT_SS_ELIGIBILITY", "1")
    assert gmp._search_command_isolated(cmd) is False


def test_flag_on_does_not_change_bare_or_plain_grep_decisions(monkeypatch):
    """The widening touches ONLY commands carrying a cd-$()/`/$VAR prefix; a plain
    grep and a bare-token-cd grep decide identically ON and OFF (the measured
    '0 divergence on non-widened commands' invariant)."""
    plain = 'grep -rn "default_cppstd" .'
    for state in (None, "1"):
        if state is None:
            monkeypatch.delenv("GT_SS_ELIGIBILITY", raising=False)
        else:
            monkeypatch.setenv("GT_SS_ELIGIBILITY", state)
        assert gmp._search_command_isolated(plain) is True
        assert gmp._search_pattern(plain) == "default_cppstd"


# ── leak-safety is INHERITED (the widening only feeds the resolver more greps; the
#    resolver's own test-path / path-exclusion / >3-file guards still decide) ─────
def test_widening_preserves_bare_symbol_gate(monkeypatch):
    """A NON-bare pattern (regex/multi-token/path) still abstains from def_partition
    even under the widened strip — the bare-symbol gate is untouched (loc_reslot,
    not def_partition, owns those)."""
    monkeypatch.setenv("GT_SS_ELIGIBILITY", "1")
    regexy = 'cd $(cat /tmp/gt_root.txt) && grep -rn "def default_cppstd\\|def default_cstd" .'
    assert gmp._search_pattern(regexy) is None      # multi-token -> not a def_partition symbol
