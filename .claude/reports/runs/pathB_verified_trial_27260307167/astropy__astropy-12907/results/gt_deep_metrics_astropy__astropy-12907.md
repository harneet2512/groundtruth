# Verified deep metrics — `astropy__astropy-12907`

- pipeline: `verified-miniswe` · model: `deepseek/deepseek-v4-flash`
- branch `gt-trial` @ `301e367d7c03`
- **outcome: resolved** · resolved=True · has_patch=True
- in_resolved_denominator: True (classification=RESOLVED)

## Agent behavior
- steps: 39.0 · edits: 6.0 · first_edit@: 7.0
- edited_files: ['setup.py', 'astropy/modeling/separable.py', 'astropy/utils/src/compiler.c']
- wall_clock_s: 169.02382708 · max_iter: 250

## Tokens & cost (per-call, model-agnostic; cost may be 0.0 — litellm unmapped for this model)
- in: 512416.0 out: 10508.0 total: 522924.0
- gt_sent_tokens: 549.0 · overhead%: 0.10713951
- cost_usd: 0.0 (source: none_litellm_unmapped)

## GT reached the agent (fired AND delivered — agent observation)
- brief: 2.0 · evidence: 6.0 · scope: 6.0
- contract: 2.0 · cochange: 1.0 · nudge: 2.0
- GT observation chars: 27487.0

## Substrate depth
- nodes: NOT COLLECTED edges: NOT COLLECTED det_pct: NOT COLLECTED
- LSP resolved/residual: NOT COLLECTED/3092
- substrate_digest: ghcr.io/hbali-stack/gt-substrate@sha256:db7bd22de2299ce677f78048d47a4796280556d9ef64a22853fe48eb0cfdc1b9
- graph.db sha256: b142b638ff5c2d61e48e60ad58d53ac496195d9f9c2a424f1e873c00ec4cf584
- trajectory sha256: 11e1691371b9cbef5520ff6924da11199fb8e669d949a469589324df9e52c31b

_inputs present: {'trajectory': True, 'outcome_json': True, 'substrate_certs': True, 'brief': True, 'graph_db': True}_
