# Verified deep metrics — `sympy__sympy-11618`

- pipeline: `verified-miniswe` · model: `deepseek/deepseek-v4-flash`
- branch `gt-trial` @ `301e367d7c03`
- **outcome: resolved** · resolved=True · has_patch=True
- in_resolved_denominator: True (classification=RESOLVED)

## Agent behavior
- steps: 68.0 · edits: 9.0 · first_edit@: 5.0
- edited_files: ['sympy/core/basic.py', 'sympy/geometry/point.py', 'sympy/geometry/tests/test_point.py', 'sympy/matrices/sparse.py', 'sympy/plotting/plot.py', 'sympy/assumptions/sathandlers.py', 'point.py']
- wall_clock_s: 225.21403694 · max_iter: 250

## Tokens & cost (per-call, model-agnostic; cost may be 0.0 — litellm unmapped for this model)
- in: 991948.0 out: 9886.0 total: 1001834.0
- gt_sent_tokens: 672.0 · overhead%: 0.06774549
- cost_usd: 0.0 (source: none_litellm_unmapped)

## GT reached the agent (fired AND delivered — agent observation)
- brief: 2.0 · evidence: 11.0 · scope: 4.0
- contract: 5.0 · cochange: 1.0 · nudge: 2.0
- GT observation chars: 19776.0

## Substrate depth
- nodes: NOT COLLECTED edges: NOT COLLECTED det_pct: NOT COLLECTED
- LSP resolved/residual: NOT COLLECTED/2305
- substrate_digest: ghcr.io/hbali-stack/gt-substrate@sha256:db7bd22de2299ce677f78048d47a4796280556d9ef64a22853fe48eb0cfdc1b9
- graph.db sha256: 33fc5ff76d02d317ec2483685a437f53f800de18d3594b7c376ab0fbe3ccc08c
- trajectory sha256: 9678e757102553fe121a3f76370eccfc98c85289aba96b27120f6e0b12533b9e

_inputs present: {'trajectory': True, 'outcome_json': True, 'substrate_certs': True, 'brief': True, 'graph_db': True}_
