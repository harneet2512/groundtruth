# Verified deep metrics — `astropy__astropy-13033`

- pipeline: `verified-miniswe` · model: `deepseek/deepseek-v4-flash`
- branch `gt-trial` @ `301e367d7c03`
- **outcome: unresolved_with_patch** · resolved=False · has_patch=True
- in_resolved_denominator: True (classification=AGENT)

## Agent behavior
- steps: 62.0 · edits: 15.0 · first_edit@: 5.0
- edited_files: ['astropy/timeseries/binned.py', 'astropy/timeseries/core.py', 'binned.py', 'setup.py', 'longintrepr.h', 'tmp/fix_core.py', 'astropy/timeseries/tests/test_sampled.py']
- wall_clock_s: 215.06733394 · max_iter: 250

## Tokens & cost (per-call, model-agnostic; cost may be 0.0 — litellm unmapped for this model)
- in: 1142314.0 out: 9574.0 total: 1151888.0
- gt_sent_tokens: 686.0 · overhead%: 0.06005354
- cost_usd: 0.0 (source: none_litellm_unmapped)

## GT reached the agent (fired AND delivered — agent observation)
- brief: 2.0 · evidence: 10.0 · scope: 7.0
- contract: 2.0 · cochange: 1.0 · nudge: 3.0
- GT observation chars: 38344.0

## Substrate depth
- nodes: NOT COLLECTED edges: NOT COLLECTED det_pct: NOT COLLECTED
- LSP resolved/residual: NOT COLLECTED/3063
- substrate_digest: ghcr.io/hbali-stack/gt-substrate@sha256:db7bd22de2299ce677f78048d47a4796280556d9ef64a22853fe48eb0cfdc1b9
- graph.db sha256: 90ceffdc4e4a14c7230e3690b27b61b1fb388b04dc9115b423b44d8c7e46b52d
- trajectory sha256: c19cb0b1efb5a8007fb19273f9aa9f69a079b53d3818c5a3dc888aa61d11579b

_inputs present: {'trajectory': True, 'outcome_json': True, 'substrate_certs': True, 'brief': True, 'graph_db': True}_
