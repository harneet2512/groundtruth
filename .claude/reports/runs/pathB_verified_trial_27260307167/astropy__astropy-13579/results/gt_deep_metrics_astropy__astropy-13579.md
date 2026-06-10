# Verified deep metrics — `astropy__astropy-13579`

- pipeline: `verified-miniswe` · model: `deepseek/deepseek-v4-flash`
- branch `gt-trial` @ `301e367d7c03`
- **outcome: resolved** · resolved=True · has_patch=True
- in_resolved_denominator: True (classification=RESOLVED)

## Agent behavior
- steps: 33.0 · edits: 7.0 · first_edit@: 13.0
- edited_files: ['astropy/wcs/wcsapi/wrappers/sliced_wcs.py', 'astropy/wcs/wcsapi/wrappers/tests/test_sliced_wcs.py']
- wall_clock_s: 243.26526403 · max_iter: 250

## Tokens & cost (per-call, model-agnostic; cost may be 0.0 — litellm unmapped for this model)
- in: 665917.0 out: 14913.0 total: 680830.0
- gt_sent_tokens: 601.0 · overhead%: 0.09025149
- cost_usd: 0.0 (source: none_litellm_unmapped)

## GT reached the agent (fired AND delivered — agent observation)
- brief: 2.0 · evidence: 6.0 · scope: 4.0
- contract: 2.0 · cochange: 1.0 · nudge: 2.0
- GT observation chars: 27777.0

## Substrate depth
- nodes: NOT COLLECTED edges: NOT COLLECTED det_pct: NOT COLLECTED
- LSP resolved/residual: NOT COLLECTED/3106
- substrate_digest: ghcr.io/hbali-stack/gt-substrate@sha256:db7bd22de2299ce677f78048d47a4796280556d9ef64a22853fe48eb0cfdc1b9
- graph.db sha256: 415759a359f212bc6574627101985c358633ce8e282a47c2d2a68784806b9bd0
- trajectory sha256: add3592e97755db60ff021cedfe05e811ebe2ac66898a9a025c2a2b6196b9c0d

_inputs present: {'trajectory': True, 'outcome_json': True, 'substrate_certs': True, 'brief': True, 'graph_db': True}_
