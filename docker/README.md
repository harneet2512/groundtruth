# GT substrate — containerized (the agent-with-GT's library closure, guaranteed)

## Why this exists

The **baseline** (GT off) is reliable because the SWE-bench-Live **eval image** guarantees the
task's libraries (python + repo deps + test runner). **GT-on** adds a second library closure:

| pillar | libraries |
|---|---|
| resolution / JARVIS | `gt-index` (CGO + `sqlite_fts5`, correct glibc) → `graph.db` |
| LSP | `pyright` (+ Node) + `pydantic` (the client) |
| embedder | `onnxruntime` + `tokenizers` + the **e5** model |
| pipeline | `numpy` + the GT code |

Today those are **pip-installed per job**, so they silently vanish (gt_trial §1.5: a swallowed
`pip` failure → embedder dead **and** LSP dead **and** name_match-garbage, GT degraded to a grep
baseline, undetected), and the gates run **split across host/image** (conan: graph built in-image,
embedder on the host with no source → `sem_count=0`).

This image fixes the asymmetry: GT's closure is **guaranteed by a container the same way the task's
is.** Everything is built once, on the **same base as the eval images (Ubuntu 20.04 / glibc 2.31)**,
and is fully self-contained (static `gt-index`, a python-build-standalone interpreter, a bundled
Node) — so it runs unchanged **inside any eval container** and never touches the eval image's own
interpreter/libs (the baseline comparison stays fair).

## Structure

- **`docker/Dockerfile.gt-substrate`** — builds `/opt/gt` = static gt-index + portable Python + GT
  deps + Node/pyright + e5 + the pipeline. A **build-time self-test** fails the build if any library
  is missing/unrunnable → you can never ship a missing-library substrate.
- **`docker/gt-substrate-run.sh`** (`gt-substrate` in the image) — runs the **whole substrate in one
  place**: `index → resolve(LSP) → 3-GATE verdict`, against the repo's own source + deps. Fail-closed.
- **`.github/workflows/gt_substrate_image.yml`** — builds + pushes `ghcr.io/hbali-stack/gt-substrate`
  (manual dispatch), verifying the published image runs on glibc 2.31.

## How the eval pipeline uses it (integration)

Replaces the host-build-then-copy + per-job-pip-install + host-side gates with:

1. `docker pull ghcr.io/hbali-stack/gt-substrate:latest` (cached).
2. `id=$(docker create gt-substrate); docker cp $id:/opt/gt /tmp/opt-gt; docker rm $id`.
3. Into the running **eval container**: `docker cp /tmp/opt-gt <eval>:/opt/gt`.
4. In the eval container: `gt-substrate /testbed /tmp/issue.txt /tmp/gt` → `graph.db` + LSP enrichment
   + the **fail-closed 3-GATE verdict**, all in the one environment where the repo's source + deps are.

No per-job install (can't silently fail). No host/image split (embedder/gates see the source).

## The goal (proof, per gt_trial)

1. **The 10** current baseline=NO tasks → **3 gates GREEN on every one (10:10)** via this image.
2. **5 NEW held-out** baseline=NO tasks → GREEN too → proves it's the *container* guaranteeing the
   substrate, not per-task patching (generalization).
3. Only then the paired flip eval (GT-on vs the frozen baseline).
