r"""SM-6 (B) — the step-0 baked-brief reduction (GT_BRIEF_MINIMAL). 2026-07-11.

The plan: ``Brief step-0 -> obligations + calibrated minimal orientation`` — retire
graph-map and contract narration; HIGH keeps one file header, while MEDIUM/LOW keep
their compact contention and grep hedge. Reactive ``def_partition`` remains the
post-search follow-up channel.

CARDINAL invariants (the LIPI checks all by execution):
  * FLAG-GATED, DEFAULT-OFF, BYTE-IDENTICAL WHEN OFF — the reducer is invoked ONLY inside
    ``if _brief_minimal_on():`` at BOTH generate_v1r_brief return sites (AST pin), and
    ``_brief_minimal_on()`` is False for every off-token.
  * KEEPS obligations + calibrated minimal orientation; MEDIUM/LOW retain contention + hedge.
  * NO DARK GAP: ``def_partition`` is an enabled Profile-2 fact class + ``post_search`` a
    Profile-2 event bridge, so removing brief localization does not blind the agent.
  * LEAK = 0: the reducer never introduces a test identity (it only removes bytes).

BAKED-DORMANT HONESTY: the brief is generated at substrate-build time and consumed
read-only in-container, so this reduction has ZERO effect on any run until the SM-8 rebake
sets the flag at generation. These are STAGE-1 correctness pins, not a live-consumption claim.
"""
from __future__ import annotations

import ast
from pathlib import Path

from groundtruth.pretask import v1r_brief as v
from groundtruth.runtime import rl_profile as rp

_SRC = Path(v.__file__)


# A representative FULL matched brief carrying every block class the reducer must judge.
_FULL_BRIEF = "\n".join([
    '<gt-localization confidence="high">',
    "#1 src/importer.py -- edit target",
    "</gt-localization>",
    "<gt-graph-map>",
    "importer.py::set_fields",
    "  calls: db.py::set_parse",
    "</gt-graph-map>",
    "<gt-task-brief>",
    "1. src/importer.py (set_fields, run)",
    "   Callers: 3 callers in 2 files",
    "   Calls: set_parse, commit",
    "   Context: import task pipeline",
    "2. src/db.py (set_parse)",
    "   Callers: 1 caller",
    "",
    "Expected behavior: fields should persist across sessions",
    "",
    "EDIT-TARGET CONTRACTS (importer.py):",
    "  set_parse(self, key, string: str)",
    "",
    "Other candidates -- cross-file facts (so you need not open each first):",
    "src/util.py",
    "   Contract: util(a, b)",
    "",
    "Related files to inspect: db.py, util.py",
    "",
    "Scope chain (graph-connected, check ALL): importer.py -> db.py",
    "",
    "<gt-obligations>",
    "MUST persist fields across import sessions",
    "</gt-obligations>",
    "</gt-task-brief>",
])


# --------------------------------------------------------------------------- #
# FLAG — default OFF, every off-token off, on-tokens on.
# --------------------------------------------------------------------------- #
def test_flag_default_off(monkeypatch):
    monkeypatch.delenv("GT_BRIEF_MINIMAL", raising=False)
    assert v._brief_minimal_on() is False
    for off in ("", "0", "false", "no", "off", "OFF", "  0 "):
        monkeypatch.setenv("GT_BRIEF_MINIMAL", off)
        assert v._brief_minimal_on() is False, off
    for on in ("1", "true", "yes", "on", "2"):
        monkeypatch.setenv("GT_BRIEF_MINIMAL", on)
        assert v._brief_minimal_on() is True, on


# --------------------------------------------------------------------------- #
# BYTE-IDENTICAL-OFF (structural) — BOTH reducer call sites are gated by the flag.
# --------------------------------------------------------------------------- #
def test_both_reducer_call_sites_are_flag_gated():
    """Every ``_reduce_brief_to_minimal(...)`` call in v1r_brief MUST sit inside an
    ``if _brief_minimal_on():`` block. That is the byte-identical-off guarantee — with the
    flag off the reducer is unreachable, so generate_v1r_brief's output is unchanged."""
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    guarded = 0
    total = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test_src = ast.unparse(node.test)
        if "_brief_minimal_on" not in test_src:
            continue
        for call in ast.walk(node):
            if (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                    and call.func.id == "_reduce_brief_to_minimal"):
                guarded += 1
    for call in ast.walk(tree):
        if (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                and call.func.id == "_reduce_brief_to_minimal"):
            total += 1
    assert total >= 2, f"expected >=2 reducer call sites, found {total}"
    assert guarded == total, f"{total - guarded} reducer call site(s) NOT flag-gated"


def test_candidate_population_alignment_is_minimal_flag_gated():
    """The terminal-entry header rewrite must not alter historical full-mode bytes."""
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    guarded = 0
    total = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        if "_brief_minimal_on" not in ast.unparse(node.test):
            continue
        for call in ast.walk(node):
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == "_localization_header_for_entries"
            ):
                guarded += 1
    for call in ast.walk(tree):
        if (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "_localization_header_for_entries"
        ):
            total += 1
    assert total == 1
    assert guarded == total


# --------------------------------------------------------------------------- #
# KEEP — obligations + minimal orientation survive.
# --------------------------------------------------------------------------- #
def test_reduction_keeps_obligations_and_minimal_orientation():
    red = v._reduce_brief_to_minimal(_FULL_BRIEF)
    assert "<gt-task-brief>" in red and "</gt-task-brief>" in red      # scaffold
    assert "<gt-obligations>" in red and "MUST persist" in red          # obligations KEPT
    # minimal orientation = the file-entry HEADER lines (which file), NOT the contract body.
    assert "1. src/importer.py (set_fields, run)" in red
    assert "2. src/db.py (set_parse)" in red


def test_medium_minimal_preserves_compact_contention_and_honest_visible_count():
    """MEDIUM is a contention set, never a naked singular file assertion."""
    full = "\n".join([
        '<gt-localization confidence="medium">',
        "Candidate edit targets (reason over these — confirm the edit target with grep):",
        "  1. src/localizer-first.py — guessed_leaf",
        "  2. src/evidence-first.py — verified_leaf",
        "  3. .github/workflows/check.yml",
        "</gt-localization>",
        "<gt-task-brief>",
        "1. src/evidence-first.py (verified_leaf)",
        "   Callers: one",
        "2. src/localizer-first.py (guessed_leaf)",
        "   Callers: two",
        "</gt-task-brief>",
    ])

    red = v._reduce_brief_to_minimal(full)

    assert '<gt-localization confidence="medium">' in red
    assert "confirm the edit target with grep" in red
    assert "1. src/localizer-first.py" in red
    assert "2. src/evidence-first.py" in red
    # The file-entry headers would duplicate and contradict the contention block.
    assert red.count("src/localizer-first.py") == 1
    assert red.count("src/evidence-first.py") == 1
    visible = v._model_visible_localization_entries(
        red,
        [
            v.FileEntry(path="src/evidence-first.py", score=0.9),
            v.FileEntry(path="src/localizer-first.py", score=0.8),
            v.FileEntry(path=".github/workflows/check.yml", score=0.7),
            v.FileEntry(path="src/dark.py", score=0.6),
        ],
    )
    assert [entry.path for entry in visible] == [
        "src/evidence-first.py", "src/localizer-first.py", ".github/workflows/check.yml",
    ]
    candidate_ids = [v._localization_candidate_id(entry.path) for entry in visible]
    receipts = v._brief_block_receipts(
        red, localization_candidate_ids=candidate_ids,
    )
    localization_receipts = [
        receipt for receipt in receipts
        if receipt["fact_class"] == "localization"
    ]
    assert [receipt["candidate_id"] for receipt in localization_receipts] == candidate_ids
    assert all(
        receipt["label"] == "localization-header"
        for receipt in localization_receipts
    )
    assert len({tuple(receipt["char_span"]) for receipt in localization_receipts}) == len(
        candidate_ids
    )
    for receipt, entry in zip(localization_receipts, visible):
        start, end = receipt["char_span"]
        assert entry.path in red[start:end]


def test_contention_receipts_recover_normalization_equivalent_visible_paths():
    for visible_path in (
        "./.github/workflows/check.yml",
        "././src/module.py",
        "/src/absolute-tolerated.py",
    ):
        text = "\n".join((
            '<gt-localization confidence="medium">',
            f"  1. {visible_path}",
            "</gt-localization>",
        ))
        candidate_id = v._localization_candidate_id(visible_path)
        receipts = v._brief_block_receipts(
            text, localization_candidate_ids=[candidate_id],
        )
        assert len(receipts) == 1
        receipt = receipts[0]
        assert receipt["candidate_id"] == candidate_id
        start, end = receipt["char_span"]
        assert text[start:end] == f"  1. {visible_path}"


# --------------------------------------------------------------------------- #
# DROP — HIGH localization and heavy narration are retired.
# --------------------------------------------------------------------------- #
def test_reduction_drops_graph_map_and_localization():
    red = v._reduce_brief_to_minimal(_FULL_BRIEF)
    assert "<gt-graph-map>" not in red and "</gt-graph-map>" not in red
    assert "<gt-localization" not in red and "</gt-localization" not in red


def test_reduction_drops_contract_narration():
    red = v._reduce_brief_to_minimal(_FULL_BRIEF)
    for narration in ("EDIT-TARGET CONTRACTS", "Callers:", "Calls:", "Context:",
                      "Other candidates", "Related files", "Scope chain",
                      "Expected behavior"):
        assert narration not in red, narration


def test_reduction_is_clean_and_idempotent():
    red = v._reduce_brief_to_minimal(_FULL_BRIEF)
    # no double blank lines, no leading/trailing blank (a clean minimal brief).
    assert "\n\n\n" not in red
    assert red == red.strip()
    # re-reducing an already-minimal brief is a fixed point.
    assert v._reduce_brief_to_minimal(red) == red


def test_reduction_no_op_on_namematch_shape():
    """The no-match brief (scaffold + grep-orientation note + obligations) is ALREADY
    minimal — the reducer must be a no-op there (nothing to drop)."""
    nm = "\n".join([
        "<gt-task-brief>",
        "Note: GT found no indexed file ranked for this issue. Localize with grep.",
        "<gt-obligations>",
        "MUST handle the empty case",
        "</gt-obligations>",
        "</gt-task-brief>",
    ])
    assert v._reduce_brief_to_minimal(nm) == nm


# --------------------------------------------------------------------------- #
# LEAK = 0 — the reducer only REMOVES bytes; it can never introduce a test identity.
# --------------------------------------------------------------------------- #
def test_reduction_output_is_a_subsequence_no_new_bytes():
    """Every line in the reduced brief is a line that existed in the input (the reducer
    keeps whole kept-blocks + file HEADER lines verbatim, and only DROPS). So it can never
    manufacture a test name / FAIL_TO_PASS / assertion that was not already leak-checked
    upstream — the reduction is leak-monotone (it can only lower the leak surface)."""
    red = v._reduce_brief_to_minimal(_FULL_BRIEF)
    src_lines = set(_FULL_BRIEF.split("\n"))
    for ln in red.split("\n"):
        if ln.strip() == "":
            continue
        assert ln in src_lines, f"reducer introduced a NEW line: {ln!r}"


# --------------------------------------------------------------------------- #
# NO DARK GAP — the removed localization rides the reactive def_partition channel,
# which is a Profile-2 enabled fact class + post_search is a Profile-2 event bridge.
# --------------------------------------------------------------------------- #
def test_def_partition_covers_removed_brief_localization():
    m = rp.PROFILE_MANIFESTS["2"]
    assert "def_partition" in m.enabled_fact_classes          # the reactive localization fact
    assert "localization" in m.enabled_fact_classes           # the ranked-list fact class
    assert "post_search" in m.event_bridges                   # the channel that answers grep
    assert m.renderer_per_class.get("def_partition") == "grep-native"


def test_brief_minimal_is_a_profile_2_member_unmapped():
    assert "GT_BRIEF_MINIMAL" in rp.PROFILE_MEMBERS["2"]
    # baked-at-generation: NO importable backing module -> unmapped -> assumed available
    # (never a FALSE preflight abort; flag-off is byte-identical regardless).
    assert "GT_BRIEF_MINIMAL" not in rp._MEMBER_CAPABILITY_MODULE
    # profile fan-out flips it to "1" under Profile-2 (activated at the SM-8 rebake).
    assert rp.resolve_profile({"GT_RL_PROFILE": "2"}).get("GT_BRIEF_MINIMAL") == "1"


# --------------------------------------------------------------------------- #
# MUTATIONS — the drop-set + the header-only reduction are load-bearing.
# --------------------------------------------------------------------------- #
def test_mutation_1_drop_set_is_load_bearing(monkeypatch):
    """MUTATION 1: drop ``localization-header`` from the drop-set. The reducer would then
    KEEP ``<gt-localization>`` -> ``test_reduction_drops_graph_map_and_localization`` bites.
    Proven here by running the reducer against the mutated set and showing the leak."""
    mutant = frozenset(v._BRIEF_MINIMAL_DROP_LABELS - {"localization-header"})
    monkeypatch.setattr(v, "_BRIEF_MINIMAL_DROP_LABELS", mutant)
    red_mut = v._reduce_brief_to_minimal(_FULL_BRIEF)
    assert "<gt-localization" in red_mut          # mutant leaks the retired localization block
    monkeypatch.undo()
    assert "<gt-localization" not in v._reduce_brief_to_minimal(_FULL_BRIEF)  # real: retired


def test_mutation_2_header_only_reduction_is_load_bearing():
    """MUTATION 2: keep the WHOLE file-entry block (header + contract body) instead of only
    the header. The contract narration (Callers/Calls/Context) would then survive ->
    ``test_reduction_drops_contract_narration`` bites. Proven by a local mutant reducer."""
    def _mutant(text: str) -> str:
        blocks = v._segment_brief_blocks(text)
        kept = []
        for b in blocks:
            if b["label"] in v._BRIEF_MINIMAL_DROP_LABELS:
                continue
            kept.append(b["text"])   # MUTANT: whole block, not header-only
        return "\n".join(kept)

    assert "Callers:" in _mutant(_FULL_BRIEF)                                   # mutant leaks
    assert "Callers:" not in v._reduce_brief_to_minimal(_FULL_BRIEF)           # real: dropped
