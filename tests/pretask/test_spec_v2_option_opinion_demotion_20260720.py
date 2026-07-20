"""D-I (run6 batch3, haystack-8997 m61/m75, loguru m1): the v2 obligation
extractor promoted mutually-exclusive OPTION alternative bullets and speculative
discussion openers as firm completion obligations.

An issue cannot require "Option 1 AND Option 2" (they are alternatives —
contradictory to stack). Likewise "we could handle this a couple of ways" is the
author deliberating the solution space, not a requirement. Promoting either as a
"[not exercised]" obligation floods the agent with false requirements.

The extractor is RECALL-critical (docstring: the "aiomonitor killer" was
over-filtering). So the demotion is narrow: only explicit alternative-ENUMERATION
labels (Option/Alternative/Approach/Variant/Choice/Plan + enumerator) and
option-SPACE introductions ("a couple of ways", "one option is", "the options
are"), and even those are kept when they carry firm request grammar (must/should/
required) or an imperative head. Real requirements must survive untouched.
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO / "src") not in sys.path:
    sys.path.insert(0, str(_REPO / "src"))

# NOTE: extract_spec_v2 is the explicit v2 entrypoint — it needs no env var.
# Do NOT set os.environ here: a module-level GT_OBLIGATIONS_V2 mutation leaks
# into the shared pytest process and flips OTHER tests (test_v1r_brief_obligations)
# from their intended v1 path to v2 (CODING_LOOP anti-pattern 5b: state leakage).
from groundtruth.pretask.spec import extract_spec_v2  # noqa: E402


def _verbatims(issue: str) -> list[str]:
    return [o.verbatim_text for o in extract_spec_v2(issue).obligations]


# ─────────────────────────── the demotion (precision) ───────────────────────

def test_mutually_exclusive_option_bullets_are_not_obligations():
    issue = (
        "Title: improve return handling\n\n"
        "We could handle this a couple of ways.\n\n"
        "- Option 1: return None when the key is missing\n"
        "- Option 2: raise KeyError when the key is missing\n\n"
        "The function must validate the input path.\n"
    )
    vs = _verbatims(issue)
    assert not any(v.startswith("Option 1") for v in vs), "Option 1 must not be an obligation"
    assert not any(v.startswith("Option 2") for v in vs), "Option 2 must not be an obligation"
    assert not any("couple of ways" in v for v in vs), "solution-space opener must not be an obligation"
    # the genuine requirement survives
    assert any("validate the input path" in v for v in vs), "real requirement dropped — recall regression"


def test_alternative_and_approach_labels_demoted():
    issue = (
        "## Proposal\n"
        "- Alternative A: cache the result in memory\n"
        "- Alternative B: recompute on each call\n"
        "- Approach 2: add a config flag\n"
    )
    vs = _verbatims(issue)
    assert not any(v.startswith(("Alternative A", "Alternative B", "Approach 2")) for v in vs)


def test_one_option_is_intro_demoted():
    issue = "## Solution\nOne option is to normalize the path before lookup.\n"
    vs = _verbatims(issue)
    assert not any("One option is" in v for v in vs)


# ─────────────────────────── recall must not regress ────────────────────────

def test_firm_requirements_survive():
    issue = (
        "## Requirements\n"
        "- The parser must support several ways of specifying dates.\n"
        "- Add a timeout parameter to the client.\n"
        "- It should raise KeyError on a missing key.\n"
        "- Returns None when the input is empty.\n"
    )
    vs = _verbatims(issue)
    assert any("must support several ways" in v for v in vs), "firm modal kept even with 'several ways'"
    assert any("timeout parameter" in v for v in vs), "imperative requirement kept"
    assert any("raise KeyError" in v for v in vs), "should-modal requirement kept"
    assert any("Returns None" in v for v in vs), "declarative behavior kept"


def test_imperative_option_word_survives():
    # 'support several approaches' leads with an imperative verb -> kept
    issue = "## Requirements\n- Support several approaches for authentication.\n"
    vs = _verbatims(issue)
    assert any("Support several approaches" in v for v in vs), (
        "imperative head must override the option-space demotion")
