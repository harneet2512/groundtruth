# RUNTIME FUCKUPS — GHA / eval-pipeline runtime issues

Live and latent runtime defects in the GHA + eval pipeline (distinct from
test-time issues in we_did.md). Each found by adversarial audit of the
GHA/LSP-integration map (workflow wf_fd8cffeb, 2026-05-28). The integration
plan **did not survive** the adversary (`survives=false`) — these are why.

> **Reframe that de-risks the smoke:** the curation-map smoke does NOT need the
> risky delivery-layer parts. Canary already does host-side LSP promotion (C6),
> and the v22 brief (L1, host-side) already reads the promoted db and carries
> `<gt-graph-map>` (verified E2E). Step-4 (in-container hooks) and C7 are a
> separate delivery-layer effort, **not on the critical path to proving the map.**

---

## RF-1 — LIVE C4 violation: `GT_LSP_VERIFY=1` arms an in-turn LSP cold-start  ✅ FIXED

**What:** `GT_LSP_VERIFY: "1"` was set in `canary_3arm.yml:236` and
`swebench_30task.yml:207`. The wrapper gates on `== "1"` at
`oh_gt_full_wrapper.py:3932 / 4787 / 5416`; at 5416 it does
`asyncio.run_until_complete(LazyEdgeVerifier(...).start())` — spawning a pyright
LSP server **during the agent-run setup** (~2-5s cold start, `edge_verifier.py:12`),
and per-edit verification in-turn at 4787. This is the exact thing C4 ("LSP must
NOT cold-start in the agent turn — offline/precompute only") was meant to kill.
Likely a silent no-op TODAY (edge_verifier reads host paths; container files
absent), but **armed** — step-4 (host-side source materialization) would flip it
live.

**Why it matters:** directly contradicts C4 and the offline-promotion design.
The whole point of C6 is to make in-turn LSP unnecessary.

**Fix (applied 2026-05-28):** `GT_LSP_VERIFY: "0"` in both workflows. Verified
edges come from offline C6 promotion, not an in-turn spawn. Deeper option (defer):
gate the wrapper's `verifier.start()` so it never cold-starts in-turn regardless
of the flag (C4-at-source).

---

## RF-2 — Do NOT land C6 on the primary scale gates in one shot  ⚠️ GUARDRAIL (build per spec below)

**What:** `swebench_eval.yml` (3 agent jobs) and `swebench_30task.yml` currently
work with `setup-eval` + wrapper and **no pre-index/resolve step**. Adding the
docker-create/cp + gt-index + `pip install pyright` + `resolve --resolve` block
in front of the working agent run is a NEW failure surface on the canonical eval
path.

**Why it matters:** zero-infra-failure-for-submission mandate. A pyright miss /
docker-cp failure on the scale gates = retries that look like failures.

**Build spec:** extract canary's proven pre-index block
(`canary_3arm.yml:165-213`) into a reusable composite action
`.github/actions/preindex-promote/action.yml` (keep `timeout 120` + `|| echo WARN`
non-fatal). **Prove on canary FIRST** (measure name_match→lsp yield from a real
artifact's before/after `resolution_method` distribution — not "resolve ran").
Do NOT wire onto the primary gates until the canary yield + step-4 are proven.

---

## RF-3 — Step-4 (host→container promoted db): L6 reindex OVERWRITES the promotion mid-task  ⚠️ LATENT (build per spec below)

**What:** the gap is that in-container hooks (L3/L3b/L5/L6) read the
**un-promoted** in-container `gt-index` build via `--db={config.graph_db}`
(`post_view:972`, `post_edit:999`), while only the host-side L1 brief reads the
promoted db (`_host_graph_db`, `oh_gt_full_wrapper.py:430`). Step-4 = upload the
host-promoted db into the container as `config.graph_db` and skip the in-container
build. **DANGER:** L6 reindex mutates `config.graph_db` in-container on first edit
(~`oh_gt_full_wrapper.py:4102+`) → **overwrites the uploaded promoted db,
re-introducing un-promoted edges mid-task.** Also: the `alt_root` retry
(~2952-2972) assumes the in-container build ran; skipping it without rewiring all
consumers risks zeroing the runtime layers.

**Why it matters:** silently undoes the promotion after the first edit — the
worst kind of "looks fine in logs, wrong in reality."

**Build spec (mandatory before claiming step-4 works):** (a) upload to the EXACT
`config.graph_db` container path; (b) `schema_version.verify_graph_db_schema`
(exists, used at :2127) before trusting it; (c) reconcile the `alt_root` retry;
(d) **resolve the L6-reindex interaction** — after reindex, either re-apply
promotion or preserve the promoted edges (do NOT let reindex blow them away);
(e) gate the whole thing on `GT_PREBUILT_GRAPH_DB` so the DEFAULT path
(no prebuilt db) is byte-unchanged.

---

## RF-4 — C7 (closure sidecar) depends on C6 for correctness — NOT "independent"  ⚠️ ORDERING (build per spec below)

**What:** the plan called C6/C7 independent. For CORRECTNESS they are coupled:
the transitive closure must run over **verified edges only**
(confidence≥0.5 / deterministic resolution_method), else it propagates
`name_match` false positives **transitively** (a bad 1-hop edge becomes bad
2-hop and 3-hop reach). So C7-over-promoted-edges depends on C6 having promoted
first.

**Why it matters:** an unfiltered closure amplifies graph noise instead of
delivering trustworthy deep reach.

**Build spec:** the Go closure pass filters to verified edges
(confidence≥0.5 / same_file/import/verified_unique/type_flow/lsp) before
computing reachability; depth-bounded (≤3). Compiles free in the existing
`setup-eval` CGO build (cache key hashes `gt-index/**/*.go`). **Land behind
`ci.yml` go-build smoke first** (a compile error fails every job sharing
`setup-eval`). NOTE: cannot be built/verified on the local dev box (no Go/GCC) —
CI-only verification.

---

## Build order (de-risked, per the corrections above)

1. ✅ RF-1 fixed (`GT_LSP_VERIFY=0`).
2. C6-composite: `preindex-promote` action + deterministic pyright in `setup-eval`;
   refactor canary to use it. **Do not touch the primary gates.**
3. Step-4 (RF-3): wrapper host→container, gated on `GT_PREBUILT_GRAPH_DB`,
   L6-overwrite resolved, default path unchanged.
4. C7 (RF-4): Go closure over verified edges + closure table + impact/trace
   rewrite with BFS fallback; CI-only verification, behind `ci.yml` smoke.
5. Prove on canary (yield + delivery from agent observation), THEN consider gates.
