"""G3 REAL root cause (corrected): _file_summary embeds text[:600] = the file
PREAMBLE (license header + imports), which is identical across files in a licensed
repo (beets is GPL) -> identical embeddings -> the exact 0.83886 x9 collapse.

Proof: take real GT source, simulate the common case (a licensed repo) by prepending
ONE shared ~520-char license header to each file (exactly what beets/most repos have),
then compare the REAL _file_summary (text[:600]) vs a symbol-content summary.
"""
import ast, os, statistics, sys
sys.path.insert(0, "D:/Groundtruth/src")
from groundtruth.pretask.anchor_select import _file_summary  # the REAL current summarizer
from groundtruth.memory.enrich.embed import get_embedding_model
import numpy as np

ROOT = "D:/Groundtruth/src/groundtruth"
FILES = ["memory/enrich/embed.py", "runtime/proof.py", "runtime/context.py", "resolve.py",
         "pretask/graph_localizer.py", "pretask/v7_4_brief.py", "pretask/anchor_select.py",
         "pretask/v1r_brief.py", "index/store.py", "index/graph_store.py"]
QUERY = "compute cosine similarity between issue text and code using an embedding model"
GOLD = "memory/enrich/embed.py"
# A standard ~520-char license header (Apache-style) — the kind every file in a
# licensed repo (beets=GPL, many=Apache/MIT) opens with. text[:600] => this header.
HEADER = ("# Copyright 2024 The Project Authors. All rights reserved.\n"
          "# Licensed under the Apache License, Version 2.0 (the \"License\");\n"
          "# you may not use this file except in compliance with the License.\n"
          "# You may obtain a copy of the License at\n"
          "#     http://www.apache.org/licenses/LICENSE-2.0\n"
          "# Unless required by applicable law or agreed to in writing, software\n"
          "# distributed under the License is distributed on an \"AS IS\" BASIS,\n"
          "# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.\n"
          "# See the License for the specific language governing permissions and\n"
          "# limitations under the License.\nimport os\nimport sys\nimport json\n\n")

m = get_embedding_model(); m._ensure_loaded()
def emb(texts, q):
    v = np.asarray(m.embed_batch(list(texts), is_query=q), dtype=np.float32)
    n = (v*v).sum(1, keepdims=True)**0.5; n[n==0]=1
    return v/n

# write header-prepended copies to a temp dir, summarize with the REAL _file_summary
tmp = "D:/Groundtruth/.claude/reports/runs/proof_probe_27166888850/_licensed_src"
cur_summaries, sym_summaries, names = [], [], []
for f in FILES:
    p = os.path.join(ROOT, f)
    try:
        body = open(p, encoding="utf-8", errors="ignore").read()
    except Exception:
        continue
    licensed = HEADER + body
    op = os.path.join(tmp, f.replace("/", "__"))
    os.makedirs(tmp, exist_ok=True); open(op, "w", encoding="utf-8").write(licensed)
    cur_summaries.append(_file_summary(os.path.basename(op), tmp))   # REAL current path: text[:600]
    # symbol summary (fix): names+signatures+docstrings from ast
    try:
        fs = [(n.name, ", ".join(a.arg for a in n.args.args), (ast.get_docstring(n) or "").split("\n")[0])
              for n in ast.walk(ast.parse(body)) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        sym_summaries.append(" ".join(f"{nm}({ar}) {dc}" for nm, ar, dc in fs)[:600] or "x")
    except Exception:
        sym_summaries.append("x")
    names.append(f)

q = emb([QUERY], True)[0]
cur = {names[i]: float(q @ emb([cur_summaries[i]], False)[0]) for i in range(len(names))}
sym = {names[i]: float(q @ emb([sym_summaries[i]], False)[0]) for i in range(len(names))}

def stats(d):
    vals = sorted(d.values(), reverse=True)
    med = statistics.median(vals); mad = statistics.median([abs(v-med) for v in vals])
    rank = [k for k,_ in sorted(d.items(), key=lambda x:-x[1])].index(GOLD)+1
    return round(min(vals),6), round(max(vals),6), round(mad,8), len(set(round(v,5) for v in vals)), rank

cmin,cmax,cmad,cdist,crank = stats(cur)
smin,smax,smad,sdist,srank = stats(sym)
print(f"QUERY {QUERY!r}  GOLD {GOLD}\n")
print(f"CURRENT  _file_summary (text[:600] = license header on a licensed repo):")
print(f"   cos {cmin}..{cmax}  MAD={cmad}  distinct@5dp={cdist}/{len(names)}  gold_rank={crank}")
print(f"FIX      symbol summary (names+sigs+docstrings):")
print(f"   cos {smin}..{smax}  MAD={smad}  distinct@5dp={sdist}/{len(names)}  gold_rank={srank}")
print(f"\nVERDICT: current collapses to {cdist} distinct value(s) (mad={cmad}) -> the 0.83886-style flat; "
      f"fix gives {sdist}/{len(names)} distinct, gold rank {crank}->{srank}.")
