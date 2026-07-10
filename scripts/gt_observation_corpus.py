#!/usr/bin/env python3
"""gt_observation_corpus.py — D0: the versioned OBSERVATION-SCHEMA corpus.

WHY (charter gt_gt §24 + delivery-research): GT must render facts into the EXACT native
observation grammar each harness/model was RL-trained on. That grammar is only known
EMPIRICALLY. Before converting any ``<gt-*>`` surface to a native observation, we must
CAPTURE and VERSION the real observation shapes from the ~1,377 run trajectories already
on disk. This is the substrate for the FORMAT A/B and the D1 renderer-contract tests.

Two trajectory shapes (shape-aware). The shape-detection + parsing is a small LOCAL
MIRROR of scripts/gt_substitution_grader.py (``_canonical_trajectory`` / ``_iter_openhands``
/ ``_iter_miniswe``) and scripts/swebench/gt_deep_metrics.py — it imports nothing private;
the mirror is intentional so a future reader knows where the canonical readers live.

  * OpenHands ``output.jsonl``  : a single-line JSON doc; ``.history[]`` holds action
    events and OBSERVATION events. The observation's model-facing TEXT is ``content``;
    its class is the ``observation`` field; run status is ``success`` +
    ``extras.metadata.exit_code``.
  * mini-swe ``*.trajectory.json`` : ``.messages[]`` (system / user / assistant / tool /
    exit). The model-facing TEXT is the ``role=tool`` message ``content``, formatted
    ``<returncode>N</returncode>\\n<output>...\\n</output>`` (with an optional
    ``<exception>...</exception>`` prefix). The event class must be INFERRED from the
    PRECEDING assistant command (mini has no structured action type).

This is a MEASUREMENT/EXTRACTION-ONLY reader. It edits nothing, calls no LLM, hits no
network, runs no harness, and writes only under an explicit ``--out-dir``.

Determinism: same input dir -> byte-identical catalog. Inputs iterated in SORTED order;
NO timestamps inside the payload (only in the output DIR/filename, chosen by the caller);
everything sorted; numeric fractions at 8-decimal precision.

Redaction (leak-safety, per CLAUDE.md): representative samples KEEP the structural shape
+ field names + structural values (observation class, role, exit codes, tag scaffold) but
SCRUB repo-identifying free text (commands, file paths, code bodies, diffs) to ``«redacted»``.

Usage:
  gt_observation_corpus.py --in D:/gt_runs --out-dir D:/gt_runs/observation_corpus_20260710/
  gt_observation_corpus.py --in <dir>            # print catalog JSON to stdout, no write
"""
from __future__ import annotations

import argparse
import glob as _glob
import hashlib
import json
import os
import re
from collections import Counter

SCHEMA_VERSION = "d0.1.0"

# Representative samples retained per (shape, event_class). Counting is exhaustive; only
# the DISTINCT redacted skeletons kept are capped, for a small deterministic catalog.
SAMPLE_CAP = 3

# ---------------------------------------------------------------------------
# Canonical event taxonomy (uniform across shapes; detection differs per shape).
# The four RENDERER-TARGET classes are where GT would inject native facts.
# ---------------------------------------------------------------------------
RENDERER_TARGET_CLASSES = ("search-result", "file-view", "edit-result", "test-run")
EVENT_CLASSES = (
    "search-result", "file-view", "edit-result", "test-run",
    "submit-finish", "error-observation", "other-command-result",
    "other-observation", "task-brief",
)

# ---------------------------------------------------------------------------
# Command classifiers (mirror of gt_substitution_grader — trimmed to what we need).
# ---------------------------------------------------------------------------
_GREP_BIN = re.compile(r"\b(?:grep|egrep|fgrep|rg|ag|ack)\b")
_GIT_GREP = re.compile(r"\bgit\s+grep\b")
_FIND_BIN = re.compile(r"\b(?:find|fd)\b")
_FIND_NAME_ARG = re.compile(r"-(?:name|iname|path|regex)\s+")
_VIEW_VERB = re.compile(r"\b(?:cat|head|tail|less|more|nl|view|open|bat)\b")
_SED_VIEW = re.compile(r"\bsed\s+-n\b")
_SRC_EXT = (r"(?:py|pyi|go|ts|tsx|js|jsx|mjs|cjs|java|rb|rs|c|cc|cpp|cxx|h|hpp|"
            r"kt|kts|scala|php|swift|m|mm|cs)")
_EDIT_SIG = re.compile(
    r"(?:str_replace|apply_patch|insert_line|new_str|\bpatch\b|\btee\b|"
    r"sed\s+-i|>>?\s*[\w./\-]+\.%s|python[0-9.]*\s+-c)" % _SRC_EXT)
_TEST_RUN = re.compile(
    r"\b(?:pytest|py\.test|unittest|nosetests|tox|go\s+test|cargo\s+test|"
    r"npx\s+jest|jest|vitest|mocha|mvn\s+(?:test|verify)|gradle\s+test|"
    r"rspec|ruby\s+-Itest|npm\s+(?:test|run\s+test)|yarn\s+test|ctest|"
    r"phpunit|dotnet\s+test)\b")
_INFRA_PROBE = re.compile(
    r"gt_root\.txt|/opt/gt|\bgo\.mod\b|package\.json|pyproject|Cargo\.toml|"
    r"tsconfig|site-packages|node_modules")
# tight edit-FAILURE markers (avoid the generic word "Error" in normal stdout)
_EDIT_FAIL = re.compile(
    r"No replacement was performed|did not appear|ERROR: No |No changes to make|"
    r"parameter .* is required|Ran into .* while trying to replace", re.I)
_TIMEOUT_MARK = re.compile(r"timed out after|command timed out|TimeoutError", re.I)


def classify_command(cmd: str) -> str:
    """Classify a shell command into an event-CLASS token. Priority edit>test>search>view."""
    c = cmd or ""
    is_test = bool(_TEST_RUN.search(c))
    is_edit = bool(_EDIT_SIG.search(c))
    is_search = bool((_GREP_BIN.search(c) or _GIT_GREP.search(c)
                      or (_FIND_BIN.search(c) and _FIND_NAME_ARG.search(c)))
                     and not _INFRA_PROBE.search(c))
    is_view = bool((_VIEW_VERB.search(c) or _SED_VIEW.search(c)) and not is_edit)
    if is_test:
        return "test-run"
    if is_edit:
        return "edit-result"
    if is_search:
        return "search-result"
    if is_view:
        return "file-view"
    return "other-command-result"


# ---------------------------------------------------------------------------
# Canonical model-facing text field MAP (per shape × class). This is the MUTATION
# handle: extraction routes the observation text pull through here, so breaking a
# path makes the fixture test bite.
# ---------------------------------------------------------------------------
def text_field_for(shape: str, event_class: str):
    """Return the JSON key-path where the model-facing observation TEXT lives, or None
    for terminal/agent-authored classes (submit-finish)."""
    if event_class == "submit-finish":
        return None
    if shape == "output_jsonl":
        if event_class == "task-brief":
            return "instruction"
        return "history[].content"
    if shape == "miniswe_trajectory":
        # brief AND every tool observation surface the text as messages[].content
        return "messages[].content"
    return None


def _text(env: dict, shape: str, event_class: str, override_field: str | None = None):
    """Pull the model-facing text out of an envelope via the canonical field path."""
    field = override_field if override_field is not None else text_field_for(shape, event_class)
    if not field:
        return None
    leaf = field.split(".")[-1].replace("[]", "")
    val = env.get(leaf)
    return val if isinstance(val, str) else (json.dumps(val) if val is not None else None)


# ---------------------------------------------------------------------------
# Shape detection
# ---------------------------------------------------------------------------
def detect_shape(path: str) -> str:
    p = (path or "").replace("\\", "/")
    if p.endswith("output.jsonl"):
        return "output_jsonl"
    if p.endswith(".trajectory.json"):
        return "miniswe_trajectory"
    return "unknown"


def _canonical_trajectories(input_dir: str) -> list[tuple[str, str]]:
    """All trajectory files under input_dir, SORTED, each tagged with its shape."""
    found: list[tuple[str, str]] = []
    for p in _glob.glob(os.path.join(input_dir, "**", "output.jsonl"), recursive=True):
        found.append((p.replace("\\", "/"), "output_jsonl"))
    for pat in ("mini-swe-agent.trajectory.json", "*.trajectory.json"):
        for p in _glob.glob(os.path.join(input_dir, "**", pat), recursive=True):
            pp = p.replace("\\", "/")
            if not pp.endswith(".trajectory.json"):
                continue
            found.append((pp, "miniswe_trajectory"))
    # dedup (the two globs can both catch mini-swe-agent.trajectory.json) + sort
    return sorted(set(found), key=lambda t: (t[0], t[1]))


# ---------------------------------------------------------------------------
# Keypath fingerprint
# ---------------------------------------------------------------------------
def keypaths(obj, prefix: str = "") -> set:
    """Recursive set of dotted key-paths (dict keys only; list -> '[]' marker)."""
    out: set = set()
    if isinstance(obj, dict):
        for k in obj:
            kp = f"{prefix}.{k}" if prefix else str(k)
            out.add(kp)
            out |= keypaths(obj[k], kp)
    elif isinstance(obj, list):
        kp = f"{prefix}[]"
        for el in obj:
            out |= keypaths(el, kp)
    return out


# ---------------------------------------------------------------------------
# Sub-status detection
# ---------------------------------------------------------------------------
def _oh_run_substatus(event_class: str, content: str, exit_code, success, suffix: str) -> str:
    body = (content or "")
    # timeout is a SUB-STATUS of whatever command class ran (test-run/search/…/generic),
    # NOT a separate class — it keeps the "this was a test/grep/…" signal for the renderer.
    if _TIMEOUT_MARK.search(suffix or "") or exit_code == -1:
        return "timeout"
    if event_class == "search-result":
        return "ok" if body.strip() else "empty"
    if event_class == "test-run":
        return "pass" if (exit_code == 0 or success is True) else "fail"
    # generic command
    return "ok" if (exit_code == 0 or success is True) else "nonzero"


def _mini_output_body(content: str) -> str:
    m = re.search(r"<output>\n?(.*?)\n?</output>", content or "", re.S)
    return m.group(1) if m else (content or "")


def _mini_substatus(event_class: str, content: str, returncode, exc: str) -> str:
    if _TIMEOUT_MARK.search(content or ""):
        return "timeout"
    body = _mini_output_body(content)
    if event_class == "search-result":
        return "ok" if body.strip() else "empty"
    if event_class == "test-run":
        return "pass" if returncode in (0, "0") else "fail"
    if event_class == "file-view":
        return "ok" if returncode in (0, "0") else "error"
    if event_class == "edit-result":
        if returncode not in (0, "0") or _EDIT_FAIL.search(body):
            return "failure"
        return "success"
    return "ok" if returncode in (0, "0") else "nonzero"


# ---------------------------------------------------------------------------
# EXTRACTION — OpenHands
# ---------------------------------------------------------------------------
def _mk_event(shape, event_class, sub_status, env, model_text, model_text_field):
    return {
        "shape": shape,
        "event_class": event_class,
        "sub_status": sub_status,
        "model_text": model_text,
        "model_text_field": model_text_field,
        "keypaths": sorted(keypaths(env, "history[]" if shape == "output_jsonl"
                                     else "messages[]")),
        "_env": env,
    }


def _extract_oh(doc: dict) -> list[dict]:
    events: list[dict] = []
    instr = doc.get("instruction")
    if isinstance(instr, str) and "<gt-" in instr:
        # the GT delivery channel (input, not an observation) — catalogued as informational
        events.append(_mk_event("output_jsonl", "task-brief", "delivered",
                                 {"instruction": instr}, _text({"instruction": instr},
                                 "output_jsonl", "task-brief"), "instruction"))
    saw_finish = False
    for e in doc.get("history") or []:
        if not isinstance(e, dict):
            continue
        obs = e.get("observation")
        act = e.get("action")
        if obs == "run":
            extras = e.get("extras") or {}
            md = extras.get("metadata") or {}
            cmd = extras.get("command") or ""
            cls = classify_command(cmd)
            if cls not in ("search-result", "test-run", "file-view", "edit-result"):
                cls = "other-command-result"
            ec = md.get("exit_code")
            suffix = md.get("suffix") or ""
            content = e.get("content") or ""
            sub = _oh_run_substatus(cls, content, ec, e.get("success"), suffix)
            events.append(_mk_event("output_jsonl", cls, sub, e,
                                     _text(e, "output_jsonl", cls), "history[].content"))
        elif obs == "read":
            content = e.get("content") or ""
            sub = "error" if re.match(r"\s*ERROR", content) else "ok"
            events.append(_mk_event("output_jsonl", "file-view", sub, e,
                                     _text(e, "output_jsonl", "file-view"), "history[].content"))
        elif obs == "edit":
            content = e.get("content") or ""
            sub = "failure" if _EDIT_FAIL.search(content) else "success"
            events.append(_mk_event("output_jsonl", "edit-result", sub, e,
                                     _text(e, "output_jsonl", "edit-result"), "history[].content"))
        elif act == "finish":
            saw_finish = True
            events.append(_mk_event("output_jsonl", "submit-finish", "submitted", e, None, None))
        elif obs in ("recall", "think", "task_tracking", "agent_state_changed"):
            events.append(_mk_event("output_jsonl", "other-observation", str(obs), e,
                                     _text(e, "output_jsonl", "other-observation"),
                                     "history[].content"))
    # doc-level terminal error (RuntimeError iteration cap etc.) — an error terminal state
    err = doc.get("error")
    if isinstance(err, str) and err.strip():
        sub = "iteration-cap" if "maximum iteration" in err.lower() else "run-error"
        env = {"error": err}
        events.append(_mk_event("output_jsonl", "error-observation", sub, env, err, "error"))
        if not saw_finish:
            events.append(_mk_event("output_jsonl", "submit-finish", "no-submit", env, None, None))
    return events


# ---------------------------------------------------------------------------
# EXTRACTION — mini-swe
# ---------------------------------------------------------------------------
def _extract_mini(doc: dict) -> list[dict]:
    events: list[dict] = []
    pending: list[str] = []  # commands from the last assistant turn(s), FIFO
    saw_user_brief = False
    for m in doc.get("messages") or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role == "user" and not saw_user_brief:
            saw_user_brief = True
            content = m.get("content")
            if isinstance(content, str) and ("<gt-" in content or "<pr_description>" in content):
                events.append(_mk_event("miniswe_trajectory", "task-brief", "delivered", m,
                                         _text(m, "miniswe_trajectory", "task-brief"),
                                         "messages[].content"))
            continue
        if role == "assistant":
            for a in (m.get("extra") or {}).get("actions") or []:
                if isinstance(a, dict) and isinstance(a.get("command"), str):
                    pending.append(a["command"])
            continue
        if role == "tool":
            extra = m.get("extra") or {}
            content = m.get("content") or ""
            if isinstance(content, list):
                content = json.dumps(content)
            returncode = extra.get("returncode")
            exc = extra.get("exception_info") or ""
            cmd = pending.pop(0) if pending else ""
            if exc.strip():
                cls, sub = "error-observation", "not-executed"
            else:
                cls = classify_command(cmd)
                sub = _mini_substatus(cls, content, returncode, exc)
            events.append(_mk_event("miniswe_trajectory", cls, sub, m,
                                     _text(m, "miniswe_trajectory", cls), "messages[].content"))
            continue
        if role == "exit":
            status = (m.get("extra") or {}).get("exit_status") or "unknown"
            sub = "submitted" if str(status).lower() == "submitted" else str(status)
            events.append(_mk_event("miniswe_trajectory", "submit-finish", sub, m, None, None))
            continue
    # info-level terminal status (covers runs whose exit message was not 'Submitted')
    info = doc.get("info") or {}
    st = info.get("exit_status")
    if isinstance(st, str) and st and str(st).lower() != "submitted":
        # only record if the messages didn't already carry an exit turn of this status
        has_exit = any(m.get("role") == "exit" for m in (doc.get("messages") or [])
                       if isinstance(m, dict))
        if not has_exit:
            events.append(_mk_event("miniswe_trajectory", "submit-finish", str(st),
                                     {"info": {"exit_status": st}}, None, None))
    return events


def extract_events(doc: dict, shape: str) -> list[dict]:
    if shape == "output_jsonl":
        return _extract_oh(doc)
    if shape == "miniswe_trajectory":
        return _extract_mini(doc)
    return []


def _load(path: str, shape: str) -> dict | None:
    try:
        if shape == "output_jsonl":
            with open(path, encoding="utf-8", errors="replace") as fh:
                return json.loads(fh.readline())
        with open(path, encoding="utf-8", errors="replace") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# REDACTION (deterministic, leak-safe)
# ---------------------------------------------------------------------------
# 'prefix'/'suffix' are OH harness BOILERPLATE appended to the rendered observation
# ("[The command completed with exit code N.]" etc.) — part of the native observation
# grammar, contains no repo content, so KEEP them (structural).
_STRUCT_KEYS = {"observation", "action", "source", "role", "success", "recall_type",
                "impl_source", "exit_status", "type", "trajectory_format", "prev_exist",
                "confirmation_state", "blocking", "is_input", "is_static", "hidden",
                "returncode", "exit_code", "prefix", "suffix"}
_FREETEXT_KEYS = {"content", "output", "raw_output", "submission", "final_thought",
                  "thought", "message", "diff", "new_content", "old_content", "new_str",
                  "old_str", "file_text", "path", "error", "exception_info", "command",
                  "_diff_cache"}


def _redact_cmd(cmd: str) -> str:
    toks = (cmd or "").split()
    return (toks[0] + " «redacted»") if toks else "«redacted»"


def _redact_mini_text(content: str) -> str:
    if not content:
        return content
    s = re.sub(r"(<exception>).*?(</exception>)", r"\1«redacted»\2", content, flags=re.S)

    def _out(m):
        body = m.group(1)
        n = len([ln for ln in body.splitlines() if ln.strip()])
        return "<output>\n«redacted: %d nonempty-lines»\n</output>" % n
    s = re.sub(r"<output>\n?(.*?)\n?</output>", _out, s, flags=re.S)
    return s


def _redact_plain_text(content: str) -> str:
    if content is None:
        return None
    n = len([ln for ln in content.splitlines() if ln.strip()])
    return "«redacted: %d nonempty-lines»" % n


def _redact_env(obj, key=None):
    if isinstance(obj, dict):
        return {k: _redact_env(obj[k], k) for k in sorted(obj)}
    if isinstance(obj, list):
        return [_redact_env(v, key) for v in obj[:2]]
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, (int, float)):
        return obj
    if isinstance(obj, str):
        if key in _STRUCT_KEYS:
            return obj
        if key == "command":
            return _redact_cmd(obj)
        if key in _FREETEXT_KEYS:
            return "«redacted»" if obj else obj
        return "«redacted»" if obj else obj
    return obj


def _redact_model_text(shape: str, text: str) -> str | None:
    if text is None:
        return None
    if shape == "miniswe_trajectory":
        return _redact_mini_text(text)
    return _redact_plain_text(text)


# ---------------------------------------------------------------------------
# CATALOG BUILD
# ---------------------------------------------------------------------------
def _status(seen_trajs: int, stable: bool) -> str:
    if seen_trajs == 0:
        return "ABSENT"
    if seen_trajs == 1:
        return "RARE"  # single witness -> cross-trajectory schema stability unconfirmable
    return "CAPTURED" if stable else "CAPTURED-UNSTABLE"


def build_catalog(input_dir: str) -> dict:
    input_dir = input_dir.replace("\\", "/")
    trajs = _canonical_trajectories(input_dir)
    by_shape = Counter()
    unreadable = 0

    # accumulator: (shape, class) -> dict
    acc: dict[tuple, dict] = {}
    # per (shape,class) union-per-trajectory to test stability
    per_traj_union: dict[tuple, set] = {}

    def bucket(shape, cls):
        k = (shape, cls)
        if k not in acc:
            acc[k] = {
                "event_count": 0,
                "trajectories": set(),
                "sub_status": Counter(),
                "union_kp": set(),
                "inter_kp": None,
                "text_fields": set(),
                "samples": {},  # redacted-skeleton-json -> sample dict
                "traj_unions": set(),  # frozensets of per-traj union keypaths
            }
        return acc[k]

    for path, shape in trajs:
        doc = _load(path, shape)
        if doc is None:
            unreadable += 1
            continue
        by_shape[shape] += 1
        events = extract_events(doc, shape)
        # per-trajectory union keypaths per class (for stability)
        traj_class_kp: dict[tuple, set] = {}
        for ev in events:
            k = (ev["shape"], ev["event_class"])
            b = bucket(*k)
            b["event_count"] += 1
            b["trajectories"].add(path)
            b["sub_status"][ev["sub_status"]] += 1
            kp = set(ev["keypaths"])
            b["union_kp"] |= kp
            b["inter_kp"] = kp if b["inter_kp"] is None else (b["inter_kp"] & kp)
            b["text_fields"].add(ev["model_text_field"])
            traj_class_kp.setdefault(k, set()).update(kp)
            # sample (redacted, deterministic)
            skel = _redact_env(ev["_env"])
            rtext = _redact_model_text(ev["shape"], ev["model_text"])
            sample = {"envelope_skeleton": skel,
                      "model_facing_text_redacted": rtext,
                      "sub_status": ev["sub_status"]}
            skey = json.dumps(sample, sort_keys=True, default=str)
            b["samples"].setdefault(skey, sample)
        for k, kpset in traj_class_kp.items():
            bucket(*k)["traj_unions"].add(frozenset(kpset))

    # ---- assemble sorted, deterministic catalog entries ----
    entries = []
    for (shape, cls) in sorted(acc):
        b = acc[(shape, cls)]
        seen_trajs = len(b["trajectories"])
        stable = len(b["traj_unions"]) <= 1
        inter = sorted(b["inter_kp"] or set())
        union = sorted(b["union_kp"])
        fp = hashlib.sha256(("|".join(inter)).encode()).hexdigest()[:16]
        text_fields = sorted(str(t) for t in b["text_fields"])
        samples = [b["samples"][sk] for sk in sorted(b["samples"])][:SAMPLE_CAP]
        total_shape = by_shape[shape] or 0
        frac = round(seen_trajs / total_shape, 8) if total_shape else 0.0
        entries.append({
            "shape": shape,
            "event_class": cls,
            "is_renderer_target": cls in RENDERER_TARGET_CLASSES,
            "status": _status(seen_trajs, stable),
            "model_facing_text_field": (text_fields[0] if len(text_fields) == 1
                                        else text_fields),
            "canonical_text_field": text_field_for(shape, cls),
            "seen_event_count": b["event_count"],
            "seen_in_trajectories": seen_trajs,
            "fraction_of_shape_trajectories": frac,
            "sub_status_counts": dict(sorted(b["sub_status"].items())),
            "required_keypaths": inter,
            "all_keypaths": union,
            "fingerprint_stable_across_trajectories": stable,
            "fingerprint_sha256_16": fp,
            "samples": samples,
        })

    # ---- ABSENCE report: renderer-target classes we CANNOT validate ----
    present = {(e["shape"], e["event_class"]): e for e in entries}
    absence = []
    for shape in ("output_jsonl", "miniswe_trajectory"):
        for cls in EVENT_CLASSES:
            e = present.get((shape, cls))
            if e is None:
                absence.append({"shape": shape, "event_class": cls, "status": "ABSENT",
                                "is_renderer_target": cls in RENDERER_TARGET_CLASSES,
                                "seen_event_count": 0, "seen_in_trajectories": 0})
            elif e["status"] in ("RARE",):
                absence.append({"shape": shape, "event_class": cls, "status": e["status"],
                                "is_renderer_target": e["is_renderer_target"],
                                "seen_event_count": e["seen_event_count"],
                                "seen_in_trajectories": e["seen_in_trajectories"]})
    absence.sort(key=lambda a: (a["shape"], a["event_class"]))

    # sub-status coverage per renderer-target class (what a D1 renderer can/can't assert)
    substatus_coverage = []
    for shape in ("output_jsonl", "miniswe_trajectory"):
        for cls in RENDERER_TARGET_CLASSES + ("submit-finish", "error-observation"):
            e = present.get((shape, cls))
            substatus_coverage.append({
                "shape": shape, "event_class": cls,
                "sub_statuses_seen": (sorted(e["sub_status_counts"]) if e else []),
                "status": (e["status"] if e else "ABSENT"),
            })

    catalog = {
        "schema_version": SCHEMA_VERSION,
        "generated_from": input_dir,
        "corpus": {
            "total_trajectories": len(trajs),
            "by_shape": {"output_jsonl": by_shape["output_jsonl"],
                         "miniswe_trajectory": by_shape["miniswe_trajectory"]},
            "unreadable": unreadable,
        },
        "renderer_target_classes": list(RENDERER_TARGET_CLASSES),
        "event_taxonomy": list(EVENT_CLASSES),
        "event_classes": entries,
        "substatus_coverage": substatus_coverage,
        "absence_report": absence,
    }
    return catalog


# ---------------------------------------------------------------------------
# Human CATALOG.md
# ---------------------------------------------------------------------------
def render_markdown(cat: dict) -> str:
    L = []
    L.append("# GT Observation-Schema Corpus (D0)\n")
    L.append(f"- **schema_version**: `{cat['schema_version']}`")
    L.append(f"- **generated_from**: `{cat['generated_from']}`")
    c = cat["corpus"]
    L.append(f"- **trajectories**: {c['total_trajectories']} "
             f"(OpenHands `output.jsonl`: {c['by_shape']['output_jsonl']}, "
             f"mini-swe `*.trajectory.json`: {c['by_shape']['miniswe_trajectory']}, "
             f"unreadable: {c['unreadable']})")
    L.append(f"- **renderer-target classes**: {', '.join('`%s`' % r for r in cat['renderer_target_classes'])}")
    L.append("")
    L.append("Model-facing TEXT field = the field the policy actually reads. For OpenHands "
             "observations that is `history[].content`; for mini-swe it is the `role=tool` "
             "`messages[].content` (`<returncode>N</returncode><output>…</output>`).\n")
    L.append("> NOTE (OH grammar): the observation the model sees is `content` PLUS the harness "
             "boilerplate `extras.metadata.suffix` (`[The command completed with exit code N.]`, "
             "or `[The command timed out after N seconds…]`). The `suffix`/`prefix` templates are "
             "preserved verbatim in the samples — a D1 renderer must fit that grammar. mini-swe "
             "has NO such suffix; the whole grammar is the `<returncode>`/`<output>` XML wrapper.\n")

    L.append("## Per shape × event class\n")
    L.append("| shape | event class | status | seen (traj / events) | text field | sub-statuses | fp16 |")
    L.append("|---|---|---|---|---|---|---|")
    for e in cat["event_classes"]:
        tf = e["model_facing_text_field"]
        tf = tf if isinstance(tf, str) else ", ".join(tf) if tf else "—"
        subs = ", ".join(f"{k}:{v}" for k, v in e["sub_status_counts"].items()) or "—"
        star = " ⚑" if e["is_renderer_target"] else ""
        L.append(f"| `{e['shape']}` | `{e['event_class']}`{star} | **{e['status']}** | "
                 f"{e['seen_in_trajectories']} / {e['seen_event_count']} | `{tf}` | {subs} | `{e['fingerprint_sha256_16']}` |")
    L.append("\n(⚑ = renderer-target class — where GT would inject native facts.)\n")

    L.append("## Sub-status coverage (what a D1 renderer contract can assert)\n")
    L.append("| shape | event class | status | sub-statuses captured |")
    L.append("|---|---|---|---|")
    for s in cat["substatus_coverage"]:
        L.append(f"| `{s['shape']}` | `{s['event_class']}` | {s['status']} | "
                 f"{', '.join(s['sub_statuses_seen']) or '—'} |")
    L.append("")

    L.append("## Absence / RARE report (UNVALIDATED renderer surfaces)\n")
    if not cat["absence_report"]:
        L.append("_None — every taxonomy class was observed in ≥2 trajectories per shape._\n")
    else:
        L.append("These (shape × class) pairs are ABSENT or single-witness RARE in the corpus. "
                 "A renderer for them is **UNVALIDATED** against real observations — D1 cannot "
                 "assert a byte-contract for these yet.\n")
        L.append("| shape | event class | status | renderer-target? | seen (traj/events) |")
        L.append("|---|---|---|---|---|")
        for a in cat["absence_report"]:
            L.append(f"| `{a['shape']}` | `{a['event_class']}` | **{a['status']}** | "
                     f"{'YES' if a['is_renderer_target'] else 'no'} | "
                     f"{a['seen_in_trajectories']}/{a['seen_event_count']} |")
        L.append("")
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--in", dest="input_dir", default="D:/gt_runs",
                    help="root dir to scan for trajectories (default D:/gt_runs)")
    ap.add_argument("--out-dir", help="write observation_schema_catalog.json + CATALOG.md here")
    args = ap.parse_args()

    cat = build_catalog(args.input_dir)
    payload = json.dumps(cat, indent=2, sort_keys=True, default=str)
    if args.out_dir:
        os.makedirs(args.out_dir, exist_ok=True)
        jpath = os.path.join(args.out_dir, "observation_schema_catalog.json")
        mpath = os.path.join(args.out_dir, "CATALOG.md")
        with open(jpath, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(payload)
        with open(mpath, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(render_markdown(cat))
        print(f"[WROTE] {jpath}")
        print(f"[WROTE] {mpath}")
    else:
        print(payload)


if __name__ == "__main__":
    main()
