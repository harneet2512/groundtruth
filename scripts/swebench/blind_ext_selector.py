#!/usr/bin/env python3
"""blind_ext_selector — deterministic, leak-safe blind extension selection (Cluster-5 ITEM 4).

Selects an EXTENSION task set for the SS-LIVE campaign from BLIND fields only — instance_id, repo,
base_commit, problem_statement — and NEVER the gold/test fields (patch, test_patch, FAIL_TO_PASS,
PASS_TO_PASS, test_cmds, log_parser, difficulty, hints_text). Leak-safety is enforced structurally:
:func:`project_blind` builds each record from an ALLOW-LIST of the four blind keys, so a forbidden
field is never even read out of the dataset.

The selection is a PURE FUNCTION of (dataset bytes, locked-30 bytes, VERSION):

  * the dataset + locked-30 bytes are bound by sha256 to the exact frozen inputs the vFINAL
    selection was computed from (``benchmarks/data/blind_ext_selection_vfinal_20260718.json``);
    ANY hash mismatch REFUSES to run (fail-closed) — the selection is meaningless on drifted bytes;
  * for the pinned VERSION the frozen 7 ids (3 signature + 4 new-file) + the derivation pools are
    the committed authority, reproduced bit-for-bit. The predicate derivation that PRODUCED them is
    specified in BLIND_EXT_SELECTOR_SPEC.md (P_SIG / P_NF + the ``(tier, repo_in_locked, sha256(
    VERSION|instance_id))`` ordering); its OUTPUT is frozen here so reproduction is exact and never
    drifts with a prose re-interpretation.

The selector additionally VALIDATES the frozen selection against the blind projection — every
selected id must exist as a real record reachable from the blind fields alone — so the frozen answer
can never reference a phantom or a record that needed a gold field to identify.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from typing import Any, Mapping

VERSION = "blind-ext-selector-vFINAL-20260718"

# The four BLIND fields — the ONLY keys the selector is permitted to read from a dataset record.
BLIND_FIELDS: tuple[str, ...] = ("instance_id", "repo", "base_commit", "problem_statement")

# Fields that would LEAK the answer — asserted absent from every projected record.
FORBIDDEN_FIELDS: frozenset[str] = frozenset({
    "patch", "test_patch", "FAIL_TO_PASS", "PASS_TO_PASS", "test_cmds",
    "log_parser", "difficulty", "hints_text", "all_hints_text",
})

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_HERE))
_FROZEN_PATH = os.path.join(
    _REPO_ROOT, "benchmarks", "data", "blind_ext_selection_vfinal_20260718.json"
)


class SelectorRefusal(RuntimeError):
    """Raised (fail-closed) when the inputs do not reproduce the frozen selection's bytes."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_frozen() -> dict[str, Any]:
    """The committed vFINAL frozen selection (VERSION, expected input hashes, pools, 7 ids)."""
    with open(_FROZEN_PATH, encoding="utf-8") as fh:
        frozen = json.load(fh)
    if frozen.get("version") != VERSION:
        raise SelectorRefusal(
            f"frozen selection version {frozen.get('version')!r} != selector VERSION {VERSION!r}"
        )
    return frozen


def project_blind(dataset_bytes: bytes) -> list[dict[str, str]]:
    """Project the dataset to BLIND fields only (allow-list). Each record is rebuilt from
    :data:`BLIND_FIELDS`; forbidden gold/test fields are NEVER read. Deterministic, ordered."""
    out: list[dict[str, str]] = []
    for line in dataset_bytes.decode("utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        if not isinstance(rec, dict):
            raise SelectorRefusal("dataset record is not a JSON object")
        projected = {k: rec.get(k, "") for k in BLIND_FIELDS}
        # Structural leak guard: the projection carries ONLY blind keys.
        leaked = FORBIDDEN_FIELDS & set(projected)
        if leaked:
            raise SelectorRefusal(f"blind projection leaked forbidden fields: {sorted(leaked)}")
        out.append(projected)
    return out


def select(
    dataset_bytes: bytes, locked30_bytes: bytes, *, version: str = VERSION,
) -> dict[str, Any]:
    """Reproduce the frozen blind extension selection. PURE + fail-closed.

    Refuses (``SelectorRefusal``) when: the version is unknown, either input's sha256 does not
    match the frozen expected hash, or a selected id is not present in the blind projection.
    """
    if version != VERSION:
        raise SelectorRefusal(f"unknown selector version {version!r} (pinned: {VERSION!r})")
    frozen = load_frozen()

    got_dataset = _sha256(dataset_bytes)
    got_locked = _sha256(locked30_bytes)
    if got_dataset != frozen["dataset_sha256"]:
        raise SelectorRefusal(
            f"dataset sha256 mismatch: got {got_dataset} != frozen {frozen['dataset_sha256']}"
        )
    if got_locked != frozen["locked30_sha256"]:
        raise SelectorRefusal(
            f"locked-30 sha256 mismatch: got {got_locked} != frozen {frozen['locked30_sha256']}"
        )

    projected = project_blind(dataset_bytes)
    blind_ids = {r["instance_id"] for r in projected}
    selected = list(frozen["selected_signature"]) + list(frozen["selected_newfile"])
    missing = [i for i in selected if i not in blind_ids]
    if missing:
        raise SelectorRefusal(
            f"frozen selection references ids absent from the blind projection: {missing}"
        )
    return {
        "version": VERSION,
        "dataset_sha256": got_dataset,
        "locked30_sha256": got_locked,
        "selected_signature": list(frozen["selected_signature"]),
        "selected_newfile": list(frozen["selected_newfile"]),
        "selected": selected,
    }


def _locked_ids(locked30_bytes: bytes) -> list[str]:
    """The 30 locked task ids (comment/blank lines stripped), in file order."""
    ids: list[str] = []
    for line in locked30_bytes.decode("utf-8").splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            ids.append(s)
    return ids


def build_campaign_manifest(dataset_bytes: bytes, locked30_bytes: bytes) -> dict[str, Any]:
    """The expanded-campaign manifest for the SS-LIVE run: locked-30 ∪ selected-7.

    Deterministic; carries the blind ``repo``/``base_commit`` for each expanded id (from the blind
    projection only) and the input hashes for provenance. Fail-closed via :func:`select`.
    """
    sel = select(dataset_bytes, locked30_bytes)
    locked = _locked_ids(locked30_bytes)
    blind_by_id = {r["instance_id"]: r for r in project_blind(dataset_bytes)}
    # Expanded set = locked-30 then the selected-7 not already locked (order-stable, de-duplicated).
    expanded: list[str] = list(locked)
    for i in sel["selected"]:
        if i not in expanded:
            expanded.append(i)
    rows = []
    for i in expanded:
        rec = blind_by_id.get(i, {})
        rows.append({
            "instance_id": i,
            "repo": rec.get("repo", ""),
            "base_commit": rec.get("base_commit", ""),
            "bucket": "locked30" if i in set(locked) else "blind_ext_selected",
        })
    return {
        "schema": "gt.blind_ext_campaign.v1",
        "version": VERSION,
        "dataset_sha256": sel["dataset_sha256"],
        "locked30_sha256": sel["locked30_sha256"],
        "counts": {
            "locked30": len(locked),
            "selected": len(sel["selected"]),
            "expanded": len(expanded),
        },
        "selected_signature": sel["selected_signature"],
        "selected_newfile": sel["selected_newfile"],
        "campaign": rows,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--dataset",
        default=os.path.join(_REPO_ROOT, "benchmarks", "data", "swebench_live_lite.jsonl"),
    )
    ap.add_argument(
        "--locked30",
        default=os.path.join(_REPO_ROOT, ".claude", "reports", "mixture_bag_30_20260712.txt"),
    )
    ap.add_argument("--output", default=None, help="campaign manifest output (default: stdout)")
    ap.add_argument("--selection-only", action="store_true", help="emit the 7-id selection, not the manifest")
    args = ap.parse_args(argv)
    try:
        dataset_bytes = open(args.dataset, "rb").read()
        locked_bytes = open(args.locked30, "rb").read()
    except OSError as exc:
        print(f"blind_ext_selector: input unreadable: {exc}", file=sys.stderr)
        return 2
    try:
        doc: Mapping[str, Any] = (
            select(dataset_bytes, locked_bytes) if args.selection_only
            else build_campaign_manifest(dataset_bytes, locked_bytes)
        )
    except SelectorRefusal as exc:
        print(f"blind_ext_selector: REFUSED (fail-closed): {exc}", file=sys.stderr)
        return 3
    text = json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"blind_ext_selector: wrote {args.output}")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
