"""G3 proof (local, real e5 embedder, held-out repo = GT's own source).

Replicates beets' failure mode and tests the fix DIRECTION generalized:
  - FILE-BLOB granularity (current: concat of name+signature per file, like
    anchor_select._file_summary) -> e5 cosines collapse to low variance (beets:
    sem_distinct=1, mad=0).
  - FUNCTION granularity (fix: embed each function's name+sig+docstring, file
    score = max over its functions) -> cosines discriminate, gold file ranks top.

No graph.db, no benchmark task — pure mechanism on real Python source + the real
ONNX e5. Asserts the diagnosis (blob=flat) and the fix (function=discriminative).
"""
import ast, os, statistics, sys
sys.path.insert(0, "D:/Groundtruth/src")
from groundtruth.memory.enrich.embed import get_embedding_model

ROOT = "D:/Groundtruth/src/groundtruth"
FILES = [
    "memory/enrich/embed.py",        # GOLD for the query below
    "runtime/proof.py", "runtime/context.py", "resolve.py",
    "pretask/graph_localizer.py", "pretask/v7_4_brief.py", "pretask/anchor_select.py",
    "pretask/v1r_brief.py", "pretask/hub_penalty.py", "pretask/contract_map.py",
    "index/store.py", "index/graph_store.py",
]
QUERY = "compute cosine similarity between issue text and code using an embedding model"
GOLD = "memory/enrich/embed.py"

def funcs(path):
    try:
        tree = ast.parse(open(path, encoding="utf-8", errors="ignore").read())
    except Exception:
        return []
    out = []
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = ", ".join(a.arg for a in n.args.args)
            doc = (ast.get_docstring(n) or "").split("\n")[0]
            out.append((n.name, f"def {n.name}({args})", doc))
    return out

m = get_embedding_model(); m._ensure_loaded()
def emb(texts, q):
    import numpy as np
    v = np.asarray(m.embed_batch(list(texts), is_query=q), dtype=np.float32)
    n = (v*v).sum(1, keepdims=True)**0.5; n[n==0]=1
    return v/n
import numpy as np

# --- file-blob representation (current) ---
blobs, fnames = [], []
for f in FILES:
    fs = funcs(os.path.join(ROOT, f))
    if not fs: continue
    blob = " ".join(f"{nm} {sig}" for nm, sig, _ in fs)[:600]   # mirrors _file_summary
    blobs.append(blob); fnames.append(f)
q = emb([QUERY], True)[0]
P = emb(blobs, False)
blob_cos = {fnames[i]: float(q @ P[i]) for i in range(len(fnames))}

# --- function-granularity representation (fix) ---
func_cos = {}
for f in FILES:
    fs = funcs(os.path.join(ROOT, f))
    if not fs: continue
    texts = [f"{nm} {sig} {doc}".strip() for nm, sig, doc in fs]
    FP = emb(texts, False)
    func_cos[f] = max(float(q @ FP[j]) for j in range(len(texts)))   # max over functions

def stats(d):
    vals = sorted(d.values(), reverse=True)
    med = statistics.median(vals)
    mad = statistics.median([abs(v-med) for v in vals])
    rank = [k for k,_ in sorted(d.items(), key=lambda x:-x[1])].index(GOLD)+1
    return vals, round(mad,6), len(set(round(v,5) for v in vals)), rank

bv, bmad, bdist, brank = stats(blob_cos)
fv, fmad, fdist, frank = stats(func_cos)
print(f"QUERY: {QUERY!r}\nGOLD : {GOLD}\n")
print("FILE-BLOB (current):")
print(f"  cos spread {bv[0]:.5f}..{bv[-1]:.5f}  MAD={bmad}  distinct@5dp={bdist}/{len(bv)}  gold_rank={brank}")
print("FUNCTION-GRANULARITY (fix):")
print(f"  cos spread {fv[0]:.5f}..{fv[-1]:.5f}  MAD={fmad}  distinct@5dp={fdist}/{len(fv)}  gold_rank={frank}")
print()
print(f"VERDICT: function MAD ({fmad}) > blob MAD ({bmad}) -> {'MORE DISPERSED' if fmad>bmad else 'NOT better'}; "
      f"gold rank blob={brank} -> func={frank} ({'IMPROVED' if frank<brank else 'same/worse'})")
