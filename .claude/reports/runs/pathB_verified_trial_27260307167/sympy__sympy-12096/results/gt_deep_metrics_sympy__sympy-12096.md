# Verified deep metrics — `sympy__sympy-12096`

- pipeline: `verified-miniswe` · model: `deepseek/deepseek-v4-flash`
- branch `gt-trial` @ `301e367d7c03`
- **outcome: resolved** · resolved=True · has_patch=True
- in_resolved_denominator: True (classification=RESOLVED)

## Agent behavior
- steps: 51.0 · edits: 13.0 · first_edit@: 11.0
- edited_files: ['setup.py', 'sympy/core/basic.py', 'sympy/plotting/plot.py', 'sympy/core/function.py', 'sympy/core/tests/test_function.py', 'sympy/utilities/tests/test_lambdify.py']
- wall_clock_s: 150.46497965 · max_iter: 250

## Tokens & cost (per-call, model-agnostic; cost may be 0.0 — litellm unmapped for this model)
- in: 609334.0 out: 7725.0 total: 617059.0
- gt_sent_tokens: 578.0 · overhead%: 0.09485766
- cost_usd: 0.0 (source: none_litellm_unmapped)

## GT reached the agent (fired AND delivered — agent observation)
- brief: 2.0 · evidence: 12.0 · scope: 6.0
- contract: 4.0 · cochange: 1.0 · nudge: 1.0
- GT observation chars: 23772.0

## Substrate depth
- nodes: NOT COLLECTED edges: NOT COLLECTED det_pct: NOT COLLECTED
- LSP resolved/residual: NOT COLLECTED/5735
- substrate_digest: ghcr.io/hbali-stack/gt-substrate@sha256:db7bd22de2299ce677f78048d47a4796280556d9ef64a22853fe48eb0cfdc1b9
- graph.db sha256: 6e5aa2c45e1c1c4f81cb8cd8afa76fecf44279efaf18969309244cee76c4e6ae
- trajectory sha256: 0491d607b74b55b11bf8d77e61db618cd5d6a0384ba936d313e6ccc3bfe65fce

_inputs present: {'trajectory': True, 'outcome_json': True, 'substrate_certs': True, 'brief': True, 'graph_db': True}_
