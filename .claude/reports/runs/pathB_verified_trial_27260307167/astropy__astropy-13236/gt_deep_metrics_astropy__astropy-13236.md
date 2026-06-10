# Verified deep metrics — `astropy__astropy-13236`

- pipeline: `verified-miniswe` · model: `deepseek/deepseek-v4-flash`
- branch `gt-trial` @ `301e367d7c03`
- **outcome: unresolved_with_patch** · resolved=False · has_patch=True
- in_resolved_denominator: True (classification=AGENT)

## Agent behavior
- steps: 96.0 · edits: 18.0 · first_edit@: 16.0
- edited_files: ['astropy/table/table.py', 'astropy/version.py', 'astropy/__init__.py', 'astropy/_version.py', '_version.py', 'opt/miniconda3/envs/testbed/lib/python3.9/site-packages/erfa/__init__.py', 'setup.py', 'longintrepr.h', 'opt/miniconda3/include/python3.11/cpython/longintrepr.h', 'opt/miniconda3/include/python3.11/longintrepr.h']
- wall_clock_s: 313.41393256 · max_iter: 250

## Tokens & cost (per-call, model-agnostic; cost may be 0.0 — litellm unmapped for this model)
- in: 2291437.0 out: 13736.0 total: 2305173.0
- gt_sent_tokens: 708.0 · overhead%: 0.03089764
- cost_usd: 0.0 (source: none_litellm_unmapped)

## GT reached the agent (fired AND delivered — agent observation)
- brief: 2.0 · evidence: 9.0 · scope: 7.0
- contract: 2.0 · cochange: 1.0 · nudge: 3.0
- GT observation chars: 26435.0

## Substrate depth
- nodes: NOT COLLECTED edges: NOT COLLECTED det_pct: NOT COLLECTED
- LSP resolved/residual: NOT COLLECTED/3121
- substrate_digest: ghcr.io/hbali-stack/gt-substrate@sha256:db7bd22de2299ce677f78048d47a4796280556d9ef64a22853fe48eb0cfdc1b9
- graph.db sha256: 3e1fce5e572bc5f26ce6805ef2f38bc2681718983605fb478062d55a5ed19834
- trajectory sha256: 01ecf0647b4222b814deb101b45fcae13a654ef4b62ffcc6152ad877b68d0a3b

_inputs present: {'trajectory': True, 'outcome_json': True, 'substrate_certs': True, 'brief': True, 'graph_db': True}_
