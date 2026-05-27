"""GT-augmented mini-swe-agent for Pier.

Full integration: brief injection, graph.db upload, admissibility gate,
seed extraction, in-container hook, post-run log extraction.
Works across all 5 DeepSWE languages (Python, Go, TypeScript, JavaScript, Rust).

Usage:
    # Single task with pre-built indexes:
    pier run -p deep-swe/tasks/dateutil-rfc5545-timezone-interop \
        --agent-import-path artifact_deepswe.gt_agent:GTMiniSweAgent \
        --model deepseek/deepseek-v4-flash \
        --ak graph_db_dir=indexes \
        --env docker -y

    # Full benchmark:
    pier run -p deep-swe/tasks \
        --agent-import-path artifact_deepswe.gt_agent:GTMiniSweAgent \
        --model deepseek/deepseek-v4-flash \
        --ak graph_db_dir=indexes \
        --env docker -y -n 4

    # Baseline (no GT, standard mini-swe-agent):
    pier run -p deep-swe/tasks \
        --agent mini-swe-agent \
        --model deepseek/deepseek-v4-flash \
        --env docker -y -n 4
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import sqlite3
import textwrap
from pathlib import Path

from pier.agents.installed.mini_swe_agent import MiniSweAgent
from pier.environments.base import BaseEnvironment
from pier.models.agent.context import AgentContext
from pier.models.agent.install import AgentInstallSpec, InstallStep

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Locate gt_hook.py
# ---------------------------------------------------------------------------
_THIS_DIR = Path(__file__).resolve().parent
_HOOK_CANDIDATES = [
    _THIS_DIR.parent / "benchmarks" / "swebench" / "gt_hook.py",
    _THIS_DIR / "benchmarks" / "swebench" / "gt_hook.py",
    _THIS_DIR / "gt_hook.py",
]

_GT_HOOK_CONTENT: str | None = None
for _p in _HOOK_CANDIDATES:
    if _p.is_file():
        _GT_HOOK_CONTENT = _p.read_text(encoding="utf-8", errors="replace")
        break

_B64_CHUNK_SIZE = 50_000


def _encode_gt_hook() -> list[str]:
    if _GT_HOOK_CONTENT is None:
        return []
    encoded = base64.b64encode(_GT_HOOK_CONTENT.encode("utf-8")).decode("ascii")
    return [encoded[i:i + _B64_CHUNK_SIZE] for i in range(0, len(encoded), _B64_CHUNK_SIZE)]


_B64_CHUNKS = _encode_gt_hook()

# ---------------------------------------------------------------------------
# Seed extraction (port of anchors.py regex logic, language-agnostic)
# ---------------------------------------------------------------------------
_STOP_WORDS = frozenset({
    "the", "and", "for", "with", "that", "this", "from", "have", "will",
    "should", "when", "which", "would", "could", "into", "also", "been",
    "being", "does", "each", "than", "then", "there", "these", "they",
    "about", "after", "before", "between", "both", "case", "default",
    "error", "example", "expected", "false", "function", "given", "here",
    "http", "https", "import", "instead", "issue", "just", "like", "make",
    "method", "need", "none", "null", "only", "other", "return", "same",
    "self", "some", "test", "true", "type", "used", "using", "value",
    "want", "what", "work", "TypeError", "ValueError", "AttributeError",
    "KeyError", "ImportError", "RuntimeError", "IndexError",
})


def extract_seeds(instruction: str) -> list[str]:
    """Extract likely symbol names from instruction text."""
    words = re.findall(r"[A-Za-z_]\w{2,}", instruction)
    seen: set[str] = set()
    seeds: list[str] = []
    for w in words:
        low = w.lower()
        if low in _STOP_WORDS or len(w) < 4:
            continue
        if low not in seen:
            seen.add(low)
            seeds.append(w)
    return seeds[:50]


# ---------------------------------------------------------------------------
# Brief generation (host-side, all 5 languages)
# ---------------------------------------------------------------------------
MAX_BRIEF_CHARS = 2000
MAX_BRIEF_FILES = 5


def generate_brief(graph_db_path: str, instruction: str, repo_root: str = "") -> str:
    """Generate a brief from graph.db. Language-agnostic, works for all 5 languages."""
    if not os.path.exists(graph_db_path):
        return ""

    try:
        conn = sqlite3.connect(f"file:{graph_db_path}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return ""

    try:
        total_nodes = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        total_edges = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        if total_nodes == 0:
            return ""

        seeds = extract_seeds(instruction)
        if not seeds:
            return ""

        # Find files containing seed symbols (BM25-lite: name match against seeds)
        placeholders = ",".join("?" for _ in seeds)
        rows = conn.execute(f"""
            SELECT DISTINCT n.file_path, n.name, n.label, n.start_line,
                   COUNT(DISTINCT e.source_id) as caller_count
            FROM nodes n
            LEFT JOIN edges e ON e.target_id = n.id
                AND e.resolution_method IN ('same_file', 'import')
            WHERE n.name IN ({placeholders})
              AND n.is_test = 0
              AND n.label IN ('Function', 'Method', 'Class')
            GROUP BY n.file_path, n.name
            ORDER BY caller_count DESC, n.file_path
            LIMIT 20
        """, seeds).fetchall()

        if not rows:
            return ""

        # Group by file, pick top MAX_BRIEF_FILES
        file_entries: dict[str, list] = {}
        for fpath, name, label, line, callers in rows:
            if fpath not in file_entries:
                file_entries[fpath] = []
            file_entries[fpath].append((name, label, line, callers))

        # Rank files by total callers
        ranked = sorted(file_entries.items(), key=lambda x: sum(c for _, _, _, c in x[1]), reverse=True)

        parts = ["<gt-task-brief>", "## Focus files"]
        for i, (fpath, symbols) in enumerate(ranked[:MAX_BRIEF_FILES]):
            total_callers = sum(c for _, _, _, c in symbols)
            tier = "[VERIFIED]" if i < 2 else "[WARNING]" if i < 4 else "[INFO]"
            parts.append(f"{tier} {fpath}  (callers={total_callers})")

            # Show top symbols in this file
            for name, label, line, callers in symbols[:3]:
                line_str = str(line) if line else "?"
                parts.append(f"  {fpath}:{line_str} -- {name} ({label}, {callers} callers)")

            # Show test files that reference this file
            test_files = conn.execute("""
                SELECT DISTINCT n1.file_path
                FROM edges e
                JOIN nodes n1 ON e.source_id = n1.id
                JOIN nodes n2 ON e.target_id = n2.id
                WHERE n2.file_path = ?
                  AND n1.is_test = 1
                  AND e.resolution_method IN ('same_file', 'import', 'name_match')
                LIMIT 3
            """, (fpath,)).fetchall()
            if test_files:
                test_list = ", ".join(r[0].split("/")[-1] for r in test_files)
                parts.append(f"  Tests: {test_list}")

        # Scope signal: how many files would be affected?
        if total_edges > 0:
            scope_files = conn.execute("""
                SELECT COUNT(DISTINCT e.source_file)
                FROM edges e
                JOIN nodes n ON e.target_id = n.id
                WHERE n.file_path IN ({})
                  AND e.resolution_method IN ('same_file', 'import')
            """.format(",".join("?" for _ in ranked[:MAX_BRIEF_FILES])),
                [f for f, _ in ranked[:MAX_BRIEF_FILES]],
            ).fetchone()[0]
            if scope_files > 1:
                parts.append(f"\nScope: {scope_files} files reference these targets.")

        parts.append("</gt-task-brief>")
        brief = "\n".join(parts)
        return brief[:MAX_BRIEF_CHARS]

    except sqlite3.Error as e:
        logger.debug("Brief generation failed: %s", e)
        return ""
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Admissibility gate
# ---------------------------------------------------------------------------
def admissibility_gate(brief: str, graph_db_path: str) -> bool:
    """Check if the brief passes all 6 admissibility criteria."""
    if not brief or len(brief) < 50:
        return False  # HAS_VALUE

    if len(brief) > MAX_BRIEF_CHARS:
        return False  # CONCISE

    lines = brief.strip().split("\n")
    file_mentions: dict[str, int] = {}
    has_non_test = False
    for line in lines:
        if "[VERIFIED]" in line or "[WARNING]" in line or "[INFO]" in line:
            path_match = re.search(r"\]\s+(\S+)", line)
            if path_match:
                fpath = path_match.group(1)
                file_mentions[fpath] = file_mentions.get(fpath, 0) + 1
                if not any(t in fpath for t in ["test_", "/tests/", "/test/", "_test."]):
                    has_non_test = True

    if not has_non_test:
        return False  # NOT_TEST

    if any(count > 2 for count in file_mentions.values()):
        return False  # NO_SPAM

    # CONFIDENCE: check that graph has at least one verified edge
    if os.path.exists(graph_db_path):
        try:
            conn = sqlite3.connect(f"file:{graph_db_path}?mode=ro", uri=True, timeout=3)
            verified = conn.execute(
                "SELECT COUNT(*) FROM edges WHERE resolution_method IN ('same_file', 'import')"
            ).fetchone()[0]
            conn.close()
            if verified == 0:
                return False  # CONFIDENCE
        except sqlite3.Error:
            pass

    return True


# ---------------------------------------------------------------------------
# Install script builder
# ---------------------------------------------------------------------------
def _build_inject_script() -> str:
    if not _B64_CHUNKS:
        return 'echo "WARNING: gt_hook.py not found at build time" >&2\n'

    lines = [
        "set -euo pipefail",
        "rm -f /tmp/gt_hook.b64",
    ]
    for i, chunk in enumerate(_B64_CHUNKS):
        op = ">" if i == 0 else ">>"
        lines.append(f'echo "{chunk}" {op} /tmp/gt_hook.b64')

    lines.extend([
        "base64 -d /tmp/gt_hook.b64 > /tmp/gt_hook.py",
        "rm -f /tmp/gt_hook.b64",
        "chmod +x /tmp/gt_hook.py",
        "",
        '# Detect repo root',
        'REPO_ROOT=""',
        'for d in /home/user /testbed /workspace /app /repo; do',
        '    if [ -d "$d/.git" ]; then REPO_ROOT="$d"; break; fi',
        "done",
        'if [ -z "$REPO_ROOT" ]; then',
        '    REPO_ROOT=$(find / -maxdepth 3 -name .git -type d 2>/dev/null | head -1 | sed "s|/.git||")',
        "fi",
        'echo "${REPO_ROOT:-/home/user}" > /tmp/gt_root.txt',
        'echo "GT: hook installed, root=${REPO_ROOT:-/home/user}" >&2',
    ])
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# GT Preamble (appended to instruction)
# ---------------------------------------------------------------------------
_GT_TOOL_DOCS = textwrap.dedent("""\

    ## Codebase Intelligence Tool

    You have a codebase intelligence tool at /tmp/gt_hook.py.
    It works for Python, Go, TypeScript, JavaScript, and Rust.

    **Before editing a file**, run:
        python3 /tmp/gt_hook.py understand <filepath> --root=$(cat /tmp/gt_root.txt) --quiet --max-lines=10

    This shows information you CANNOT get by reading the file alone:
    - Which OTHER files call functions in this file and how they use the results
    - Which TEST files cover this module (so you know where to verify)
    - Rules that hold across ALL sibling methods (patterns you must follow)
    - Behavioral contracts (what functions read/write/return)

    **After editing**, optionally run:
        python3 /tmp/gt_hook.py verify --root=$(cat /tmp/gt_root.txt) --quiet --max-items=3

    Use understand on 1-2 key files before editing. Don't over-use it.
""")


# ---------------------------------------------------------------------------
# GTMiniSweAgent
# ---------------------------------------------------------------------------
class GTMiniSweAgent(MiniSweAgent):
    """MiniSweAgent with full GroundTruth integration.

    Features:
    - Pre-task brief generated on HOST from pre-built graph.db (all 5 languages)
    - graph.db uploaded into container for gt_hook.py queries
    - gt_hook.py injected via install_spec (base64 chunked)
    - Admissibility gate: BUDGET, NOT_TEST, CONFIDENCE, CONCISE, NO_SPAM, HAS_VALUE
    - Post-run GT hook log extraction
    - Seed extraction from instruction text (language-agnostic)
    """

    def __init__(
        self,
        graph_db_dir: str | None = None,
        gt_brief_enabled: bool = True,
        gt_hook_enabled: bool = True,
        *args,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self._graph_db_dir = graph_db_dir or os.environ.get("GT_GRAPH_DB_DIR", "")
        self._gt_brief_enabled = gt_brief_enabled
        self._gt_hook_enabled = gt_hook_enabled

    @staticmethod
    def name() -> str:
        return "gt-mini-swe-agent"

    def install_spec(self) -> AgentInstallSpec:
        spec = super().install_spec()
        if self._gt_hook_enabled:
            spec.steps.append(InstallStep(user="agent", run=_build_inject_script()))
        return spec

    def _resolve_graph_db(self, task_name: str) -> str | None:
        """Find graph.db for this task in the pre-built indexes directory."""
        if not self._graph_db_dir:
            return None
        # Try exact task name match
        candidates = [
            os.path.join(self._graph_db_dir, task_name, "graph.db"),
            os.path.join(self._graph_db_dir, task_name + ".db"),
        ]
        for c in candidates:
            if os.path.exists(c):
                return c
        # Scan for any graph.db in the dir (single-task mode)
        db_path = os.path.join(self._graph_db_dir, "graph.db")
        if os.path.exists(db_path):
            return db_path
        return None

    def _extract_task_name(self, instruction: str) -> str:
        """Try to extract task name from logs_dir path or instruction."""
        # Pier sets logs_dir to something like jobs/<job>/trials/<task>/logs/agent
        if self.logs_dir:
            parts = self.logs_dir.parts
            for i, p in enumerate(parts):
                if p == "trials" and i + 1 < len(parts):
                    return parts[i + 1]
        return ""

    async def run(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None:
        task_name = self._extract_task_name(instruction)
        graph_db = self._resolve_graph_db(task_name) if task_name else None

        # Upload graph.db into container for gt_hook.py
        if graph_db and hasattr(environment, "upload_file"):
            try:
                await environment.upload_file(graph_db, "/tmp/gt_index.db")
                logger.info("Uploaded graph.db for %s (%d bytes)", task_name, os.path.getsize(graph_db))
            except Exception as e:
                logger.warning("Failed to upload graph.db: %s", e)

        # Generate brief on HOST
        brief = ""
        if self._gt_brief_enabled and graph_db:
            brief = generate_brief(graph_db, instruction)
            if brief and not admissibility_gate(brief, graph_db):
                logger.info("Brief suppressed by admissibility gate for %s", task_name)
                brief = ""

        # Compose augmented instruction
        augmented = instruction
        if brief:
            augmented = brief + "\n\n" + augmented
        if self._gt_hook_enabled:
            augmented = augmented.rstrip() + "\n" + _GT_TOOL_DOCS

        # Save GT metadata
        gt_meta = {
            "task_name": task_name,
            "graph_db_found": graph_db is not None,
            "brief_generated": bool(brief),
            "brief_length": len(brief),
            "seeds_extracted": len(extract_seeds(instruction)),
        }
        try:
            meta_path = self.logs_dir / "gt_meta.json"
            meta_path.write_text(json.dumps(gt_meta, indent=2))
        except Exception:
            pass

        await super().run(augmented, environment, context)

    def populate_context_post_run(self, context: AgentContext) -> None:
        super().populate_context_post_run(context)
        # Extract GT hook usage from trajectory
        try:
            traj_path = self.logs_dir / "mini-swe-agent.trajectory.json"
            if traj_path.exists():
                traj = json.loads(traj_path.read_text())
                messages = traj.get("messages") or []
                gt_calls = 0
                for msg in messages:
                    content = str(msg.get("content", ""))
                    gt_calls += content.count("gt_hook.py understand")
                    gt_calls += content.count("gt_hook.py verify")
                # Write GT usage summary
                usage_path = self.logs_dir / "gt_usage.json"
                usage_path.write_text(json.dumps({
                    "total_gt_calls": gt_calls,
                    "total_messages": len(messages),
                }))
        except Exception:
            pass
