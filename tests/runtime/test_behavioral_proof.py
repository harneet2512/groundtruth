from __future__ import annotations

import dataclasses

import pytest

from groundtruth.runtime import obligations as ob


SUBJECTS = {"inverse_match"}

CHECKED = """python3 - <<'PY'
from pkg.policy import inverse_match
print(f'allow: {inverse_match("allow")}')  # True
print(f'deny: {inverse_match("deny")}')  # False
PY"""
CHECKED_OUTPUT = "allow: True\ndeny: False"

MATRIX = """python3 - <<'PY'
from pkg.policy import Policy
policy = Policy()
for value in ("inverse_match_allow", "inverse_match_deny"):
    result = policy.inverse_match(value)
    print(f'{value} -> {result}')
PY"""
MATRIX_OUTPUT = (
    "inverse_match_allow -> inverse_match:True\n"
    "inverse_match_deny -> inverse_match:False"
)

CONSTRUCTOR_BOUND = """python3 - <<'PY'
from pkg.ignore import IgnoreMatcher
conanignore_path = '/tmp/config/.conanignore'
matcher = IgnoreMatcher(conanignore_path)
print(f'profiles entry: {matcher.matches("profiles/settings.yml")}')  # False
print(f'random entry: {matcher.matches("random.txt")}')  # True
PY"""
CONSTRUCTOR_BOUND_OUTPUT = "profiles entry: False\nrandom entry: True"


def test_checked_direct_calls_produce_immutable_scoped_proof():
    proof = ob.classify_checked_behavioral_proof(
        CHECKED, CHECKED_OUTPUT, 0, SUBJECTS, turn=15)

    assert proof is not None
    assert proof.subjects == frozenset({"inverse_match"})
    assert proof.turn == 15
    assert proof.kind == "direct_checked_calls"
    with pytest.raises(dataclasses.FrozenInstanceError):
        proof.turn = 16  # type: ignore[misc]


def test_checked_dynamic_matrix_produces_scoped_proof():
    proof = ob.classify_checked_behavioral_proof(
        MATRIX, MATRIX_OUTPUT, 0, SUBJECTS, turn=15)

    assert proof is not None
    assert proof.subjects == frozenset({"inverse_match"})
    assert proof.kind == "checked_result_matrix"


def test_constructor_bound_receiver_links_checked_calls_to_obligation_subject():
    proof = ob.classify_checked_behavioral_proof(
        CONSTRUCTOR_BOUND, CONSTRUCTOR_BOUND_OUTPUT, 0, {"conanignore"}, turn=15)

    assert proof is not None
    assert proof.subjects == frozenset({"conanignore"})
    assert proof.kind == "direct_checked_calls"


def test_literal_subject_label_does_not_make_unrelated_receiver_a_proof():
    command = """python3 - <<'PY'
from pkg.other import UnrelatedMatcher
matcher = UnrelatedMatcher('/tmp/config')
print(f'.conanignore allow: {matcher.matches("allow")}')  # True
print(f'.conanignore deny: {matcher.matches("deny")}')  # False
PY"""
    output = ".conanignore allow: True\n.conanignore deny: False"

    assert ob.classify_checked_behavioral_proof(
        command, output, 0, {"conanignore"}, turn=15) is None


@pytest.mark.parametrize(("command", "output", "returncode"), [
    (CHECKED, CHECKED_OUTPUT, 1),
    (CHECKED, CHECKED_OUTPUT, "0"),
    (CHECKED, CHECKED_OUTPUT, True),
    (CHECKED, CHECKED_OUTPUT, False),
    ("python3 -c \"print('allow: True'); print('deny: False')\"",
     CHECKED_OUTPUT, 0),
    ("""python3 - <<'PY'
from pkg.policy import inverse_match
print(f'allow: {inverse_match("allow") if False else True}')  # True
print(f'deny: {inverse_match("deny") if False else False}')  # False
PY""", CHECKED_OUTPUT, 0),
    ("""python3 - <<'PY'
from pkg.policy import inverse_match
allow = inverse_match("allow")
allow = True
deny = inverse_match("deny")
deny = False
print(f'allow: {allow}')
print(f'deny: {deny}')
PY""", CHECKED_OUTPUT, 0),
    ("""python3 - <<'PY'
from pkg.other import unrelated_check
print(f'allow: {unrelated_check("allow")}')  # True
print(f'deny: {unrelated_check("deny")}')  # False
PY""", CHECKED_OUTPUT, 0),
    ("""python3 - <<'PY'
from pkg.policy import Policy
policy = Policy()
for value, expected in (("inverse_match_allow", True), ("inverse_match_deny", False)):
    result = policy.inverse_match(value)
    result = expected
    print(f'{value} -> {result}')
PY""", MATRIX_OUTPUT, 0),
])
def test_unproven_shapes_are_rejected(command, output, returncode):
    assert ob.classify_checked_behavioral_proof(
        command, output, returncode, SUBJECTS, turn=15) is None


def test_checked_values_must_equal_original_observed_output():
    assert ob.classify_checked_behavioral_proof(
        CHECKED, "allow: True\ndeny: True", 0, SUBJECTS, turn=15) is None
    assert ob.classify_checked_behavioral_proof(
        CHECKED, CHECKED_OUTPUT + "\ncontradiction: False", 0, SUBJECTS, turn=15) is None


def test_proof_state_is_obligation_scoped_and_edit_freshness_bound():
    state = ob.BehavioralProofState()
    proof = ob.classify_checked_behavioral_proof(
        CHECKED, CHECKED_OUTPUT, 0, SUBJECTS, turn=15)
    assert proof is not None
    state.record(proof)

    assert state.covers({"inverse_match"}, after_turn=0)
    assert not state.covers({"normalize_path"}, after_turn=0)
    assert not state.covers({"inverse_match"}, after_turn=16)
    assert state.snapshot() == (proof,)


def test_rendered_subject_adapter_excludes_control_metadata():
    payload = (
        '<gt-nudge reason="test_evidence_gap">\n'
        '[edited, untested] "Exercise inverse_match before submit"\n'
        '</gt-nudge>'
    )
    groups = ob.rendered_obligation_subject_groups(payload)

    assert groups == (frozenset({"inverse_match"}),)
    assert all("test_evidence_gap" not in group for group in groups)


def test_public_subject_terms_preserve_plain_obligation_subject():
    assert ob.obligation_subject_terms(
        "Exercise conanignore before submit") == frozenset({"conanignore"})


def test_resurface_bullet_is_a_model_facing_subject():
    payload = (
        "before you submit, re-read the issue:\n"
        "  - Tekstowo backend does not return lyrics any more\n"
    )

    assert ob.rendered_obligation_subject_groups(payload) == (
        frozenset({"tekstowo", "backend", "does", "return", "lyrics", "more"}),
    )


def test_each_status_row_is_independent_and_exception_keeps_full_subject():
    payload = (
        '[edited, untested] "Given the documentation, I\'d expect the behavior to '
        'mirror `bambi.interpret`"\n'
        '[edited, untested] "I think a TypeError informing the user of the lack of '
        'support for categorical values would be helpful."\n'
    )

    groups = ob.rendered_obligation_subject_groups(payload)

    assert len(groups) == 2
    assert {"documentation", "mirror", "bambi.interpret"} <= groups[0]
    assert {"typeerror", "informing", "lack", "categorical", "values"} <= groups[1]


def test_edited_and_unaddressed_rows_are_both_parsed_without_cross_row_deletion():
    payload = (
        '[edited, untested] "Preserve normalize_path behavior for Windows paths"\n'
        '[not addressed] "Raise ConfigError when policy loading fails"\n'
    )

    groups = ob.rendered_obligation_subject_groups(payload)

    assert len(groups) == 2
    assert {"normalize_path", "windows", "paths"} <= groups[0]
    assert {"configerror", "policy", "loading", "fails"} <= groups[1]


def test_code_terms_do_not_delete_behavioral_terms_from_same_row():
    assert {
        "normalize_path", "preserve", "windows", "separators"
    } <= ob.obligation_subject_terms(
        "normalize_path must preserve Windows separators"
    )


def test_explicit_proposal_does_not_erase_another_real_requirement():
    payload = (
        '[edited, untested] "Handle Windows paths without changing separators"\n'
        '[edited, untested] "The parser must preserve symbolic links"\n'
    )

    assert ob.rendered_obligation_subject_groups(payload) == (
        frozenset({"handle", "windows", "paths", "without", "changing", "separators"}),
        frozenset({"parser", "preserve", "symbolic", "links"}),
    )


def test_contextual_comparison_alone_remains_an_obligation():
    payload = (
        '[edited, untested] "Given the documentation, I expect behavior to mirror '
        'the public parser"\n'
    )

    assert ob.rendered_obligation_subject_groups(payload)


def test_subject_bound_successful_test_runner_requires_complete_group():
    command = "python3 -m pytest test/plugins/test_lyrics.py -k Tekstowo -v"
    output = (
        "test/plugins/test_lyrics.py::TekstowoExtractLyricsTest::test_good_lyrics PASSED\n"
        "3 passed, 2 skipped, 27 deselected in 0.42s\n"
    )

    assert ob.classify_checked_behavioral_proof(
        command, output, 0,
        {"tekstowo", "backend", "lyrics"}, turn=15) is None

    proof = ob.classify_checked_behavioral_proof(
        command, output + "\nTekstowo backend lyrics", 0,
        {"tekstowo", "backend", "lyrics"}, turn=15)
    assert proof is not None
    assert proof.kind == "subject_bound_test_runner"


def test_direct_checked_calls_require_complete_group_not_one_matching_term():
    assert ob.classify_checked_behavioral_proof(
        CHECKED, CHECKED_OUTPUT, 0,
        {"inverse_match", "preserve_windows"}, turn=15) is None


@pytest.mark.parametrize(
    ("command", "output", "returncode"),
    [
        ("python3 -m pytest tests/test_unrelated.py -q", "1 passed in 0.1s", 0),
        ("python3 -m pytest test/plugins/test_lyrics.py -k Tekstowo -q",
         "3 passed in 0.1s", 1),
        ("python3 -m pytest test/plugins/test_lyrics.py -k Tekstowo -q",
         "1 failed, 3 passed in 0.1s", 0),
    ],
)
def test_test_runner_proof_requires_subject_binding_exact_rc_and_clean_green(
        command, output, returncode):
    assert ob.classify_checked_behavioral_proof(
        command, output, returncode,
        {"tekstowo", "backend", "does", "lyrics", "more"}, turn=15) is None


EXPECTED_EXCEPTION = """python3 - <<'PY'
import arviz as az
import numpy as np
x = np.array(['A', 'B', 'C'])
y = np.ones((1, 3, 3))
try:
    az.plot_hdi(x=x, y=y, smooth=True)
    print('ERROR: No exception raised!')
except TypeError as exc:
    print(f'OK: TypeError raised: {exc}')
except Exception as exc:
    print(f'Other exception: {type(exc).__name__}: {exc}')
x2 = np.arange(3)
try:
    az.plot_hdi(x=x2, y=y, smooth=True)
    print('OK: No exception')
except Exception as exc:
    print(f'Exception: {type(exc).__name__}: {exc}')
PY"""


def test_expected_exception_and_control_produce_behavioral_proof():
    output = (
        "OK: TypeError raised: categorical x values are not supported\n"
        "OK: No exception\n"
    )

    proof = ob.classify_checked_behavioral_proof(
        EXPECTED_EXCEPTION, output, 0, {"typeerror"}, turn=15)

    assert proof is not None
    assert proof.kind == "expected_exception_control"


def test_expected_exception_control_rejects_partially_observed_condition_description():
    output = (
        "OK: TypeError raised: categorical x values are not supported\n"
        "OK: No exception\n"
    )
    condition = ob.obligation_subject_terms(
        "The default smooth behavior throws an error for categorical values"
    )

    proof = ob.classify_checked_behavioral_proof(
        EXPECTED_EXCEPTION, output, 0, condition, turn=15)

    assert proof is None


def test_expected_exception_control_rejects_unrelated_condition_description():
    output = (
        "OK: TypeError raised: categorical x values are not supported\n"
        "OK: No exception\n"
    )

    assert ob.classify_checked_behavioral_proof(
        EXPECTED_EXCEPTION, output, 0,
        ob.obligation_subject_terms("Preserve symbolic links during extraction"),
        turn=15,
    ) is None

    # One coincidental term is not enough to bind a prose condition to the probe.
    assert ob.classify_checked_behavioral_proof(
        EXPECTED_EXCEPTION, output, 0,
        ob.obligation_subject_terms("Smooth symbolic-link extraction"),
        turn=15,
    ) is None


def test_condition_description_requires_a_specific_exception_handler():
    generic_handler = """python3 - <<'PY'
import arviz as az
import numpy as np
x = np.array(['A', 'B', 'C'])
y = np.ones((1, 3, 3))
try:
    az.plot_hdi(x=x, y=y, smooth=True)
    print('ERROR: No exception raised!')
except Exception as exc:
    print(f'OK: generic exception raised: {exc}')
x2 = np.arange(3)
try:
    az.plot_hdi(x=x2, y=y, smooth=True)
    print('OK: No exception')
finally:
    pass
PY"""
    output = (
        "OK: generic exception raised: categorical x values are not supported\n"
        "OK: No exception\n"
    )

    assert ob.classify_checked_behavioral_proof(
        generic_handler, output, 0,
        ob.obligation_subject_terms(
            "The default smooth behavior throws an error for categorical values"),
        turn=15,
    ) is None


@pytest.mark.parametrize(
    ("output", "returncode"),
    [
        ("ERROR: No exception raised!\nOK: No exception\n", 0),
        ("Other exception: ValueError: bad\nOK: No exception\n", 0),
        ("OK: TypeError raised: expected\nException: ValueError: bad\n", 0),
        ("OK: TypeError raised: expected\nOK: No exception\n", 1),
    ],
)
def test_expected_exception_proof_requires_expected_branch_control_and_exact_rc(
        output, returncode):
    assert ob.classify_checked_behavioral_proof(
        EXPECTED_EXCEPTION, output, returncode, {"typeerror"}, turn=15) is None
