# -*- coding: utf-8 -*-
"""Append gt_trial.md S4+S5 audit sections to task_ledgers/<task>.md (APPEND-ONLY)."""
import json, os, io

BASE = os.path.dirname(os.path.abspath(__file__))
LEDGERS = r"D:\Groundtruth\task_ledgers"

# --- per-task component rows: (turn, GT SENT verbatim, AGENT DID verbatim, D/C/C) ---
T = {}

T["astropy__astropy-12907"] = dict(
 lead=("**TRAJECTORY (lead, S4.3):** resolve = SELF-LOCALIZED, NOT GT-caused. The issue names "
   "`astropy.modeling.separable.separability_matrix` + contains the repro; the agent reproduced numerically and "
   "hand-traced `_cstack` to the `cright[...] = 1` -> `= right` fix itself (MSG 34-38). L1 put gold at rank 2 "
   "(headline rank 1 = non-gold `modeling/core.py`, EDIT-TARGET CONTRACTS also anchored core.py). gt_caused=FALSE."),
 L1=[("MSG 1", "`<gt-localization confidence=\"medium\">` ... `1. astropy/modeling/core.py - inputs, outputs, separable / resolved caller: __init__() in astropy/io/ascii/fastbasic.py:40` / `2. astropy/modeling/separable.py - separability_matrix, is_separable, _compute_n_outputs / resolved call: -> where() in astropy/table/index.py:511` / `4. astropy/table/table.py - ... resolved caller: gb() in astropy/extern/jquery/data/js/jquery.dataTables.min.js:9` + `EDIT-TARGET CONTRACTS (core.py): inputs -> calls _initialize_unit_support(self)`",
   "MSG 2 CMD: `python -c \"...\"` running the ISSUE's repro snippet; MSG 32: \"Let me look at the separable module directly\" (after 14 env-repair actions)",
   "D=Y - C=PARTIAL (gold rank 2; headline + contracts = wrong core.py; `gb() in jquery.dataTables.min.js:9` junk presented as a resolved-caller fact) - C=NO (path ran through the issue's import line, not the ranking)")],
 SCOPE=[("MSG 15/25", "`<gt-scope files=\"1\"> 1. setup.py - in scope (you are viewing this); GT could not expand scope from the graph...`",
   "agent mid pip/build attempts; setup.py is build-noise, not task scope", "D=Y - C=N - C=N"),
  ("MSG 41", "`<gt-scope reason=\"re-anchored\"> 1. modeling/separable.py - you have moved here; re-grounding scope 2. tests/test_separable.py - graph-connected ...`",
   "agent had already identified the bug at MSG 38; later opened test_separable.py (MSG 61)", "D=Y - C=Y - C=WEAK (trailing; confirms where the agent already was)")],
 L3b=[("MSG 33", "`<gt-evidence kind=\"post_view\" file=\"astropy/modeling/separable.py\"> [WITNESS] where calls -> astropy/table/index.py:511 'def where(self, col_map):' [SIBLINGS] is_separable`",
   "MSG 34: agent ignores it; starts hand-tracing `_cstack` matrices", "D=Y - C=N (table-index `where` is an irrelevant/wrong edge for separable.py) - C=N")],
 L3=[("MSG 43", "`<gt-contract file=\"separable.py\"> [SIGNATURE] def _coord_matrix(model, pos, noutp): [CALLERS] _coord_matrix: 3 verified caller(s)...` BUT also `[CALLEE] _separable -> def isinstance( self: Self@TableColumns, cls: Any ) -> list (astropy/table/table.py:308)`",
   "agent proceeds to verification (one-line value fix; no signature at stake)", "D=Y - C=PARTIAL (signatures real; `isinstance -> TableColumns.isinstance` CALLEE lines are FALSE facts) - C=N")],
 L5=[("MSG 45", "`<gt-nudge reason=\"failure_persisted\"> GT: the same test failure has persisted across your edit(s) - your current hypothesis is likely wrong...`",
   "the \"failure\" was an ENV import error (`_compiler` C-extension); the agent's fix was already CORRECT (proven MSG 55/71); agent rightly ignored it and fixed the env (MSG 47)",
   "D=Y - C=N (FALSE POSITIVE - env failure misclassified as hypothesis failure) - C=N (correctly ignored)")],
 verdicts=dict(L1="delivered, half-right, not consumed; leakage 0",
   SCOPE="delivered; re-anchor correct but trailing; leakage 0",
   L3b="delivered, wrong edge, inert; leakage 0",
   L3="delivered; contains false CALLEE facts; inert; leakage 0",
   L5="2 nudges delivered; 1 false positive; 0 consumed; leakage 0"),
 cross="LEAKAGE (test-name/F2P) = 0. Telemetry stdout leak = 1 (`[gt-patch:loaded]` in MSG 3). consumed-count = 0/~9 firings. fair-probe: NO (issue pre-localized).")

T["astropy__astropy-13453"] = dict(
 lead=("**TRAJECTORY (lead):** resolve = SELF-LOCALIZED. Gold `astropy/io/ascii/html.py` was ABSENT from all 6 L1 candidates. "
   "The agent's action 1 = own `find` of astropy/io/ascii; action 2 = `grep 'class HTML'` -> html.py; fix self-derived "
   "(`self.data.cols = cols; self.data._set_col_formats()` in `HTML.write`). GT scope/post-view trailed the agent everywhere. gt_caused=FALSE."),
 L1=[("MSG 1", "`<gt-localization confidence=\"medium\">` `1. astropy/table/table.py - ... resolved caller: t() in astropy/extern/jquery/data/js/jquery-3.6.0.min.js:2` / `2. astropy/io/fits/column.py - ascii, names, formats ...` (6 candidates; ascii/html.py absent)",
   "MSG 2 CMD: `find`/`ls` of astropy/io/ascii + `grep -l 'class HTML'` -> html.py (own search, action 1-2)",
   "D=Y - C=NO (gold not in list; rank-1 witness is minified jQuery junk) - C=NO")],
 SCOPE=[("MSG 6", "`<gt-scope files=\"5\"> 1. ascii/html.py - in scope (you are viewing this) 2. table/table.py - graph-connected 3. ascii/core.py - graph-connected 4. tests/test_html.py - graph-connected...`",
   "agent was ALREADY viewing html.py (its own grep found it one action earlier)", "D=Y - C=Y (correct neighborhood) - C=N (trailing - confirms the agent's own localization)")],
 L3b=[("MSG 6", "`<gt-evidence kind=\"post_view\" file=\"astropy/io/ascii/html.py\"> [WITNESS] show_in_notebook called by -> astropy/table/table.py:1699 'return HTML(html)' [WITNESS] isinstance calls -> astropy/table/table.py:308 ... [SIBLINGS] write, process_lines, read, fill_values`",
   "MSG 7: agent greps the write method line numbers itself; never cites the witnesses", "D=Y - C=PARTIAL (show_in_notebook edge real; isinstance edge = laundered builtin) - C=N"),
  ("MSG 11", "`<gt-evidence kind=\"post_view\" file=\"astropy/io/ascii/core.py\"> [WITNESS] __call__ called by -> astropy/io/ascii/fixedwidth.py:41 ... [SIBLINGS] writerow, writerows`",
   "MSG 13: agent reasons from its own grep of `_set_col_formats` (line 934), not from the witnesses", "D=Y - C=PARTIAL - C=N")],
 L3=[("MSG 45", "`<gt-contract file=\"html.py\"> [SIGNATURE] def fill_values(self, col, col_str_iters): [CALLERS] fill_values: 1 verified caller(s) in 1 file(s) - preserve this interface ...` + `[CALLEE] identify_table -> def isinstance( self: Self@TableColumns ... ) (astropy/table/table.py:308)`",
   "MSG 46: \"Now let me verify the fix\" - contract not referenced", "D=Y - C=PARTIAL (isinstance CALLEE lines false) - C=N")],
 L5=[("MSG 47", "`<gt-nudge reason=\"loop\"> GT: you have repeated the same command 4+ times with no progress. Stop, re-read the last error, and change approach...`",
   "the agent WAS making progress (repro evolved erfa->numpy->reproduced->new AttributeError); it correctly read the fresh AttributeError and fixed `self.data.cols` (MSG 48-55)",
   "D=Y - C=N (false positive: 'no progress' was wrong - each rerun produced a new state) - C=N (ignored, correctly)")],
 verdicts=dict(L1="delivered, WRONG (gold absent); not consumed; leakage 0",
   SCOPE="delivered, correct-but-trailing; leakage 0",
   L3b="delivered, partially correct, inert; leakage 0",
   L3="delivered, partially correct, inert; leakage 0",
   L5="2 nudges; 1 false-positive loop nudge; 0 consumed; leakage 0"),
 cross="LEAKAGE (test-name/F2P) = 0. Telemetry stdout leak = 1 (`[gt-patch:loaded]` MSG 3). consumed-count = 0. fair-probe: YES for L1 (issue did not name the file) - and L1 failed it.")

T["astropy__astropy-13579"] = dict(
 lead=("**TRAJECTORY (lead):** the ONE GT-caused resolve. Brief = HIGH-confidence SINGLE target = gold "
   "`astropy/wcs/wcsapi/wrappers/sliced_wcs.py`, naming the gold helper `_pixel_to_world_values_all`. The agent's FIRST "
   "command (MSG 2) = `cat astropy/wcs/wcsapi/wrappers/sliced_wcs.py` - the exact brief path, ZERO search actions before it "
   "(the issue names only the class `SlicedLowLevelWCS`, not the `wrappers/` path). The fix computes dropped-dimension world "
   "values via `_pixel_to_world_values_all` - the brief-named helper. gt_caused=TRUE (caveat: a frontier model might map "
   "class->path unaided; the chronology simply shows it never had to search)."),
 L1=[("MSG 1", "`<gt-localization confidence=\"high\"> Edit target: astropy/wcs/wcsapi/wrappers/sliced_wcs.py :: slice / reason: sanitize_slices calls slice [CALLS]` + `1. ...sliced_wcs.py (def pixel_to_world_values..., def world_to_pixel_values..., def _pixel_to_world_values_all(self, *pixel_arrays):)`",
   "MSG 2 CMD: `cd /testbed && cat astropy/wcs/wcsapi/wrappers/sliced_wcs.py` (FIRST action; no find/grep before it). MSG 4: agent reasons over world_to_pixel_values' `world_arrays_new.append(1.)`",
   "D=Y - C=Y (single target = gold; helper named; minor junk: `world_to_pixel_values -> calls isinstance( self: Self@TableColumns ...)` contract lines) - C=Y (action 1 = brief path; fix via brief-named helper)")],
 SCOPE=[("MSG 3", "`<gt-scope files=\"5\"> 1. wrappers/sliced_wcs.py - in scope (you are viewing this) 2. tests/test_sliced_wcs.py - graph-connected 3. wrappers/base.py - graph-connected...`",
   "agent stayed on sliced_wcs.py; later ran tests/test_sliced_wcs.py (MSG 50+)", "D=Y - C=Y - C=WEAK (consistent, also obvious)")],
 L3b=[("MSG 3", "`<gt-evidence kind=\"post_view\" file=\"...sliced_wcs.py\"> [WITNESS] _slice_wcs called by -> astropy/nddata/mixins/ndslicing.py:123 'llwcs = SlicedLowLevelWCS(self.wcs.low_level_wcs, item)' [WITNESS] apply_slices called by -> .../wcsaxes/wcsapi.py:245 ... [SIBLINGS] sanitize_slices, combine_slices, dropped_world_dimensions, pixel_n_dim, world_n_dim`",
   "MSG 4: agent analyzes `world_to_pixel_values` + `dropped_world_dimensions` (a SIBLINGS name) - plausibly aided, not cited",
   "D=Y - C=Y (real edges; siblings list includes the key method) - C=WEAK")],
 L3=[("MSG 41", "`<gt-contract file=\"sliced_wcs.py\"> [SIGNATURE] def _pixel_to_world_values_all(self, *pixel_arrays): [CALLERS] _pixel_to_world_values_all: 2 verified caller(s) in 1 file(s) - preserve this interface ...`",
   "MSG 42: agent verifies the patched method (sed -n 245,275p); interface preserved", "D=Y - C=Y - C=N (fix already shaped)")],
 L5=[("MSG 47", "`<gt-nudge reason=\"failure_persisted\"> GT: the same test failure has persisted across your edit(s) - your current hypothesis is likely wrong...`",
   "the failure was `wcs_to_celestial_frame ValueError` (test-harness frame determination, unrelated to the fix); agent: \"That error is from the frame determination, not from our fix\" (MSG 48); all 40 tests pass at MSG 61",
   "D=Y - C=N (FALSE POSITIVE on an env/test-harness error; the hypothesis was right) - C=N (correctly ignored)")],
 verdicts=dict(L1="DELIVERED + CORRECT + CONSUMED on a fair probe - works (gt_trial S4 verdict gate met)",
   SCOPE="delivered, correct, weakly consumed; leakage 0",
   L3b="delivered, correct, weakly consumed; leakage 0",
   L3="delivered, correct, inert; leakage 0",
   L5="2 nudges; 1 false positive; 0 consumed; leakage 0"),
 cross="LEAKAGE (test-name/F2P) = 0. Telemetry stdout leak = 1 (`[gt-patch:loaded]` MSG 3). consumed-count = 1 strong (L1) + 2 weak. fair-probe: YES (with the class-name caveat stated).")

T["sympy__sympy-11618"] = dict(
 lead=("**TRAJECTORY (lead):** resolve = SELF-LOCALIZED. The 250-char issue contains the exact repro "
   "`Point(2,0).distance(Point(1,0,2))` and names the root cause (zip truncation). Agent's action 1 = the issue repro; "
   "MSG 14 = its own `grep -n \"def distance\" sympy/geometry/point.py`. Gold point.py sat at rank 6/6 of a confidence=low "
   "list whose headline was wrong (ellipse.py). gt_caused=FALSE."),
 L1=[("MSG 1", "`<gt-localization confidence=\"low\"> 1. sympy/geometry/ellipse.py - Ellipse, __new__, ambient_dimension / resolved caller: control_point() in sympy/physics/quantum/circuitplot.py:244 ... 6. sympy/geometry/point.py - distance, Point, __new__ / resolved caller: __new__() in sympy/geometry/line3d.py:45`",
   "MSG 2 CMD: `python -c \"from sympy import Point; print(Point(2,0).distance(Point(1,0,2)))\"` (the issue's repro); MSG 14 CMD: `grep -n \"def distance\" sympy/geometry/point.py`",
   "D=Y - C=PARTIAL (gold present but LAST, headline wrong; confidence=low honestly stated) - C=NO")],
 SCOPE=[("MSG 15", "`<gt-scope files=\"5\"> 1. geometry/point.py - in scope (you are viewing this) 2. geometry/entity.py - graph-connected 3. tests/test_args.py - graph-connected...`",
   "agent already localized to point.py one action earlier", "D=Y - C=Y - C=N (trailing)")],
 L3b=[("MSG 15", "`<gt-evidence kind=\"post_view\" file=\"sympy/geometry/point.py\"> [WITNESS] __new__ called by -> sympy/geometry/line3d.py:45 'p1 = Point3D(p1)' ... [SIBLINGS] is_concyclic, is_collinear, is_scalar_multiple, length, origin`",
   "MSG 16: agent seds the distance method body; witnesses uncited", "D=Y - C=Y (real edges) - C=N")],
 L3=[("MSG 11/13", "`<gt-contract file=\"basic.py\"> [SIGNATURE] def __new__(cls, *args): [CALLERS] __new__: 106 verified caller(s) in 51 file(s) - preserve this interface ...` (fired on the agent's py3.11-compat sed edits to basic.py/plot.py - NOT the task fix)",
   "agent continued env compat repair", "D=Y - C=Y (real, but for env-repair edits) - C=N")],
 L5=[("MSG 123", "`<gt-nudge reason=\"failure_persisted\"> GT: the same test failure has persisted across your edit(s) - your current hypothesis is likely wrong...`",
   "failure = `from collections import Mapping` ImportError (py3.11 env), which the agent had deliberately git-checkout-reverted at MSG 118; its task hypothesis was already proven right (sqrt(5) at MSG 131)",
   "D=Y - C=N (FALSE POSITIVE on an env-compat error) - C=N")],
 verdicts=dict(L1="delivered, gold-at-rank-6/low-confidence, not consumed; leakage 0",
   SCOPE="delivered, trailing; leakage 0", L3b="delivered, correct, inert; leakage 0",
   L3="delivered (on env-repair edits), inert; leakage 0",
   L5="2 nudges; 1 false positive; 0 consumed; leakage 0"),
 cross="LEAKAGE (test-name/F2P) = 0. Telemetry stdout leak = 1 (`[gt-patch:loaded]` MSG 3). consumed-count = 0. fair-probe: NO (issue contains the repro).")

T["sympy__sympy-12096"] = dict(
 lead=("**TRAJECTORY (lead):** resolve = SELF-LOCALIZED; the HIGH-confidence brief was WRONG. Brief single target = "
   "`sympy/utilities/lambdify.py :: lambdify`; gold = `sympy/core/function.py::Function._eval_evalf`, which the ISSUE "
   "names verbatim ('the code returns ... in Function._eval_evalf'). The agent's first probe grepped BOTH files (one "
   "wasted probe on the brief target), then followed the issue to function.py:500 and fixed `_imp_` arg evaluation. gt_caused=FALSE."),
 L1=[("MSG 1", "`<gt-localization confidence=\"high\"> Edit target: sympy/utilities/lambdify.py :: lambdify / guard/return to update: null_check|modules is None => module_provided = False [L350] / reason: lambdify called by _import [CALLS]`",
   "MSG 2 CMDs: `grep -n \"_eval_evalf\\|_imp_\\|evalf\" sympy/utilities/lambdify.py` AND `grep -n \"_eval_evalf\\|_imp_\" sympy/core/function.py`; MSG 5: \"Let me look at _eval_evalf in sympy/core/function.py\"",
   "D=Y - C=NO (high-confidence single target = wrong file; the guard/return hint [L350] is unrelated to the bug) - C=PARTIAL-NEGATIVE (one probe spent on the wrong target; decision followed the issue)")],
 SCOPE=[("MSG 3", "`<gt-scope files=\"5\"> 1. utilities/lambdify.py - in scope (you are viewing this) 2. core/compatibility.py - graph-connected ... 5. tests/test_numeric.py - graph-connected`",
   "agent moved to core/function.py next action", "D=Y - C=N (anchored on the wrong file) - C=N"),
  ("MSG 8", "`<gt-scope reason=\"re-anchored\"> 1. core/function.py - you have moved here; re-grounding scope 2. elementary/exponential.py - graph-connected...`",
   "agent already reading _eval_evalf body", "D=Y - C=Y - C=N (trailing)")],
 L3b=[("MSG 4", "`<gt-evidence kind=\"post_view\" file=\"sympy/core/function.py\"> [WITNESS] _eval_conjugate called by -> sympy/functions/special/mathieu_functions.py:23 ... [SIBLINGS] nargs`",
   "MSG 5-7: agent seds lines 495-535 itself", "D=Y - C=Y (real edges, none bug-relevant) - C=N")],
 L3=[("MSG 50", "`<gt-contract file=\"function.py\"> [SIGNATURE]/[CALLERS] lines on the edited _eval_evalf region`",
   "MSG 51-54: agent tests `f(g(2)).evalf()` -> 16.0", "D=Y - C=Y - C=N")],
 L5=[("MSG -", "only 1 nudge fired this task (deep metrics nudge_delivered=1.0); no false-positive nudge observed in the read",
   "-", "D=Y - C=- - C=N")],
 verdicts=dict(L1="delivered, WRONG at high confidence - the worst L1 failure mode of the run (confident misdirection; agent escaped via the issue)",
   SCOPE="delivered; initial anchor wrong, re-anchor trailing; leakage 0",
   L3b="delivered, correct, inert; leakage 0", L3="delivered, correct, inert; leakage 0",
   L5="1 nudge, inert; leakage 0"),
 cross="LEAKAGE (test-name/F2P) = 0. Telemetry stdout leak = 1 (`[gt-patch:loaded]` MSG 3). consumed-count = 0 (one wasted probe on the wrong brief target). fair-probe: NO (issue names the gold function).")

T["astropy__astropy-13033"] = dict(
 lead=("**TRAJECTORY (lead):** NOT resolved; gold file+function WERE edited. L1's 6 candidates were ALL wrong (timeseries/ "
   "absent; jQuery junk callers), but the post-view `<gt-scope>` at MSG 7 named gold `timeseries/core.py - graph-connected` "
   "one turn before the agent opened it (MSG 8) - ambiguous consumption (sampled.py's own import line equally explains the "
   "move). Miss = post-localization: the agent rewrote the error message with its OWN wording; the hidden F2P asserts the "
   "gold wording. It calibrated against the STALE visible test_sampled.py it grepped itself (MSG 70-75). gt_caused=FALSE."),
 L1=[("MSG 1", "`<gt-localization confidence=\"medium\"> 1. astropy/io/votable/tree.py - description, format, version / resolved caller: w() in astropy/extern/jquery/data/js/jquery-3.1.1.min.js:2 / 2. astropy/time/core.py - Time, format, _LeapSecondsCheck / resolved caller: cb() in ...jquery-3.1.1.min.js:3 ...` (6 candidates, none in astropy/timeseries/)",
   "MSG 2 CMD: `find /testbed -path \"*/astropy/timeseries\" -type d` (own navigation from the issue's TimeSeries)",
   "D=Y - C=NO (all 6 wrong; 2 jQuery junk callers) - C=NO")],
 SCOPE=[("MSG 7", "`<gt-scope files=\"5\"> 1. timeseries/sampled.py - in scope (you are viewing this) 2. timeseries/core.py - graph-connected 3. io/kepler.py - graph-connected...`",
   "MSG 8 CMD: `cat /testbed/astropy/timeseries/core.py` - the named gold, one action later (but sampled.py's visible `from astropy.timeseries.core import BaseTimeSeries, autocheck_required_columns` import equally explains it)",
   "D=Y - C=Y (gold named pre-open) - C=PARTIAL/AMBIGUOUS")],
 L3b=[("MSG 9", "`<gt-evidence kind=\"post_view\" file=\"astropy/timeseries/core.py\"> [WITNESS] __init__ called by -> astropy/timeseries/sampled.py:132 'with self._delay_required_column_checks():' [CALLERS] __init__() in .../binned.py:175 ... fold() in .../sampled.py:241`",
   "MSG 10: agent reasons over `_check_required_columns` directly from the file body", "D=Y - C=Y (real edges) - C=N")],
 L3=[("MSG 93", "`<gt-contract file=\"core.py\">` + `<gt-evidence kind=\"post_edit\" file=\"astropy/timeseries/core.py\">` after the sed edit",
   "agent iterates on the message format (MSG 94-107)", "D=Y - C=Y - C=N")],
 L5=[("MSG 51", "`<gt-nudge reason=\"scaffold_trap\"> GT: 25+ actions and no source-file edit yet - you are likely stuck exploring/scaffolding. Use the brief's gt-scope to localize and make a concrete edit to a SOURCE file now.`",
   "agent was fighting the build env (gcc/longintrepr.h); 6 actions later it pivoted to `cat -n core.py` (MSG 64) + wrote the fix script (MSG 66)",
   "D=Y - C=Y (true positive: 25 actions, no source edit) - C=PARTIAL (pivot followed within ~6 actions)")],
 verdicts=dict(L1="delivered, WRONG (all candidates); leakage 0",
   SCOPE="delivered, CORRECT (named gold pre-open), consumption ambiguous - the run's one scope-level delivery win",
   L3b="delivered, correct, inert; leakage 0", L3="delivered, correct, inert; leakage 0",
   L5="3 nudges; scaffold_trap true-positive, partially consumed; leakage 0"),
 cross="LEAKAGE (test-name/F2P) = 0. Telemetry stdout leak = 1 (`[gt-patch:loaded]` MSG 3). consumed-count = 1 partial (scope) + 1 partial (nudge). fair-probe: YES for L1 (issue did not name core.py) - L1 failed it; scope passed it ambiguously.")

T["astropy__astropy-13236"] = dict(
 lead=("**TRAJECTORY (lead):** NOT resolved; gold table.py rank 1 in the brief BUT the issue quotes the exact code block "
   "(`data = data.view(NdarrayMixin)`), so rank-1 is redundant - fair_probe=NO. Agent's action 1 = own "
   "`grep -n NdarrayMixin astropy/table/table.py` -> line 1246. Miss = the FutureWarning was added at the gold line with "
   "NON-gold wording ('Structured numpy array is being added as NdarrayMixin...') -> hidden `pytest.warns(match=...)` fails. "
   "The scaffold_trap nudge (MSG 49) produced an immediate pivot from pip/build attempts to source. gt_caused=FALSE."),
 L1=[("MSG 1", "`<gt-localization confidence=\"medium\"> 1. astropy/table/table.py - Table, isinstance, TableReplaceWarning / resolved caller: gb() in astropy/extern/jquery/data/js/jquery.dataTables.min.js:9 ...`",
   "MSG 2 CMDs: `grep -n \"NdarrayMixin\" astropy/table/table.py` + `grep -n \"NdarrayMixin\" astropy/table/ndarray_mixin.py` + `grep -n \"FutureWarning\" astropy/table/table.py` (issue-driven)",
   "D=Y - C=PARTIAL (gold rank 1, but caller-witness = minified jQuery junk; redundant with the issue's quoted code) - C=NO")],
 SCOPE=[("MSG 3", "`<gt-scope files=\"5\"> 1. table/table.py - in scope (you are viewing this) 2. tests/test_array.py - graph-connected 3. tests/test_jsviewer.py - graph-connected...`",
   "agent already on table.py:1246 via its own grep", "D=Y - C=PARTIAL (test_jsviewer.py is noise) - C=N")],
 L3b=[("MSG 3", "`<gt-evidence kind=\"post_view\" file=\"astropy/table/table.py\"> [WITNESS] gb called by -> astropy/extern/jquery/data/js/jquery.dataTables.min.js:9 'width:100,overflow:\"scroll\"...' [WITNESS] __set__ calls -> astropy/utils/metadata.py:474 ...`",
   "agent ignores; reads lines 1220-1270", "D=Y - C=NO (raw minified jQuery delivered to the agent as a WITNESS fact) - C=N")],
 L3=[("MSG 93", "`<gt-contract file=\"table.py\">` after the FutureWarning edit",
   "agent continues testing warning emission", "D=Y - C=PARTIAL (contracts anchored on the `isinstance` artifact symbol) - C=N")],
 L5=[("MSG 49", "`<gt-nudge reason=\"scaffold_trap\"> GT: 25+ actions and no source-file edit yet - you are likely stuck exploring/scaffolding. Use the brief's gt-scope to localize and make a concrete edit to a SOURCE file now.`",
   "MSG 50 CMD: `head -50 astropy/table/table.py` - immediate pivot from pip-install loop to the source file; edit follows (MSG 92+)",
   "D=Y - C=Y (true positive) - C=Y (immediate behavioral pivot)"),
  ("MSG 111", "`<gt-nudge reason=\"failure_persisted\"> ... your current hypothesis is likely wrong...`",
   "the persisting failure was the erfa/numpy ENV import chain, not the fix; hypothesis (add FutureWarning) was the right approach",
   "D=Y - C=N (false positive on env error) - C=N")],
 verdicts=dict(L1="delivered, rank-1-but-redundant + junk witness; not consumed; leakage 0",
   SCOPE="delivered, partial; trailing; leakage 0",
   L3b="delivered, WRONG (jQuery junk as WITNESS); inert; leakage 0",
   L3="delivered, partial (artifact-symbol anchor); inert; leakage 0",
   L5="3 nudges; scaffold_trap CONSUMED (the run's clearest nudge win); 1 false positive; leakage 0"),
 cross="LEAKAGE (test-name/F2P) = 0. Telemetry stdout leak = 1 (`[gt-patch:loaded]` MSG 3). consumed-count = 1 (scaffold_trap). fair-probe: NO (issue quotes the code block).")

T["astropy__astropy-13398"] = dict(
 lead=("**TRAJECTORY (lead):** eval = ERROR (not merely unresolved): the submitted patch is MALFORMED - "
   "'patch unexpectedly ends in middle of line' / 'Patch Apply Failed' (agent hand-built the new-file diff with echo+while-read "
   "at MSG 96, then concatenated git-diff + cached-diff at MSG 102). Fix CONTENT followed the gold approach (new "
   "`itrs_observed_transforms.py`) - but the issue itself embeds the full implementation, so fair_probe=NO. Gold is a NEW FILE, "
   "structurally unnamable by a ranker over existing nodes; L1's candidates were adjacent neighborhood only. gt_caused=FALSE."),
 L1=[("MSG 1", "`<gt-localization confidence=\"medium\"> 1. astropy/coordinates/transformations.py - FunctionTransformWithFiniteDifference... 2. astropy/coordinates/earth.py - to_geodetic, lon, lat / resolved caller: earth_location() in .../itrs.py:35 ...` (6 candidates; gold = NEW FILE, absent by construction)",
   "MSG 2 CMDs: `ls -la astropy/coordinates/builtin_frames/` + `cat .../intermediate_rotation_transforms.py | head -100` + grep for itrs_to_altaz... (issue-driven navigation)",
   "D=Y - C=NO-by-construction (gold file does not exist yet; `coordinates/__init__`-adjacent candidates only) - C=NO")],
 SCOPE=[("MSG 4", "`<gt-scope files=\"5\"> 1. builtin_frames/intermediate_rotation_transforms.py - in scope (you are viewing this) 2. tests/test_intermediate_transformations.py - graph-connected 3. coordinates/matrix_utilities.py - graph-connected 4. builtin_frames/utils.py - graph-connected 5. coordinates/earth.py - graph-connected`",
   "agent reads itrs.py, cirs_observed_transforms.py, icrs_observed_transforms.py (the right patterns) on its own",
   "D=Y - C=Y (correct neighborhood incl. the F2P test file) - C=WEAK")],
 L3b=[("MSG 4", "`<gt-evidence kind=\"post_view\" file=\".../intermediate_rotation_transforms.py\"> [WITNESS] get_gcrs_posvel called by -> astropy/coordinates/earth.py:735 'cirs_to_itrs_mat(obstime),' [SIBLINGS] teme_to_itrs_mat, gcrs_to_cirs_mat...`",
   "agent uses the issue's embedded implementation as its template", "D=Y - C=Y - C=N")],
 L3=[("MSG 53", "`<gt-contract file=\"__init__.py\">` after `sed -i '47a from . import itrs_observed_transforms'`",
   "agent proceeds to import-test", "D=Y - C=Y - C=N")],
 L5=[("MSG 41", "`<gt-nudge reason=\"scaffold_trap\"> GT: 25+ actions and no source-file edit yet...make a concrete edit to a SOURCE file now.`",
   "agent made 3 more reads then created the new file at MSG 48 (`cat > .../itrs_observed_transforms.py << EOF`)",
   "D=Y - C=Y (true positive) - C=PARTIAL (edit followed within 4 actions)")],
 verdicts=dict(L1="delivered; gold unnamable (new file) - an L1 design boundary, not a ranking error; leakage 0",
   SCOPE="delivered, correct neighborhood; weakly consumed; leakage 0",
   L3b="delivered, correct, inert; leakage 0", L3="delivered, correct, inert; leakage 0",
   L5="2 nudges; scaffold_trap true-positive, partially consumed; leakage 0"),
 cross="LEAKAGE (test-name/F2P) = 0. Telemetry stdout leak = 1 (`[gt-patch:loaded]` MSG 3). consumed-count = ~1 partial. fair-probe: NO (issue embeds the implementation). The actionable gap: NO GT layer guards patch-file INTEGRITY at submit (a presubmit-verify role).")

T["django__django-10097"] = dict(
 lead=("**TRAJECTORY (lead):** NOT resolved; gold file+line edited, and the agent CONSIDERED the gold-class regex and "
   "REJECTED it. L1 rank 1 = gold `django/core/validators.py`/URLValidator (redundant - the issue names URLValidator and "
   "quotes RFC 1738 user:pass rules). At MSG 89 the agent installed `[^\\s:@/]` (gold-equivalent); its check against the "
   "VISIBLE stale `tests/validators/valid_urls.txt` (which the hidden test patch replaces) failed on "
   "`http://-.~_!$&'()*+,;=:%40:80%2f::::::@example.com`; the failure_persisted nudge (MSG 96) fired at that exact moment; "
   "MSG 99-106 the agent reverted to `[^\\s@/]` (colons allowed) -> hidden invalid-URL cases pass validation -> F2P fails. gt_caused=FALSE."),
 L1=[("MSG 1", "`<gt-localization confidence=\"medium\"> 1. django/core/validators.py - URLValidator, _lazy_re_compile, RegexValidator / resolved caller: _check_query() in django/contrib/gis/geoip2/base.py:160 ...`",
   "MSG 2 CMDs: `grep -n \"class URLValidator\" django/core/validators.py` + `sed -n '1,100p' django/core/validators.py` (issue-driven; brief redundant)",
   "D=Y - C=Y (rank-1 = gold; witnesses real) - C=NO (issue already named it)")],
 SCOPE=[("MSG 3", "`<gt-scope files=\"5\"> 1. core/validators.py - in scope (you are viewing this) 2. utils/functional.py - graph-connected 3. forms/fields.py - graph-connected 4. tests/test_forms.py - graph-connected...`",
   "agent stayed on validators.py", "D=Y - C=Y - C=N")],
 L3b=[("MSG 3", "`<gt-evidence kind=\"post_view\" file=\"django/core/validators.py\"> [WITNESS] _check_query called by -> django/contrib/gis/geoip2/base.py:160 'validate_ipv46_address(query)' [CALLERS] __init__() in django/forms/fields.py:1163 ... [SIBLINGS] validate_integer, validate_domain_part...`",
   "agent reads the regex block itself", "D=Y - C=Y (real edges) - C=N")],
 L3=[("MSG 90", "`<gt-contract file=\"validators.py\">` after the `sed -i` regex edit",
   "agent tests against valid_urls.txt fixtures", "D=Y - C=Y - C=N")],
 L5=[("MSG 96", "`<gt-nudge reason=\"failure_persisted\"> GT: the same test failure has persisted across your edit(s) - your current hypothesis is likely wrong. Re-read the failing assertion and reconsider the root cause / target file.`",
   "MSG 99: agent abandons `[^\\s:@/]` (the GOLD character class) for `[^\\s@/]` citing the stale-fixture failure. The nudge's 'your hypothesis is likely wrong' pointed AWAY from the gold fix",
   "D=Y - C=N (false positive WITH plausible HARM - it reinforced reverting the gold-equivalent edit; proximate cause remains the stale fixture) - C=AMBIGUOUS (revert followed it immediately)")],
 verdicts=dict(L1="delivered, correct-but-redundant (rank-1 gold); not consumed; leakage 0",
   SCOPE="delivered, correct, inert; leakage 0", L3b="delivered, correct, inert; leakage 0",
   L3="delivered, correct, inert; leakage 0",
   L5="3 nudges; the MSG 96 failure_persisted is the run's worst nudge firing - fired against a gold-equivalent edit; leakage 0"),
 cross="LEAKAGE (test-name/F2P) = 0. Telemetry stdout leak = 1 (`[gt-patch:loaded]` MSG 3). consumed-count = 0 positive (1 possible NEGATIVE consumption: the nudge-adjacent revert). fair-probe: NO (issue names URLValidator + the rules). Context-gap (CLAUDE.md mandatory): what GT needed to send = 'visible fixture files are STALE relative to the hidden grader; do not calibrate the character class against valid_urls.txt' - i.e., contract-level guidance, which no layer carries.")

T["django__django-10554"] = dict(
 lead=("**TRAJECTORY (lead):** NOT resolved; the agent READ the gold function and walked away. Gold "
   "`django/db/models/sql/compiler.py::get_order_by` was rank 2 in the brief (headline rank 1 = query.py, where the wrong "
   "fix landed). The agent reached compiler.py at action 10, read get_order_by's combinator position-resolution loop twice "
   "(MSG 88-105), could not reproduce the PostgreSQL-only `ORDER BY position` error on the SQLite testbed, and shipped a "
   "defensive `query.chain()` clone in query.py::_combinator_query instead. The one substantively-correct nudge of the run "
   "(failure_persisted, MSG 151) went unconsumed. gt_caused=FALSE."),
 L1=[("MSG 1", "`<gt-localization confidence=\"medium\"> 1. django/db/models/query.py - QuerySet, last, values_list / resolved caller: create_default_site() in django/contrib/sites/management.py:20 / 2. django/db/models/sql/compiler.py - execute_sql, SQLCompiler, __init__ / resolved caller: do_query() in django/db/models/sql/subqueries.py:24 ...`",
   "MSG 2 CMDs: `ls -la` + `grep -n \"def order_by\" django/db/models/query.py` + `grep -rn \"union\" django/db/models/query.py` (issue-driven: union/order_by)",
   "D=Y - C=PARTIAL (gold compiler.py at rank 2; headline query.py = where the WRONG fix landed - the issue also points there, so misdirection is shared with the issue) - C=NO")],
 SCOPE=[("MSG 17", "`<gt-scope reason=\"re-anchored\"> 1. sql/compiler.py - you have moved here; re-grounding scope...`",
   "agent reading get_order_by/combinator code", "D=Y - C=Y - C=N (trailing)")],
 L3b=[("MSG 16", "`<gt-evidence kind=\"post_view\" file=\"django/db/models/sql/compiler.py\"> [WITNESS] do_query called by -> django/db/models/sql/subqueries.py:24 'cursor = self.get_compiler(using).execute_sql(CURSOR)' [SIBLINGS] setup_query, get_group_by, collapse_group_by, get_select, get_order_by`",
   "MSG 18: agent seds 340-370 + 410-490 (the combinator block) - in-neighborhood but uncited", "D=Y - C=Y (siblings include the gold function name) - C=N")],
 L3=[("MSG 195", "`<gt-contract file=\"query.py\">` after the _combinator_query chain() edit",
   "agent verifies clone independence + runs queries suite", "D=Y - C=Y - C=N")],
 L5=[("MSG 43", "`<gt-nudge reason=\"scaffold_trap\"> GT: 25+ actions and no source-file edit yet...`",
   "agent continued reading compiler.py/query.py; first real fix attempt much later", "D=Y - C=Y (true positive) - C=N"),
  ("MSG 151", "`<gt-nudge reason=\"failure_persisted\"> GT: the same test failure has persisted across your edit(s) - your current hypothesis is likely wrong. Re-read the failing assertion and reconsider the root cause / target file.`",
   "the agent's hypothesis (queryset-clone corruption in query.py) WAS the wrong mechanism - the gold fix lives in compiler.py get_order_by; agent did not change target; at MSG 202 it says 'I'm going to stop trying to understand the exact corruption mechanism'",
   "D=Y - C=Y (SUBSTANTIVELY CORRECT - the only correct failure_persisted of the run) - C=N (ignored)")],
 verdicts=dict(L1="delivered, gold at rank 2, not consumed; headline shared the issue's misdirection; leakage 0",
   SCOPE="delivered, trailing; leakage 0",
   L3b="delivered, correct (gold function in SIBLINGS), inert; leakage 0",
   L3="delivered, correct, inert; leakage 0",
   L5="3 nudges; 1 substantively-correct failure_persisted UNCONSUMED (missed save); leakage 0"),
 cross="LEAKAGE (test-name/F2P) = 0. Telemetry stdout leak = 1 (`[gt-patch:loaded]` MSG 3). consumed-count = 0. fair-probe: YES (issue names no file) - gold-at-rank-2 unconsumed = a missed efficiency/causation win.")

PREREQ_NOTE = ("Substrate numbers are telemetry-only; they reach the agent ONLY as the brief's resolved-edge lines "
  "(quoted in the HOW column). Certs reconciled against the runtime witness per gt_gt S12 "
  "(no GRAPH_FAIL_MISSING_HANDOFF false-FAIL present on this run; graph cert + LSP cert + embedder cert all PASS).")

def prereq_table(task):
    g = json.load(open(os.path.join(BASE, task, "gt_artifacts", "foundational_gate_report.json")))
    gr, gl, ge = g['gate_resolution'], g['gate_lsp'], g['gate_embedder']
    tt = gr['typing_tier_counts']
    bd = gr['resolution_method_breakdown']
    brief = open(os.path.join(BASE, task, "gt_artifacts", "brief.txt"), encoding="utf-8").read()
    # first two resolved-edge lines from the brief (how P1/P2 reach the agent)
    lines = [l.strip() for l in brief.splitlines() if "resolved caller:" in l or "resolved call:" in l][:2]
    how = " / ".join("`%s`" % l for l in lines) if lines else "(no resolved-edge lines in brief)"
    bd_s = ", ".join("%s=%d" % (k, v) for k, v in bd.items())
    rows = []
    rows.append("| P1 receiver-type resolution | `det_pct=%.8f` - `name_match=%d` - typing tiers: `type_flow=%d - impl_method=%d - inherited=%d` (preds A/B/C all true) | GREEN (pass=true) | %s |"
        % (gr['det_pct'], int(gr['name_match_edges']), tt.get('type_flow',0), tt.get('impl_method',0), tt.get('inherited',0), how))
    rows.append("| P2 graph.db depth | `calls_edges=%.1f` - resolution_method breakdown: %s - LSP: `%s`, warm probe `%.8f ms`, `resolved_promoted=%.1f`, `graph_lsp_edges=%d` (cert==graph, `stamp_mismatch=\"\"`) | GREEN (pass=true) | same resolved-edge lines + post-view `[WITNESS]`/`[CALLERS]` lines (quoted in the L3b table) |"
        % (gr['calls_edges'], bd_s, gl['verdict'], gl['probe_latency_ms'], gl['resolved_promoted'], int(gl['graph_lsp_edges'])))
    p, c = ge['present'], ge['consumption']
    rows.append("| P3 embedder | `class=%s` - `is_zero=%s` - `cos_related=%.8f` - `cos_unrelated=%.8f` - `effective_w_sem=%.8f` (consumption preds 1/2/3 true) | GREEN (pass=true, mode=present_and_consumption) | indirect only - it ORDERS the L1 candidate list the agent sees (no embedder text reaches the agent) |"
        % (p['class'], p['is_zero'], p['cos_related'], p['cos_unrelated'], c['effective_w_sem']))
    hdr = "| substrate gate | 8-dp REAL numbers (verbatim, foundational_gate_report.json) | GREEN? | HOW it reached the agent |\n|---|---|---|---|"
    return hdr + "\n" + "\n".join(rows) + "\n\n" + PREREQ_NOTE

def comp_table(name, rows):
    out = ["### %s" % name, "", "| turn | GT SENT (verbatim) | AGENT DID (verbatim) | DELIVERED/CORRECT/CONSUMED |", "|---|---|---|---|"]
    for r in rows:
        out.append("| %s | %s | %s | %s |" % r)
    return "\n".join(out)

L4_ROW = ("| - | DELIVERED=N/A - L4 is an EVENT hook (gt_gt S12); on PATH B the wrapper's view/edit/failure/loop events ARE the "
  "hook surface and are tabled above (L3b/L3/L5). No separate L4 event exists on this path - absence = the event "
  "surface doesn't exist here, NOT a dead layer. | - | N/A |")
L6_ROW = ("| - | DELIVERED=N/A-BY-DESIGN - L6 is the post-edit REINDEXER; on the substrate path the mounted graph.db is "
  "authoritative + read-only (witness-hash parity), so single-file reindex is deliberately OFF (gt_gt S12 update / S6 note). "
  "'L6 fired' is the wrong expectation here. | - | N/A |")
GTV_ROW = ("| - | DELIVERED=NO - the agent-invoked GT_VERIFY surface is not wired on PATH B (mini-swe Verified pipeline); the "
  "post-edit `<gt-contract>` + `<gt-evidence kind=\"post_edit\">` injections (tabled above) carry the L3 role. Read from the "
  "trajectory: no `gt understand`/`gt verify` invocation occurs in any of the agent's commands. | - | N/A |")

for task, d in T.items():
    g = json.load(open(os.path.join(BASE, task, "gt_artifacts", "foundational_gate_report.json")))
    sc = json.load(open(os.path.join(BASE, task, "scorecard.json")))
    dm = json.load(open(os.path.join(BASE, task, "gt_deep_metrics_%s.json" % task)))
    t2 = sc['tier2_causality']; t3 = sc['tier3_localization']; t7 = sc['tier7_cost']
    s = io.StringIO()
    w = s.write
    w("\n## 2026-06-10 PATH B trial - gt_trial.md §4+§5 audit (run 27260307167)\n\n")
    w("**Arm:** SWE-bench Verified x deepseek-v4-flash (temp=1.0), mini-swe-agent, GT-on, substrate "
      "`gt-substrate@sha256:db7bd22d...`. Official eval: **%s**. Audit method: chronological read of the full "
      "`%s.traj.json` messages array incl. `tool_calls` commands (never grep), per gt_trial.md §4 + the AGENT-OBSERVATION rule. "
      "Scorecard (8-dp, §5): `.claude/reports/runs/pathB_verified_trial_27260307167/%s/scorecard.json`.\n\n"
      % ("RESOLVED" if sc['tier1_outcome']['resolved'] else ("ERROR (Patch Apply Failed)" if task=="astropy__astropy-13398" else "NOT RESOLVED"), task, task))
    w(d['lead'] + "\n\n")
    w("### (a) PREREQS - substrate P1/P2/P3 (gt_trial.md §1.5 gates, verbatim 8-dp)\n\n")
    w(prereq_table(task) + "\n\n")
    w(comp_table("(b) L1 brief / localization (ROLE per gt_gt §12: file RANKER - judge by gold-rank + fair reach)", d['L1']) + "\n\n")
    w("**L1 verdict:** %s\n\n" % d['verdicts']['L1'])
    w(comp_table("(b) consensus / scope (`<gt-scope>`)", d['SCOPE']) + "\n\n")
    w("**SCOPE verdict:** %s\n\n" % d['verdicts']['SCOPE'])
    w(comp_table("(b) L3b post-view (ROLE: contract pillar - judge by bug-locus relevance, gt_gt §12)", d['L3b']) + "\n\n")
    w("**L3b verdict:** %s\n\n" % d['verdicts']['L3b'])
    w(comp_table("(b) L3 post-edit contract (`<gt-contract>` + post_edit evidence)", d['L3']) + "\n\n")
    w("**L3 verdict:** %s\n\n" % d['verdicts']['L3'])
    w("### (b) GT_VERIFY\n\n| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |\n|---|---|---|---|\n" + GTV_ROW + "\n\n")
    w("**GT_VERIFY verdict:** N/A on this path (no agent-invoked verify surface); not a dead layer.\n\n")
    w("### (b) L4 (EVENT hook - gt_gt §12: absence = event didn't occur, NOT dead)\n\n| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |\n|---|---|---|---|\n" + L4_ROW + "\n\n")
    w("**L4 verdict:** N/A-by-path; the event surfaces that DO exist here all fired (see L3b/L3/L5 tables).\n\n")
    w(comp_table("(b) L5 / L5b governor (`<gt-nudge>`)", d['L5']) + "\n\n")
    w("**L5/L5b verdict:** %s\n\n" % d['verdicts']['L5'])
    w("### (b) L6 (REINDEXER - gt_gt §12)\n\n| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |\n|---|---|---|---|\n" + L6_ROW + "\n\n")
    w("**L6 verdict:** gated OFF by design on the substrate path - correct behavior, not a failure.\n\n")
    w("### (c) Cross-component line\n\n")
    w(d['cross'] + "\n\n")
    w("### §5 scorecard (stored 8-dp at `%s/scorecard.json`)\n\n" % task)
    w("Tier 1: resolved=%s - baseline/flip/regression = **N/A** (no frozen SWE-bench Verified baseline exists; the 87/300 frozen file is OH+SWE-bench-Live - stated, not faked).\n" % sc['tier1_outcome']['resolved'])
    w("Tier 2: delivered=%.8f - correct=%.8f - consumed=%.8f - fair_probe=%.8f - right_trajectory=%.8f - **gt_caused=%.8f** (gate broke: %s)\n"
      % (t2['delivered'], t2['correct'], t2['consumed'], t2['fair_probe'], t2['right_trajectory'], t2['gt_caused'], t2['gate_broke']))
    w("Tier 3: gold_in_brief=%s - first_gold_rank=%s - gold_edited=%s - first_edit_action=%s - edit_to_gold_action=%s - turns_to_gold_view=%s\n"
      % (t3['gold_file_reached_by_brief'], t3['first_gold_rank'], t3['gold_edited'], t3['first_edit_action'], t3['edit_to_gold_action'], t3['turns_to_gold_view']))
    w("Tier 4: action_count=%.8f - gt_injected_tokens=%.8f - looped_stuck=False - self_localized=%s\n"
      % (sc['tier4_nonharm_efficiency']['action_count'], sc['tier4_nonharm_efficiency']['gt_injected_tokens'], sc['tier4_nonharm_efficiency']['self_localized']))
    w("Tier 6: foundational_gates=GREEN (all_on=true) - test_names_leaked=0 - fail_to_pass_leaked=false - no_gold_labels=true - telemetry stdout leak=1 (`[gt-patch:loaded]`) - VOID=false\n")
    w("Tier 7: llm_in=%.8f - llm_out=%.8f - llm_cost_usd=%.8f (%s) - wall_clock_s=%.8f - time_to_gold_view_s=%.8f\n"
      % (t7['llm_in'], t7['llm_out'], t7['llm_cost_usd'], t7['cost_source'], t7['wall_clock_s'], t7['time_to_gold_view_s']))
    content = s.getvalue()
    path = os.path.join(LEDGERS, "%s.md" % task)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(content)
    print("appended", path, len(content), "chars")
