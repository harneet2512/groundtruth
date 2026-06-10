# Verified deep metrics — `astropy__astropy-13398`

- pipeline: `verified-miniswe` · model: `deepseek/deepseek-v4-flash`
- branch `gt-trial` @ `301e367d7c03`
- **outcome: unresolved_with_patch** · resolved=False · has_patch=True
- in_resolved_denominator: True (classification=AGENT)

## Agent behavior
- steps: 48.0 · edits: 16.0 · first_edit@: 13.0
- edited_files: ['astropy/coordinates/builtin_frames/altaz.py', 'astropy/coordinates/builtin_frames/hadec.py', 'astropy/coordinates/builtin_frames/utils.py', 'astropy/coordinates/builtin_frames/itrs_observed_transforms.py', 'itrs_observed_transforms.py', 'astropy/coordinates/builtin_frames/__init__.py', 'astropy/coordinates/tests/test_intermediate_transformations.py', 'a/astropy/coordinates/builtin_frames/itrs_observed_transforms.py', 'b/astropy/coordinates/builtin_frames/itrs_observed_transforms.py']
- wall_clock_s: 227.31242561 · max_iter: 250

## Tokens & cost (per-call, model-agnostic; cost may be 0.0 — litellm unmapped for this model)
- in: 1254914.0 out: 9862.0 total: 1264776.0
- gt_sent_tokens: 741.0 · overhead%: 0.05904787
- cost_usd: 0.0 (source: none_litellm_unmapped)

## GT reached the agent (fired AND delivered — agent observation)
- brief: 2.0 · evidence: 13.0 · scope: 10.0
- contract: 2.0 · cochange: 1.0 · nudge: 2.0
- GT observation chars: 55539.0

## Substrate depth
- nodes: NOT COLLECTED edges: NOT COLLECTED det_pct: NOT COLLECTED
- LSP resolved/residual: NOT COLLECTED/3102
- substrate_digest: ghcr.io/hbali-stack/gt-substrate@sha256:db7bd22de2299ce677f78048d47a4796280556d9ef64a22853fe48eb0cfdc1b9
- graph.db sha256: d5e457596272254e46e386be381bf7aec1533bd3946a9e28455bbc8e9cc26bcd
- trajectory sha256: fda2f2fc1442ce74e69372ce7e6a740af840fafa64141d78c9a06ed179dde663

_inputs present: {'trajectory': True, 'outcome_json': True, 'substrate_certs': True, 'brief': True, 'graph_db': True}_
