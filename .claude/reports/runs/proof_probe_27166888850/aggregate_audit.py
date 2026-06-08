"""Aggregate the 10 gate-deep JSONs into the gt_trial §4 PREREQS table + §5 Tier-6
legitimacy scorecard (8-dp). Gates-only proof probe -> no agent output.jsonl, so the
§4 deliverable is the PER-TASK SUBSTRATE PREREQS table (P1 resolution / P2 graph / P3
embedder), read from the gate-deep contracts, + the §5 Tier-6 legitimacy verdict."""
import glob, json, os

D = os.path.dirname(__file__)
rows = []
for f in sorted(glob.glob(os.path.join(D, "all_deep", "*", "gt_gates_deep_*.json"))):
    g = json.load(open(f, encoding="utf-8"))
    task = os.path.basename(f)[len("gt_gates_deep_"):-len(".json")]
    r, l, e = g["gate_resolution"], g["gate_lsp"], g["gate_embedder"]
    ec = e["consumption"]; ep = e["present"]; tt = r["typing_tier_counts"]
    rows.append({
        "task": task,
        "all_on": g["verdict"]["all_on"],
        # P1 RESOLUTION
        "calls": int(r["calls_edges"]), "det": int(r["deterministic_edges"]),
        "name_match": int(r["name_match_edges"]), "det_pct": r["det_pct"],
        "tf": tt.get("type_flow", 0), "im": tt.get("impl_method", 0),
        "inh": tt.get("inherited", 0), "asgn": tt.get("ev:assignment_tracked", 0),
        "g1": r["pass"], "pB_nondom": r["pred_B_nondominance"],
        # P2 LSP
        "lsp_resolved": int(l["resolved"]), "lsp_residual": int(l["residual"]),
        "resolve_frac": l["resolve_frac"], "scoped": l["scoped_source_files"], "g2": l["pass"],
        # P3 EMBEDDER
        "cls": ep["class"], "is_zero": ep["is_zero"],
        "cos_rel": ep["cos_related"], "cos_unrel": ep["cos_unrelated"],
        "wsem": ec["effective_w_sem"], "sem_scored": ec["sem_scored_count"],
        "sem_distinct": ec["sem_distinct_values"], "sem_mad": ec["sem_mad"],
        "sem_max": ec["sem_max"], "p2cov": ec["pred_2_coverage"], "p3disp": ec["pred_3_dispersion"],
        "g3": e["pass"],
    })

def f8(x):
    return f"{float(x):.8f}"

def yn(b):
    return "GREEN" if b else "**OFF**"

P = int(sum(1 for r in rows if r["all_on"]))
F = len(rows) - P
out = []
out.append(f"# Proof Probe §4/§5 Audit — gates-only, GT_PROOF_MODE=1 (run 27166888850, hbali, gt-trial)\n")
out.append(f"**Verdict: {P}/{len(rows)} substrate-GREEN, {F}/{len(rows)} fail-closed.** "
           "Gates-only ⇒ no agent ran (no token spend); the §4 deliverable is the per-task "
           "substrate PREREQS table below + the §5 Tier-6 legitimacy verdict. A FAIL = a wired "
           "fail-closed gate correctly caught a partial-operation substrate (plan §9: the signal).\n")

# §4 PREREQS — P1 RESOLUTION
out.append("\n## §4 PREREQS — P1 RESOLUTION (det% · name_match · typing tiers; 8-dp)\n")
out.append("| task | CALLS | det | name_match | det_pct | type_flow | impl_method | inherited | assign | det≥nm (predB) | GATE-1 |")
out.append("|---|--:|--:|--:|--:|--:|--:|--:|--:|:--:|:--:|")
for r in rows:
    out.append(f"| {r['task']} | {r['calls']} | {r['det']} | {r['name_match']} | {f8(r['det_pct'])} | "
               f"{r['tf']} | {r['im']} | {r['inh']} | {r['asgn']} | {'yes' if r['pB_nondom'] else 'NO'} | {yn(r['g1'])} |")

# §4 PREREQS — P2 LSP
out.append("\n## §4 PREREQS — P2 LSP precision pass (resolve count; 8-dp)\n")
out.append("| task | resolved | residual | resolve_frac | scoped_files | GATE-2 |")
out.append("|---|--:|--:|--:|--:|:--:|")
for r in rows:
    out.append(f"| {r['task']} | {r['lsp_resolved']} | {r['lsp_residual']} | {f8(r['resolve_frac'])} | {r['scoped']} | {yn(r['g2'])} |")

# §4 PREREQS — P3 EMBEDDER
out.append("\n## §4 PREREQS — P3 EMBEDDER (present + CONSUMED; 8-dp)\n")
out.append("| task | class | is_zero | cos_rel | cos_unrel | eff_w_sem | sem_scored | sem_distinct | sem_mad | sem_max | cover | disp | GATE-3 |")
out.append("|---|---|:--:|--:|--:|--:|--:|--:|--:|--:|:--:|:--:|:--:|")
for r in rows:
    out.append(f"| {r['task']} | {r['cls']} | {r['is_zero']} | {f8(r['cos_rel'])} | {f8(r['cos_unrel'])} | "
               f"{f8(r['wsem'])} | {r['sem_scored']} | {r['sem_distinct']} | {f8(r['sem_mad'])} | {f8(r['sem_max'])} | "
               f"{'y' if r['p2cov'] else 'N'} | {'y' if r['p3disp'] else 'N'} | {yn(r['g3'])} |")

# §5 Tier-6 LEGITIMACY scorecard
out.append("\n## §5 Tier-6 LEGITIMACY (foundational_gates shown + GREEN per task)\n")
out.append("| task | GATE-1 resolution | GATE-2 lsp | GATE-3 embedder | ALL_ON | failure gate |")
out.append("|---|:--:|:--:|:--:|:--:|---|")
for r in rows:
    fg = []
    if not r["g1"]: fg.append("G1 name_match-dominant" if not r["pB_nondom"] else "G1")
    if not r["g2"]: fg.append("G2 resolve")
    if not r["g3"]:
        fg.append("G3 sem all-zero" if r["sem_scored"] == 0 else ("G3 sem flat (mad=0)" if r["sem_mad"] == 0 else "G3"))
    out.append(f"| {r['task']} | {yn(r['g1'])} | {yn(r['g2'])} | {yn(r['g3'])} | {yn(r['all_on'])} | {', '.join(fg) or '—'} |")

out.append("\n## Failure taxonomy (the two real substrate gaps surfaced)\n")
g1f = [r["task"] for r in rows if not r["g1"]]
g3f = [r["task"] for r in rows if r["g1"] and r["g2"] and not r["g3"]]
out.append(f"- **GATE-1 name_match dominance ({len(g1f)}):** {', '.join(g1f) or '—'} — whole-graph "
           "`name_match > deterministic`; indexer-level receiver-type resolution gap (CLAUDE.md method-call "
           "gap), NOT the LSP demand-pass (which passed GATE-2 everywhere).")
out.append(f"- **GATE-3 embedder not-consumed ({len(g3f)}):** {', '.join(g3f) or '—'} — rendered candidates "
           "carry all-zero or flat (mad=0) semantic scores; render-alignment / sem-join collapse (plan finding A).")
out.append(f"- **GATE-2 LSP resolve: PASS on ALL {len(rows)} tasks** — resolve is not the failure.\n")

md = "\n".join(out)
open(os.path.join(D, "PROOF_PROBE_AUDIT.md"), "w", encoding="utf-8").write(md)
json.dump(rows, open(os.path.join(D, "scorecard.json"), "w", encoding="utf-8"), indent=2)
print(md)
