# BRIEFING.md — How GroundTruth brief-localization is delivered (canonical reference)

> **READ THIS BEFORE ANY localization or briefing change.** A hook surfaces it on
> edits to the brief/localizer files. The 2-day failure (2026-06-02→03) happened
> because measurements ran on a HALF-ENABLED pipeline (semantic off, wrong layer,
> wrong branch) and the degraded output was reported as GT's performance. This doc
> is the full scope so that never repeats.

Legend: **[V]** = verified by direct read this session · **[A]** = from research
agents, spot-check the exact line before acting · **[D]** = from a GT doc.

---

## 0. THE PIPELINE — two scorers feeding a gated renderer  [V]

`generate_v1r_brief` (`src/groundtruth/pretask/v1r_brief.py`) = the LIVE brief the
agent receives. It is NOT `localize()` alone. Measure `generate_v1r_brief`.

```
issue → ① run_v74 (candidate gen + scoring)
        ② localize  (grep-spine + 3-ranker RRF + agreement → confidence tiers)
        ③ render_brief (hub demotion, ~5-cap + min-guarantee, confidence-gated
                        show/suppress, abstain→grep-fallback, token budget)
      → curated brief → agent
```

### ① run_v74  (`v7_4_brief.py`)  [V]
Stage A: `semantic_top_K ∪ graph_expand(trusted_anchors)`. Scored by
`W_SEM·sem + W_LEX·bm25 + W_REACH·reach + W_PROX + W_HUB + W_COMMIT + W_PATH`.
Current DEFAULT_WEIGHTS [V]: `W_SEM=0.15, W_LEX=0.50, W_REACH=0.05, W_PROX=0.05,
W_HUB=0.10, W_COMMIT=0.0, W_PATH=0.45`.

### ② localize  (`graph_localizer.py`)  [V]
grep-spine + 3 independent rankers (grep / structural / semantic) fused by
**3-way RRF** (Cormack SIGIR 2009); confidence = **AGREEMENT** = how many of the 3
rankers put a file in their own top-3 → `<gt-localization confidence=X>` tiers.
- **grep ranker** = recall SPINE (lexical token coverage); primary sort key.
- **structural ranker** = `(_witness_tier, -score, …)`, **promote-only**: lifts a
  file above grep order ONLY on a verified, non-DEFINES, issue-anchored ≤1-hop edge.
  Never promotes on reach/popularity (that is the hub trap).
- **semantic ranker** = `_semantic_score_by_file` (issue↔file dense cosine) — the
  issue→code bridge for golds sharing NO tokens with the issue.
Composite `score` (feeds the structural ranker only): `W_BM25·bm25 + W_PATH_DECAY·
decay + W_WITNESS·witness + W_LEX·lex + W_SUBJECT·subj + W_DEGREE·deg − 0.5·gen − 0.4·test`.
Current W_* [V]: `W_WITNESS=0.60, W_BM25=0.35, W_PATH_DECAY=0.30, W_LEX=0.30,
W_SUBJECT=0.15, W_DEGREE=0.10`. (`W_CLOSURE=0.30` exists only as an UNCOMMITTED
local edit — NOT in consensus; closure is the WRONG lever, see §4.)

### ③ render_brief  [V]
Hub demotion (`_is_generated −0.5`, `_is_test −0.4`, grep-spine promote-only);
~5-candidate cap with min-candidate guarantee (never collapse to 1);
confidence-gated show/suppress (drop `[INFO]`/conf<0.9; abstain → grep-fallback note
when nothing anchors); token budget trims evidence DETAIL, not the file LIST.

---

## 1. THE GRAPH-DEPTH FORMULA (hops, decay, LSP)  [A — spot-check lines]
- **Path-decay** (`graph_localizer._path_decay_scores`): Dijkstra BFS from seeds,
  `edge_cost = 1/confidence`, `cost = Σ edge_cost`, `S(f) = beta^cost` (beta=0.85),
  `max_hop=3` but DYNAMICALLY reduced by `_dynamic_max_hop` (dense+verified→2,
  sparse→3). Cites KGCompass 2025 / RepoGraph ICLR 2025. [A/D]
- **Reach** (`graph_reach.compute_reach`): per-edge-type weights (CALLS 1.0, USES 0.8,
  IMPORTS 0.6, CONTAINS 0.4…) × confidence × `1/(1+path_len)`, hub-penalty
  discounted (Lao & Cohen 2010 PRA). [A/D]
- **LSP enrichment** = the `confidence`/`resolution_method` columns. LSP-verified
  edges (`same_file`/`import`/`type_flow`/`verified_unique`, conf≥0.9) get cheap
  path cost → higher reach; name_match (0.2–0.6) is expensive/suppressed. **LSP is
  baked into the graph at resolve time; closures must be REBUILT after resolve
  (`gt-index -rebuild-closure`) or they're stale.** [V — Part A fix, commit 89615b60]

---

## 2. THE SEMANTIC GAP — the thing that makes it work is HALF-BROKEN  [V]

Two separate semantic implementations that DISAGREE, both OFF in the agent's
container (no torch):

| | how it gets semantic | in the container |
|---|---|---|
| ① run_v74 | `sentence_transformers` ONLY, else `_ZeroEmbeddingModel` (W_SEM→0) | **OFF** — no ONNX path exists |
| ② localize | `_get_embedder`: sentence_transformers, else ONNX e5-small-v2 | **OFF** — `embed.py` is gitignored, loader not shipped |

So in the container, semantic is OFF in BOTH halves → the issue→code bridge is dark →
behavior-matched/token-mismatched golds (axum, marimo, boa) stay buried under hubs.
**Any measurement without BOTH halves on, container-real, is WORTHLESS.**

### THE FIX (from semantic agent, [A] — spot-check):
1. **run_v74:** add the SAME ONNX fallback localize has — insert `_OnnxEmbedderAdapter`
   + try ONNX (`groundtruth.memory.enrich.embed.get_embedding_model`) BEFORE
   `_ZeroEmbeddingModel` in `_get_model()` (`v7_4_brief.py` ~256–300). Behavior
   unchanged when sentence_transformers present. Interface: both expose
   `.encode(texts, normalize_embeddings=…)`.
2. **embed.py shipped:** `.gitignore` line `Memory/` (case-insensitive on Windows)
   silently excludes `src/groundtruth/memory/`. Add `!src/groundtruth/memory/` to
   un-ignore the ONNX loader so it ships to the container.
3. Bake the e5 ONNX model (`scripts/setup_models.py`, commit b1ad929) into the image.

---

## 3. THE RANKING PROBLEM — gold buried under hubs (RANKING, not RECALL)  [D]

`LOCALIZATION_FINAL_REPORT.md:113`: *"RANKING not RECALL — gold files are in the
candidate set but score below hubs."* :115 *"Decrease W_REACH — graph reach
over-promotes hubs."* :114 *"Increase W_LEX."* :116 min-3 BM25 guarantee; :117 don't
collapse adaptive-K to 1. Measured 2026-06-03: brief delivers 5 candidates,
first@5=5/8; axum/marimo/boa golds at rank 14–22 (under hubs).

### THE DOCUMENTED LEVERS / change-list (from research agent, [A] — spot-check + measure each):
| # | file | current → target | why |
|---|---|---|---|
| 1 | v7_4_brief.py W_LEX / W_REACH | 0.50→0.60 / 0.05→0.02 | content over reach (FINAL_REPORT:114–115) |
| 2 | graph_localizer W_BM25/W_PATH_DECAY/W_LEX | 0.35→0.40 / 0.30→0.15 / 0.30→0.40 | cut reach-family dominance (≈33%→24%); BLUiR ASE 2013 |
| 3 | v1r_brief.py | add min-3 BM25 guarantee | FINAL_REPORT:116 — BM25-found gold never dropped |
| 4 | caller render | conf≥0.6 → deterministic-method OR unique name_match | DOC_OF_HONOR:352 name_match≠fact |
| 5 | graph_reach `_build_file_graph` | min_conf 0.0 → 0.5 + categorical filter | reach = structural only, no name-match noise |

Each change ships ONLY after measuring it raises first@5 on the brief, semantic ON,
without regressing the others. Change ONE variable at a time.

---

## 4. THE WRONG LEVER — do NOT chase graph reach / closure  [V]
The grep-spine architecture DELIBERATELY subordinates graph reach (grep primary;
structure promotes only on verified issue-anchored ≤1-hop edges) BECAUSE reach
over-promotes hubs. Adding more reach (closure-as-ranking-signal) measured a no-op
on the valid env (2026-06-03) and would push hubs UP, burying gold further. Closure
freshness (Part A) matters for impact/trace, NOT for brief ranking. Lever = content
+ hub demotion + confidence gate, NOT reach.

---

## 5. MEASUREMENT PROTOCOL (enforced)  [V]
See `MEASURE_BRIEF_PROTOCOL.md` + `measure_brief.py` (guarded — ABORTS unless env OK).
Before ANY number: assert (1) consensus branch, (2) semantic = container ONNX
(`_OnnxEmbedderAdapter`, block sentence_transformers), (3) graphs LSP-enriched +
closures fresh. Measure `generate_v1r_brief`, report: candidate count (~5, no
collapse), first@5/cov@10, hub-in-top-5, what's shown vs suppressed, per-language.

---

## 6. Research basis (as GT's code/docs cite it — venue+year; arXiv ids in code
comments may be placeholders, verify before quoting externally)
KGCompass 2025 (path-decay/hops) · RepoGraph ICLR 2025 (k-hop, hub caution) ·
SWERank ICLR 2025 (witness hard-negative) · BLUiR ASE 2013 (field-level lexical) ·
Lao & Cohen 2010 PRA (hub-discounted paths) · RRF Cormack SIGIR 2009 (3-way fusion) ·
Lost-in-the-Middle TACL 2024 / Power of Noise SIGIR 2024 (brief breadth, prepending).

---

## 7. ROOT-FIX LOG — localization miss (run13 arviz-2413, 2026-06-06) [V]

**Symptom:** the brief ranked `gallery_generator.py` #1 and dropped the gold `hdiplot.py`
entirely, even though the issue names `plot_hdi` 4× + links `hdiplot.py`. `BUG3_ANCHOR_PROX`
showed `ap=0` on EVERY candidate.

**LIPI (gt_gt-grounded):**
- *Plumbing* — gold in graph (`plot_hdi` 3 nodes), issue names it 4×, verified `lmplot→plot_hdi`
  edge. Data all present → not plumbing.
- *Implementation* — `extract_issue_anchors` DID extract `plot_hdi` (symbols/code_symbols True).
  The symbol was fine.
- *Integration / ROOT* — `anchor_select._embed` called `model.encode()` (sentence-transformers),
  but the container ONNX `EmbeddingModel` exposes `.embed`/`.embed_batch` and has **no `.encode`**.
  So `select_anchors.semantic_top_k` **raised → run_v74's semantic anchor selection produced ZERO
  anchors for EVERY in-container task** → `ap=0` → issue-named golds never anchored. This is the
  exact §"semantic must be ON in BOTH halves" half-on-pipeline failure: localize used the ONNX
  adapter, run_v74's anchor selection did not.
- *Logic* — gt_gt §4.1 says an issue-anchored verified-edge symbol is HIGH; with anchoring dead,
  the composite ranking buried the gold under lexical siblings.

**Fix (3 commits, repo/language-agnostic, test-blind):**
1. `83b39d80` — `_embed` dispatches `encode | embed_batch | embed` (issue=QUERY, files=PASSAGE).
   THE root: run_v74 semantic anchoring now works in-container.
2. `0df6c2de` — exact-name guarantee: a function named VERBATIM in the issue + in the graph →
   its file is force-promoted to the top candidates (backstop above the native ranker).
3. `48f63bb7` — specificity guard: drop dunders / short generics / names spread over >3 files
   (held-out loguru-1297 exposed `__init__`/`print` over-matching → 9 noise files).

**Verified (generate_v1r_brief, semantic ON):** arviz `hdiplot.py` absent→#0; held-out
loguru-1297 `_datetime.py` absent→#1, no regression. Measure with the ONNX env armed
(`GT_MODELS_ROOT`, `GT_FORCE_ONNX_EMBEDDER=1`); a bare run still degrades.

**Open follow-up:** now that `_embed` works, check whether the NATIVE ranker surfaces the gold
WITHOUT the bolt-on guarantee (ONE-PIPELINE) — if so, retire the guarantee.
