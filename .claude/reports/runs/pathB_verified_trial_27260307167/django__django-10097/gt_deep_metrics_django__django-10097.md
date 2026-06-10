# Verified deep metrics — `django__django-10097`

- pipeline: `verified-miniswe` · model: `deepseek/deepseek-v4-flash`
- branch `gt-trial` @ `301e367d7c03`
- **outcome: unresolved_with_patch** · resolved=False · has_patch=True
- in_resolved_denominator: True (classification=AGENT)

## Agent behavior
- steps: 79.0 · edits: 5.0 · first_edit@: 43.0
- edited_files: ['django/core/validators.py', 'tests/validators/tests.py']
- wall_clock_s: 352.8816545 · max_iter: 250

## Tokens & cost (per-call, model-agnostic; cost may be 0.0 — litellm unmapped for this model)
- in: 2275030.0 out: 27015.0 total: 2302045.0
- gt_sent_tokens: 686.0 · overhead%: 0.03015345
- cost_usd: 0.0 (source: none_litellm_unmapped)

## GT reached the agent (fired AND delivered — agent observation)
- brief: 2.0 · evidence: 6.0 · scope: 4.0
- contract: 2.0 · cochange: 1.0 · nudge: 3.0
- GT observation chars: 22277.0

## Substrate depth
- nodes: NOT COLLECTED edges: NOT COLLECTED det_pct: NOT COLLECTED
- LSP resolved/residual: NOT COLLECTED/2015
- substrate_digest: ghcr.io/hbali-stack/gt-substrate@sha256:db7bd22de2299ce677f78048d47a4796280556d9ef64a22853fe48eb0cfdc1b9
- graph.db sha256: eb9f97446064ac5415b09d8e245138d2b130570b6ed04dc3c0f898c559ec3ad7
- trajectory sha256: 2604a98646566ec28045be38774b556c88c2357a1f22eae642d1a974af4f5f16

_inputs present: {'trajectory': True, 'outcome_json': True, 'substrate_certs': True, 'brief': True, 'graph_db': True}_
