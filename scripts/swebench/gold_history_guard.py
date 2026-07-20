"""D-R (CRITICAL): SWE-bench-Live task containers retain upstream git history —
including the GOLD fix commit for the task's own issue — so an agent can
`git log` -> read the gold sha -> `git show <sha>` / `git checkout <sha> -- <file>`
and transplant the fix. This corrupts the resolve denominator across ALL arms
(keras-20443 run6 was CONFIRMED USED: msg32 `git log` -> 0adc924e1
'Fix encoding issue (#20443)' -> msg35 `git show` -> checkout the gold file).

The primary fix is a bake-time git strip (remove refs/reflog beyond base +
gc --prune). This module is the DETECTOR half: an anchored recognizer over the
agent's own commands that stamps ``gold_history_access`` so the two-layer audit
can EXCLUDE those task-attempts from resolve credit and report an honest
denominator. Conservative / correct-or-quiet: it flags only strong gold-history
USE signals (accessing a specific commit sha, or enumerating refs BEYOND HEAD) —
never legit working-tree git (`git stash`, `git diff`, `git status`, a plain
`git log`, `git checkout <branch|-- file>`).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# a commit sha the agent could only have learned from the leaked history
_SHA = r"[0-9a-f]{7,40}"

# Each pattern names a distinct gold-history-USE signature. Anchored on ``git``
# and the specific subcommand; the sha alternatives require a real hash token so
# a branch/file argument (``git checkout main`` / ``git checkout -- f.py``) never
# trips them. Pipes/redirects bound the ``--all`` scans so a later unrelated
# command on the same line is not swept in.
_PATTERNS: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    # accessing a SPECIFIC commit (the gold fix) by sha
    ("git_show_sha", re.compile(rf"\bgit\s+show\b(?:\s+-\S+)*\s+(?:[^\s|;&]+\s+)*?({_SHA})\b")),
    ("git_checkout_sha", re.compile(rf"\bgit\s+checkout\b(?:\s+-\S+)*\s+({_SHA})\b")),
    ("git_apply_sha", re.compile(
        rf"\bgit\s+(?:diff|cherry-pick|revert|merge|rebase|restore)\b(?:\s+-\S+)*\s+(?:[^\s|;&]+\s+)*?({_SHA})\b")),
    # enumerating history / refs BEYOND HEAD (the recon that reveals the gold sha)
    ("git_log_all", re.compile(r"\bgit\s+log\b[^|;&\n]*?\s--all\b")),
    ("git_log_remote", re.compile(r"\bgit\s+log\b[^|;&\n]*?\borigin/\S")),
    ("git_ref_enum", re.compile(
        r"\bgit\s+(?:branch|show-ref|for-each-ref|rev-list)\b[^|;&\n]*?(?:\s-a\b|\s--all\b|\s--remotes\b|\sorigin)")),
)


@dataclass(frozen=True)
class GoldHistoryFinding:
    signature: str          # which pattern fired (git_show_sha, git_log_all, ...)
    command: str            # the offending command, verbatim
    sha: str = ""           # the accessed commit sha, when one was captured


@dataclass
class GoldHistoryResult:
    accessed: bool = False
    findings: list[GoldHistoryFinding] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "gold_history_access": self.accessed,
            "findings": [
                {"signature": f.signature, "command": f.command, "sha": f.sha}
                for f in self.findings
            ],
        }


def detect_gold_history_access(commands: "list[str] | tuple[str, ...]") -> GoldHistoryResult:
    """Scan the agent's OWN commands for gold-history USE. ``commands`` is the
    ordered list of executed shell commands (one per action). Returns a result
    whose ``accessed`` is True iff any strong signature fired; ``findings`` names
    each. Pure/deterministic; no I/O."""
    res = GoldHistoryResult()
    for cmd in commands or ():
        if not cmd or "git" not in cmd:
            continue
        for sig, rx in _PATTERNS:
            m = rx.search(cmd)
            if m is not None:
                # some patterns (the ref-enum scans) have no capture group
                sha = m.group(1) if m.re.groups >= 1 else ""
                res.findings.append(GoldHistoryFinding(sig, cmd.strip(), sha))
    res.accessed = bool(res.findings)
    return res
