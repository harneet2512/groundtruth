# Verified deep metrics — `django__django-10554`

- pipeline: `verified-miniswe` · model: `deepseek/deepseek-v4-flash`
- branch `gt-trial` @ `301e367d7c03`
- **outcome: unresolved_with_patch** · resolved=False · has_patch=True
- in_resolved_denominator: True (classification=AGENT)

## Agent behavior
- steps: 112.0 · edits: 20.0 · first_edit@: 19.0
- edited_files: ['test_sqlite.py', 'django/db/models/sql/compiler.py', 'django/db/models/query.py', 'home/bbt/.virtualenvs/ispnext/lib/python3.6/site-packages/django/db/models/query.py', 'tmp/reproduce.py', 'tmp/patch.py', 'django/db/models/sql/query.py', 'tests/queries/test_qs_combinators.py', 'tests/runtests.py']
- wall_clock_s: 603.96364403 · max_iter: 250

## Tokens & cost (per-call, model-agnostic; cost may be 0.0 — litellm unmapped for this model)
- in: 5069498.0 out: 43641.0 total: 5113139.0
- gt_sent_tokens: 798.0 · overhead%: 0.0157412
- cost_usd: 0.0 (source: none_litellm_unmapped)

## GT reached the agent (fired AND delivered — agent observation)
- brief: 2.0 · evidence: 9.0 · scope: 11.0
- contract: 2.0 · cochange: 1.0 · nudge: 3.0
- GT observation chars: 42451.0

## Substrate depth
- nodes: NOT COLLECTED edges: NOT COLLECTED det_pct: NOT COLLECTED
- LSP resolved/residual: NOT COLLECTED/2029
- substrate_digest: ghcr.io/hbali-stack/gt-substrate@sha256:db7bd22de2299ce677f78048d47a4796280556d9ef64a22853fe48eb0cfdc1b9
- graph.db sha256: 3f9ed53df13e196ce1eaf6c23b41b7dc7399c456c2cfe360ce0adae6ad2894b5
- trajectory sha256: 0caec5d0fd2f15795708af2e891dff13557892f86ed80f75dba0f6983781b8d9

_inputs present: {'trajectory': True, 'outcome_json': True, 'substrate_certs': True, 'brief': True, 'graph_db': True}_
