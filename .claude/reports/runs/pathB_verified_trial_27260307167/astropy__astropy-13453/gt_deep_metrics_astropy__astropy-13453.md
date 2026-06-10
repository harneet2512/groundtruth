# Verified deep metrics — `astropy__astropy-13453`

- pipeline: `verified-miniswe` · model: `deepseek/deepseek-v4-flash`
- branch `gt-trial` @ `301e367d7c03`
- **outcome: resolved** · resolved=True · has_patch=True
- in_resolved_denominator: True (classification=RESOLVED)

## Agent behavior
- steps: 50.0 · edits: 11.0 · first_edit@: 19.0
- edited_files: ['astropy/io/ascii/html.py', 'astropy/io/ascii/tests/test_html.py', 'astropy/io/ascii/tests/test_write.py', 'astropy/io/ascii/tests/test_connect.py']
- wall_clock_s: 190.91150069 · max_iter: 250

## Tokens & cost (per-call, model-agnostic; cost may be 0.0 — litellm unmapped for this model)
- in: 1041995.0 out: 6619.0 total: 1048614.0
- gt_sent_tokens: 747.0 · overhead%: 0.0716894
- cost_usd: 0.0 (source: none_litellm_unmapped)

## GT reached the agent (fired AND delivered — agent observation)
- brief: 2.0 · evidence: 10.0 · scope: 5.0
- contract: 2.0 · cochange: 1.0 · nudge: 2.0
- GT observation chars: 39215.0

## Substrate depth
- nodes: NOT COLLECTED edges: NOT COLLECTED det_pct: NOT COLLECTED
- LSP resolved/residual: NOT COLLECTED/3101
- substrate_digest: ghcr.io/hbali-stack/gt-substrate@sha256:db7bd22de2299ce677f78048d47a4796280556d9ef64a22853fe48eb0cfdc1b9
- graph.db sha256: 7d8321b9872b9d012a3ac707212d61ba98e23692409d93214f7932047bef94e8
- trajectory sha256: 7f5a3090cf976a8339388b3daeb55726cd4e475c36e268389cc55e1e5a424c9a

_inputs present: {'trajectory': True, 'outcome_json': True, 'substrate_certs': True, 'brief': True, 'graph_db': True}_
