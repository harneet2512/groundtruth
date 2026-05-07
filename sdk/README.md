# GroundTruth Python SDK

Deterministic codebase intelligence for AI coding agents — read-path only against a `graph.db` produced by **`gt-index`**.

Install (zero core deps):

```bash
pip install ./sdk
```

Build an index:

```bash
gt-index -root /path/to/repo -output graph.db
```

Minimal usage:

```python
from groundtruth import GroundTruth

gt = GroundTruth("graph.db")
print(gt.briefing("my_function").evidence_text)
```

See repository [README](../README.md) for indexer installation and MCP server.
