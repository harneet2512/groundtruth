# Expected behavior for regression autopsy tests

Written from frozen artifacts ONLY, not from reading implementation code.

## Artifact inventory

- `lsp_13453/` — Qwen3-Coder lsp-hybrid arm, task astropy__astropy-13453
  - Baseline: RESOLVED (always). This GT run: FAILED.
  - 104 telemetry events. 5 steers, 5 edits, 3 ack_engagement, 3 ack_not_observed.
  - Agent patch: 2483 bytes, modifies astropy/io/ascii/html.py
  - trajectory.traj: full agent action history

- `nolsp_13453/` — Qwen3-Coder nolsp arm, same task
  - Baseline: RESOLVED (always). This GT run: FAILED.
  - 4 telemetry events (startup only). 0 edits. 0 patch bytes.

- `nolsp_13579/` — Qwen3-Coder nolsp arm, task astropy__astropy-13579
  - Baseline: RESOLVED (always). This GT run: FAILED.
  - 3 telemetry events (startup only). 0 edits. 0 patch bytes.

- `gold_13453.patch` — ground truth fix for 13453
  - Touches: astropy/io/ascii/html.py
  - 2 lines added: `self.data.cols = cols` and `self.data._set_col_formats()`

---

## Expected behaviors (derived from artifacts, not implementation)

### EB-1: lsp/13453 behavioral alignment

**Observed in artifact:** All 5 material_edit events target `astropy/io/ascii/html.py`.
All 5 steer_delivered events target `astropy/io/ascii/html.py`. The steered file
and the edited file are the same on every cycle where both occur.

**Expected classification:** This constitutes behavioral_alignment — the agent
edited the file GT steered it toward. Whether or not the agent ran an explicit
`gt_check` or `gt_ack` call is a separate question (explicit_ack). The fact that
the agent acted on the steered file IS behavioral evidence of following the steer.

A classifier that processes this trace must produce:
- `behavioral_alignment >= 1` (at least one edit matched a steered file)
- The specific value should be 5 (all 5 edits matched all 5 steers on the same file)

### EB-2: lsp/13453 steer relevance

**Observed in artifact:** All 5 steers target `astropy/io/ascii/html.py`.
The gold patch (`gold_13453.patch`) modifies `astropy/io/ascii/html.py`.

**Expected classification:** GT's steer is relevant — it points to a file the
gold patch actually touches. A steer relevance check must produce:
- `steer_targets_gold_file = true`

### EB-3: lsp/13453 low-information confirmation

**Observed in artifact:** The first steer arrives at cycle 14. The first
material_edit also occurs at cycle 14, touching `html.py`. The agent was ALREADY
editing html.py when the steer arrived (same cycle).

**Expected classification:** The steer confirmed what the agent was already
doing. It did not provide new localization. A usefulness classifier must produce:
- `low_information_confirmation = true` for the first steer (agent already editing that file)
- Steers 2-5 are even more obvious confirmations (agent has been editing html.py for cycles)

### EB-4: lsp/13453 steer repetition

**Observed in artifact:** 5 steer_delivered events, all targeting the same
file (`html.py`). Steers 2-5 repeat steer 1's target after the agent has
already demonstrated it is working on that file.

**Expected classification:**
- `repeated_steer_count = 4` (steers 2-5 repeat steer 1's target)
- A dedup system should have suppressed steers 3-5 at minimum (steer 2 is
  a reasonable single re-delivery; 3-5 are noise)

### EB-5: lsp/13453 NOT agent noncompliance

**Observed in artifact:** Agent edited the steered file 5 times. The agent
DID follow the steer behaviorally. The patch is wrong (16 lines vs gold 2
lines) but the agent was working on the correct file/function.

**Expected classification:**
- `agent_noncompliance = false`
- The failure is not "agent ignored correct steer" — it's "agent followed
  steer to correct file but produced wrong fix"

### EB-6: nolsp/13453 bootstrap failure

**Observed in artifact:** 4 telemetry events total: pre_edit_briefing (cycle 1),
checkpoint_startup (cycle 1), startup_complete (cycle 1), cycle (cycle 1, status
not recorded). Zero material_edit events. Zero steer events. Zero patch bytes.

**Expected classification:**
- `failure_class = bootstrap_infra_failure`
- `excluded_from_steer_effectiveness = true` (no steer was ever delivered;
  this task cannot be evidence for or against steer quality)
- Must NOT be classified as `agent_noncompliance` or `steer_harmful`

### EB-7: nolsp/13579 bootstrap failure (same pattern)

**Observed in artifact:** 3 telemetry events total: pre_edit_briefing (cycle 1),
checkpoint_startup (cycle 1), startup_complete (cycle 1). Zero material_edit.
Zero steers. Zero patch.

**Expected classification:** Same as EB-6.

### EB-8: negative control — agent edits DIFFERENT file than steered

This is a synthetic case for the negative control. If an agent receives a steer
for file A but edits file B:
- `behavioral_alignment = 0`
- `possible_noncompliance = true` (steer was specific, agent went elsewhere)

### EB-9: negative control — pre-steer edits don't count as alignment

If an agent edits file X at cycle 5, and a steer targeting file X arrives at
cycle 10, the cycle-5 edit must NOT count as behavioral_alignment. Only edits
AFTER steer delivery count. Temporal ordering matters.
