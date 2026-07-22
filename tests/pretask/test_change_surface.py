"""W-A — ABSENCE / ARCHITECTURAL-HOLE engine (change_surface.py).

Deterministic detection of MISSING change surfaces for new-file / feature-add
issues, mined from the repo's OWN sibling-role conventions. No hardcoded
framework knowledge, no LLM, no network — stdlib + sqlite3 only.

Fixtures synthesize small trees under tmp_path covering Python, Go, and
TypeScript shapes, plus >=2 abstention cases (single sibling; no registry) and a
conflicting-conventions case. No task IDs, no gold labels, no benchmark tests.

RED-before-GREEN: these are written against the intended public API before the
engine exists; running them before the implementation lands must error/fail.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from groundtruth.pretask.change_surface import (
    ROLE_CONFIG,
    ROLE_IMPLEMENTATION,
    ROLE_REGISTRATION,
    ROLE_TEST,
    TRUST_HYPOTHESIS,
    ChangeSurfaceResult,
    MissingRole,
    detect_change_surface,
)
from groundtruth.pretask.change_surface import _Group, _entity_links_group

# ---------------------------------------------------------------------------
# fixture builders
# ---------------------------------------------------------------------------


def _w(root: Path, rel: str, content: str = "") -> None:
    """Write a file under ``root`` (creating parent dirs)."""
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def _mk_graph(db_path: Path, defs: list[tuple[str, str, str, int]]) -> str:
    """defs = [(name, label, file_path, is_test), ...] -> graph.db path str."""
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE nodes (id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT, "
        "name TEXT, qualified_name TEXT, file_path TEXT, start_line INTEGER, "
        "end_line INTEGER, signature TEXT, return_type TEXT, is_exported INTEGER, "
        "is_test INTEGER DEFAULT 0, language TEXT DEFAULT 'python', "
        "parent_id INTEGER)"
    )
    for name, label, fpath, is_test in defs:
        conn.execute(
            "INSERT INTO nodes (label, name, file_path, is_test) VALUES (?,?,?,?)",
            (label, name, fpath, is_test),
        )
    conn.commit()
    conn.close()
    return str(db_path)


def _python_provider_repo(tmp_path: Path) -> tuple[str, str]:
    """A 4-role Python provider family: impl + config + registry + tests, with
    azure entirely absent. Returns (repo_root, graph_db)."""
    _w(tmp_path, "providers/__init__.py",
       "from .aws import AwsProvider\n"
       "from .gcp import GcpProvider\n\n"
       "REGISTRY = {'aws': AwsProvider, 'gcp': GcpProvider}\n")
    _w(tmp_path, "providers/aws.py",
       "class AwsProvider:\n    def connect(self):\n        return 'aws'\n")
    _w(tmp_path, "providers/gcp.py",
       "class GcpProvider:\n    def connect(self):\n        return 'gcp'\n")
    _w(tmp_path, "config/aws_config.py", "AWS_CONFIG = {'region': 'us-east-1'}\n")
    _w(tmp_path, "config/gcp_config.py", "GCP_CONFIG = {'zone': 'us-central1'}\n")
    _w(tmp_path, "tests/test_aws.py",
       "def test_aws():\n    assert AwsProvider().connect() == 'aws'\n")
    _w(tmp_path, "tests/test_gcp.py",
       "def test_gcp():\n    assert GcpProvider().connect() == 'gcp'\n")
    db = _mk_graph(
        tmp_path / "graph.db",
        [
            ("AwsProvider", "Class", "providers/aws.py", 0),
            ("connect", "Method", "providers/aws.py", 0),
            ("GcpProvider", "Class", "providers/gcp.py", 0),
            ("connect", "Method", "providers/gcp.py", 0),
            ("test_aws", "Function", "tests/test_aws.py", 1),
            ("test_gcp", "Function", "tests/test_gcp.py", 1),
        ],
    )
    return str(tmp_path), db


_PY_ISSUE = (
    "Add azure provider support\n\n"
    "We need an azure provider analogous to the existing aws and gcp providers.\n"
)


def _roles_for(result: ChangeSurfaceResult, entity: str) -> set[str]:
    return {r.role for r in result.missing_roles if r.entity == entity}


def _mr(result: ChangeSurfaceResult, entity: str, role: str) -> MissingRole:
    for r in result.missing_roles:
        if r.entity == entity and r.role == role:
            return r
    raise AssertionError(f"no MissingRole({role}) for {entity}: {result.missing_roles}")


# ---------------------------------------------------------------------------
# flag gating
# ---------------------------------------------------------------------------


def test_flag_off_returns_empty_immediately(tmp_path, monkeypatch):
    monkeypatch.delenv("GT_CHANGE_SURFACE", raising=False)
    root, db = _python_provider_repo(tmp_path)
    r = detect_change_surface(_PY_ISSUE, root, db)
    assert r.abstained is True
    assert r.abstain_reason == "flag_disabled"
    assert r.missing_roles == []
    assert r.destinations == []


def test_flag_zero_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("GT_CHANGE_SURFACE", "0")
    root, db = _python_provider_repo(tmp_path)
    r = detect_change_surface(_PY_ISSUE, root, db)
    assert r.abstained is True and r.missing_roles == []


# ---------------------------------------------------------------------------
# Python — full 4-role hole
# ---------------------------------------------------------------------------


def test_python_new_provider_all_roles_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("GT_CHANGE_SURFACE", "1")
    root, db = _python_provider_repo(tmp_path)
    r = detect_change_surface(_PY_ISSUE, root, db)

    assert r.abstained is False, r.abstain_reason
    # correct-or-quiet: ONLY the real new-capability token, no prose words
    # ("analogous"/"existing" sit near "provider" but are not entities).
    assert r.entities == ["azure"], r.entities
    roles = _roles_for(r, "azure")
    assert roles == {ROLE_IMPLEMENTATION, ROLE_CONFIG, ROLE_REGISTRATION, ROLE_TEST}, roles

    # every emitted fact is HYPOTHESIS tier, never CERTIFIED
    for mr in r.missing_roles:
        assert mr.trust_tier == TRUST_HYPOTHESIS
        assert len(mr.signals) >= 2, ("2-signal law", mr.role, mr.signals)
        assert mr.evidence, ("every fact carries evidence", mr.role)


def test_python_registration_points_at_registry_with_ref_lines(tmp_path, monkeypatch):
    monkeypatch.setenv("GT_CHANGE_SURFACE", "1")
    root, db = _python_provider_repo(tmp_path)
    r = detect_change_surface(_PY_ISSUE, root, db)
    reg = _mr(r, "azure", ROLE_REGISTRATION)
    assert reg.registration_file is not None
    assert reg.registration_file.replace("\\", "/").endswith("providers/__init__.py")
    # the evidence must quote the lines that register the SIBLINGS (aws/gcp)
    reffed = " ".join(t for _, t in reg.registration_lines).lower()
    assert "aws" in reffed and "gcp" in reffed
    assert "azure" not in reffed  # azure is precisely what's absent


def test_python_destination_created_for_impl_hole(tmp_path, monkeypatch):
    monkeypatch.setenv("GT_CHANGE_SURFACE", "1")
    root, db = _python_provider_repo(tmp_path)
    r = detect_change_surface(_PY_ISSUE, root, db)
    dests = [d for d in r.destinations if d.entity == "azure"]
    assert len(dests) == 1, r.destinations
    d = dests[0]
    assert d.suggested_path.replace("\\", "/") == "providers/azure.py"
    assert d.directory.replace("\\", "/") == "providers"
    assert d.template_file.replace("\\", "/") in (
        "providers/aws.py", "providers/gcp.py")
    assert d.registration_file is not None
    assert d.registration_file.replace("\\", "/").endswith("providers/__init__.py")
    assert d.trust_tier == TRUST_HYPOTHESIS


def test_leak_law_test_role_emits_paths_not_bodies(tmp_path, monkeypatch):
    monkeypatch.setenv("GT_CHANGE_SURFACE", "1")
    root, db = _python_provider_repo(tmp_path)
    r = detect_change_surface(_PY_ISSUE, root, db)
    tmr = _mr(r, "azure", ROLE_TEST)
    sibs = {s.replace("\\", "/") for s in tmr.sibling_files}
    assert sibs == {"tests/test_aws.py", "tests/test_gcp.py"}
    # never leak test bodies / assertions into any evidence anywhere
    blob = "\n".join(
        ev for mr in r.missing_roles for ev in mr.evidence
    ) + "\n".join(
        ev for d in r.destinations for ev in d.evidence
    )
    assert "assert " not in blob
    assert "def test_" not in blob


def test_determinism_same_input_same_output(tmp_path, monkeypatch):
    monkeypatch.setenv("GT_CHANGE_SURFACE", "1")
    root, db = _python_provider_repo(tmp_path)
    a = detect_change_surface(_PY_ISSUE, root, db)
    b = detect_change_surface(_PY_ISSUE, root, db)
    assert _serialize(a) == _serialize(b)


def _serialize(r: ChangeSurfaceResult) -> str:
    parts = [f"abst={r.abstained}:{r.abstain_reason}", "ent=" + ",".join(r.entities)]
    for mr in r.missing_roles:
        parts.append(
            f"MR|{mr.entity}|{mr.role}|{sorted(mr.sibling_files)}|"
            f"{mr.registration_file}|{sorted(mr.signals)}|{mr.trust_tier}"
        )
    for d in r.destinations:
        parts.append(
            f"D|{d.entity}|{d.suggested_path}|{d.template_file}|{d.registration_file}"
        )
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Go — tree-only (no graph.db), impl + registration
# ---------------------------------------------------------------------------


def test_go_new_adapter_impl_and_registration(tmp_path, monkeypatch):
    monkeypatch.setenv("GT_CHANGE_SURFACE", "1")
    _w(tmp_path, "adapters/postgres.go",
       "package adapters\nfunc NewPostgres() *Postgres { return &Postgres{} }\n")
    _w(tmp_path, "adapters/mysql.go",
       "package adapters\nfunc NewMysql() *Mysql { return &Mysql{} }\n")
    _w(tmp_path, "registry.go",
       "package main\nimport \"x/adapters\"\n"
       "var Adapters = map[string]any{\n"
       "  \"postgres\": adapters.NewPostgres,\n"
       "  \"mysql\": adapters.NewMysql,\n}\n")
    issue = "Add a sqlite adapter to the adapters registry, like postgres and mysql.\n"
    r = detect_change_surface(issue, str(tmp_path), None)

    assert r.abstained is False, r.abstain_reason
    assert r.entities == ["sqlite"], r.entities
    roles = _roles_for(r, "sqlite")
    assert roles == {ROLE_IMPLEMENTATION, ROLE_REGISTRATION}, roles
    d = [x for x in r.destinations if x.entity == "sqlite"][0]
    assert d.suggested_path.replace("\\", "/") == "adapters/sqlite.go"
    assert d.template_file.replace("\\", "/") in (
        "adapters/postgres.go", "adapters/mysql.go")
    reg = _mr(r, "sqlite", ROLE_REGISTRATION)
    assert reg.registration_file is not None
    assert reg.registration_file.replace("\\", "/").endswith("registry.go")
    reffed = " ".join(t for _, t in reg.registration_lines).lower()
    assert "postgres" in reffed and "mysql" in reffed


# ---------------------------------------------------------------------------
# TypeScript — tree-only, index.ts registry must not become an impl sibling
# ---------------------------------------------------------------------------


def test_ts_new_provider_index_is_registry_not_sibling(tmp_path, monkeypatch):
    monkeypatch.setenv("GT_CHANGE_SURFACE", "1")
    _w(tmp_path, "src/providers/aws.ts", "export class AwsProvider {}\n")
    _w(tmp_path, "src/providers/gcp.ts", "export class GcpProvider {}\n")
    _w(tmp_path, "src/providers/index.ts",
       "export { AwsProvider } from './aws';\n"
       "export { GcpProvider } from './gcp';\n")
    issue = "Add azure provider under src/providers, mirroring aws and gcp.\n"
    r = detect_change_surface(issue, str(tmp_path), None)

    assert r.abstained is False, r.abstain_reason
    assert r.entities == ["azure"], r.entities
    roles = _roles_for(r, "azure")
    assert roles == {ROLE_IMPLEMENTATION, ROLE_REGISTRATION}, roles
    d = [x for x in r.destinations if x.entity == "azure"][0]
    assert d.suggested_path.replace("\\", "/") == "src/providers/azure.ts"
    # index.ts must be the registry, never the impl template
    assert d.template_file.replace("\\", "/") in (
        "src/providers/aws.ts", "src/providers/gcp.ts")
    impl = _mr(r, "azure", ROLE_IMPLEMENTATION)
    for s in impl.sibling_files:
        assert not s.replace("\\", "/").endswith("index.ts"), (
            "registry leaked into impl siblings", impl.sibling_files)


# ---------------------------------------------------------------------------
# partial hole — the headline case: impl EXISTS, registration/config absent
# ---------------------------------------------------------------------------


def test_partial_hole_impl_present_registration_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("GT_CHANGE_SURFACE", "1")
    root, db = _python_provider_repo(tmp_path)
    # azure implementation exists, but no registry entry + no config
    _w(tmp_path, "providers/azure.py",
       "class AzureProvider:\n    def connect(self):\n        return 'azure'\n")
    # rebuild graph including azure impl node
    db = _mk_graph(
        tmp_path / "graph2.db",
        [
            ("AwsProvider", "Class", "providers/aws.py", 0),
            ("GcpProvider", "Class", "providers/gcp.py", 0),
            ("AzureProvider", "Class", "providers/azure.py", 0),
            ("test_aws", "Function", "tests/test_aws.py", 1),
            ("test_gcp", "Function", "tests/test_gcp.py", 1),
        ],
    )
    r = detect_change_surface(_PY_ISSUE, root, db)
    roles = _roles_for(r, "azure")
    assert ROLE_IMPLEMENTATION not in roles, ("impl present -> not a hole", roles)
    assert ROLE_REGISTRATION in roles
    assert ROLE_CONFIG in roles
    # impl already exists -> no new-file destination
    assert [d for d in r.destinations if d.entity == "azure"] == []


# ---------------------------------------------------------------------------
# abstention 1 — single sibling (no group with >=2 members)
# ---------------------------------------------------------------------------


def test_abstain_single_sibling(tmp_path, monkeypatch):
    monkeypatch.setenv("GT_CHANGE_SURFACE", "1")
    _w(tmp_path, "providers/aws.py", "class AwsProvider:\n    pass\n")
    issue = "Add azure provider like the aws provider.\n"
    r = detect_change_surface(issue, str(tmp_path), None)
    assert r.abstained is True
    assert r.missing_roles == [] and r.destinations == []


# ---------------------------------------------------------------------------
# abstention 2 — no registry mechanism -> registration role not minted
# ---------------------------------------------------------------------------


def test_abstain_no_registry_no_registration_role(tmp_path, monkeypatch):
    monkeypatch.setenv("GT_CHANGE_SURFACE", "1")
    _w(tmp_path, "handlers/json.py", "def handle_json():\n    return 1\n")
    _w(tmp_path, "handlers/xml.py", "def handle_xml():\n    return 2\n")
    issue = "Add a yaml handler like the json and xml handlers.\n"
    r = detect_change_surface(issue, str(tmp_path), None)
    assert r.abstained is False, r.abstain_reason
    roles = _roles_for(r, "yaml")
    assert ROLE_IMPLEMENTATION in roles
    assert ROLE_REGISTRATION not in roles, ("no registry -> no registration fact", roles)


# ---------------------------------------------------------------------------
# abstention 3 — conflicting conventions (ambiguous destination dir)
# ---------------------------------------------------------------------------


def test_abstain_conflicting_conventions(tmp_path, monkeypatch):
    monkeypatch.setenv("GT_CHANGE_SURFACE", "1")
    _w(tmp_path, "providers/aws.py", "class AwsProvider:\n    pass\n")
    _w(tmp_path, "providers/gcp.py", "class GcpProvider:\n    pass\n")
    _w(tmp_path, "plugins/aws.py", "class AwsPlugin:\n    pass\n")
    _w(tmp_path, "plugins/gcp.py", "class GcpPlugin:\n    pass\n")
    issue = "Add an azure provider and an azure plugin, mirroring aws and gcp.\n"
    r = detect_change_surface(issue, str(tmp_path), None)
    # ambiguous: azure could be a provider OR a plugin -> abstain the entity
    assert _roles_for(r, "azure") == set()
    assert [d for d in r.destinations if d.entity == "azure"] == []
    assert "conflict" in r.abstain_reason or r.abstained is True


# ---------------------------------------------------------------------------
# unrelated issue — group exists but issue does not name its category
# ---------------------------------------------------------------------------


def test_unrelated_issue_does_not_emit(tmp_path, monkeypatch):
    monkeypatch.setenv("GT_CHANGE_SURFACE", "1")
    root, db = _python_provider_repo(tmp_path)
    issue = "Fix a rounding error in the billing totals calculation.\n"
    r = detect_change_surface(issue, root, db)
    assert r.missing_roles == []
    assert r.destinations == []


def test_instructional_verbs_near_category_do_not_mint_entities(tmp_path, monkeypatch):
    """Repository-audit prose is not a request to add a new family member."""
    monkeypatch.setenv("GT_CHANGE_SURFACE", "1")
    root, db = _python_provider_repo(tmp_path)
    issue = (
        "Before editing, audit provider behavior and trace provider calls. "
        "Verify provider contracts after the repair.\n"
    )

    r = detect_change_surface(issue, root, db)

    assert r.entities == []
    assert r.missing_roles == [] and r.destinations == []
    assert r.abstained is True


def test_negated_feature_addition_does_not_mint_destination(tmp_path, monkeypatch):
    """A forbidden addition is the opposite of a missing change surface."""
    monkeypatch.setenv("GT_CHANGE_SURFACE", "1")
    root, db = _python_provider_repo(tmp_path)

    r = detect_change_surface(
        "Do not add an azure provider; only repair the existing AWS provider.\n",
        root,
        db,
    )

    assert r.entities == []
    assert r.missing_roles == [] and r.destinations == []
    assert r.abstained is True


def test_connector_after_added_symbol_is_not_a_category(tmp_path, monkeypatch):
    """A conjunction is prose structure, never a mined feature-family category."""
    monkeypatch.setenv("GT_CHANGE_SURFACE", "1")
    root, db = _python_provider_repo(tmp_path)

    r = detect_change_surface("Add telemetry and provider diagnostics.\n", root, db)

    assert r.entities == []
    assert r.missing_roles == [] and r.destinations == []
    assert r.abstained is True

    connector_group = _Group(slots=[], fixed_tokens={"and"})
    assert not _entity_links_group("telemetry", ["add", "telemetry", "and"], connector_group)


# ---------------------------------------------------------------------------
# BOUNCE F2 — a bare English adjective before the category noun must NOT mint
# an entity. Adjacency alone is ONE signal; the entity needs an independent
# second signal (novel code token, or an existing sibling member).
# ---------------------------------------------------------------------------


def _adjective_repo(tmp_path: Path) -> str:
    _w(tmp_path, "providers/__init__.py",
       "from .aws import AwsProvider\nfrom .gcp import GcpProvider\n"
       "REGISTRY = {'aws': AwsProvider, 'gcp': GcpProvider}\n")
    _w(tmp_path, "providers/aws.py", "class AwsProvider:\n    pass\n")
    _w(tmp_path, "providers/gcp.py", "class GcpProvider:\n    pass\n")
    return str(tmp_path)


def test_adjective_additional_does_not_mint(tmp_path, monkeypatch):
    monkeypatch.setenv("GT_CHANGE_SURFACE", "1")
    root = _adjective_repo(tmp_path)
    issue = "Add an additional provider, mirroring the aws and gcp providers.\n"
    r = detect_change_surface(issue, root, None)
    assert r.entities == [], r.entities
    assert r.missing_roles == [] and r.destinations == []
    assert r.abstained is True


def test_adjective_custom_does_not_mint(tmp_path, monkeypatch):
    monkeypatch.setenv("GT_CHANGE_SURFACE", "1")
    root = _adjective_repo(tmp_path)
    issue = "Add a custom provider like the existing aws and gcp providers.\n"
    r = detect_change_surface(issue, root, None)
    assert r.entities == [], r.entities
    assert r.missing_roles == [] and r.destinations == []
    assert r.abstained is True


def test_novel_token_azure_still_mints(tmp_path, monkeypatch):
    """The control for F2: a genuinely novel token in the same construction MUST
    still mint (plain prose, no backticks needed)."""
    monkeypatch.setenv("GT_CHANGE_SURFACE", "1")
    root = _adjective_repo(tmp_path)
    issue = "Add azure provider support, mirroring the aws and gcp providers.\n"
    r = detect_change_surface(issue, root, None)
    assert r.entities == ["azure"], r.entities
    assert _roles_for(r, "azure") >= {ROLE_IMPLEMENTATION, ROLE_REGISTRATION}
    # signals must be genuinely derived: adjacency + the independent novel-token
    for mr in r.missing_roles:
        assert "issue_adjacency" in mr.signals, mr.signals
        assert "novel_token" in mr.signals, mr.signals


def test_analogy_exemplars_are_not_requested_entities(tmp_path, monkeypatch):
    """The add/support governor stops at ``like``; aws/gcp are evidence for
    azure, not additional requested destinations."""
    monkeypatch.setenv("GT_CHANGE_SURFACE", "1")
    root = _adjective_repo(tmp_path)

    r = detect_change_surface(
        "Add azure provider support like aws and gcp providers.\n", root, None
    )

    assert r.entities == ["azure"], r.entities
    assert {d.entity for d in r.destinations} == {"azure"}


def test_partial_hole_entity_admitted_via_membership(tmp_path, monkeypatch):
    """F2's (b) leg: an EXISTING sibling member (azure impl present, wiring
    absent) is admitted through membership, not novelty."""
    monkeypatch.setenv("GT_CHANGE_SURFACE", "1")
    root = _adjective_repo(tmp_path)
    _w(tmp_path, "providers/azure.py", "class AzureProvider:\n    pass\n")
    issue = "Add azure provider support to the registry, like aws and gcp providers.\n"
    r = detect_change_surface(issue, root, None)
    reg = _mr(r, "azure", ROLE_REGISTRATION)
    assert "existing_sibling_member" in reg.signals, reg.signals


# ---------------------------------------------------------------------------
# BOUNCE F1 — registry precision: prose/docstring mentions never elect a
# registry; conftest is never a registry; evidence lines are CODE lines.
# ---------------------------------------------------------------------------


def test_registry_decoy_docstring_loses(tmp_path, monkeypatch):
    monkeypatch.setenv("GT_CHANGE_SURFACE", "1")
    _w(tmp_path, "providers/__init__.py",
       "from .aws import AwsProvider\nfrom .gcp import GcpProvider\n\n"
       "REGISTRY = {'aws': AwsProvider, 'gcp': GcpProvider}\n")
    _w(tmp_path, "providers/aws.py", "class AwsProvider:\n    pass\n")
    _w(tmp_path, "providers/gcp.py", "class GcpProvider:\n    pass\n")
    # DECOY: shorter path, mentions both siblings ONLY in docstring + comment
    _w(tmp_path, "docs.py",
       '"""Supported backends today: aws and gcp.\n\n'
       'TODO(perf): the aws path is slow; gcp is fine.\n'
       '"""\n'
       'SUPPORTED = "see module docstring"\n')
    issue = "Add azure provider support. We need an azure provider like aws and gcp.\n"
    r = detect_change_surface(issue, str(tmp_path), None)
    reg = _mr(r, "azure", ROLE_REGISTRATION)
    assert reg.registration_file.replace("\\", "/").endswith("providers/__init__.py"), (
        "decoy docs.py hijacked the registry", reg.registration_file)
    # evidence must be code lines, never docstring prose
    for _ln, txt in reg.registration_lines:
        assert "Supported backends" not in txt
        assert not txt.startswith('"""')
        assert ("import" in txt) or ("=" in txt) or (":" in txt), txt


def test_comment_only_mentions_never_elect_a_registry(tmp_path, monkeypatch):
    """With NO real registry, a comment/docstring-only cross-reference file must
    not mint MissingRole(registration) at all."""
    monkeypatch.setenv("GT_CHANGE_SURFACE", "1")
    _w(tmp_path, "providers/aws.py", "class AwsProvider:\n    pass\n")
    _w(tmp_path, "providers/gcp.py", "class GcpProvider:\n    pass\n")
    _w(tmp_path, "notes.py",
       "# aws is the default; gcp is opt-in\n"
       '"""aws and gcp are the supported families."""\n')
    issue = "Add azure provider like aws and gcp providers.\n"
    r = detect_change_surface(issue, str(tmp_path), None)
    assert ROLE_REGISTRATION not in _roles_for(r, "azure"), r.missing_roles


def test_conftest_never_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("GT_CHANGE_SURFACE", "1")
    _w(tmp_path, "providers/aws.py", "class AwsProvider:\n    pass\n")
    _w(tmp_path, "providers/gcp.py", "class GcpProvider:\n    pass\n")
    _w(tmp_path, "conftest.py",
       "from providers.aws import AwsProvider\n"
       "from providers.gcp import GcpProvider\n"
       "FIXTURES = {'aws': AwsProvider, 'gcp': GcpProvider}\n")
    issue = "Add azure provider like aws and gcp providers.\n"
    r = detect_change_surface(issue, str(tmp_path), None)
    assert ROLE_REGISTRATION not in _roles_for(r, "azure"), r.missing_roles
    for g in r.sibling_groups:
        rf = g.get("registry_file")
        assert not (rf and str(rf).replace("\\", "/").endswith("conftest.py")), g


# ---------------------------------------------------------------------------
# BOUNCE F3 — camelCase member refs must count as registry cross-references
# ---------------------------------------------------------------------------


def test_camelcase_only_registry_detected(tmp_path, monkeypatch):
    monkeypatch.setenv("GT_CHANGE_SURFACE", "1")
    _w(tmp_path, "providers/aws.py", "class AwsProvider:\n    pass\n")
    _w(tmp_path, "providers/gcp.py", "class GcpProvider:\n    pass\n")
    # the ONLY sibling refs are camelCase identifiers — no bare 'aws'/'gcp'
    _w(tmp_path, "wiring.py",
       "def build(registry):\n"
       "    registry.register(AwsProvider)\n"
       "    registry.register(GcpProvider)\n")
    issue = "Add azure provider like aws and gcp providers.\n"
    r = detect_change_surface(issue, str(tmp_path), None)
    reg = _mr(r, "azure", ROLE_REGISTRATION)
    assert reg.registration_file.replace("\\", "/").endswith("wiring.py")


def test_ref_pattern_casing_boundaries():
    """The word-boundary invariant across casings: camelCase / snake / ALL-CAPS
    references match; substrings never do."""
    from groundtruth.pretask.change_surface import _ref_pattern

    p = _ref_pattern("aws")
    for hay in ("AwsProvider", "register(AwsProvider)", "aws_config",
                "'aws': AwsProvider", "AWS_REGION = 1", "handleAwsRequest()"):
        assert p.search(hay), f"expected match in {hay!r}"
    for hay in ("awesome sauce", "flaws happen", "AWSOME_CONSTANT = 1"):
        assert not p.search(hay), f"unexpected match in {hay!r}"
    assert not _ref_pattern("cat").search("CATALOG = []")


# ---------------------------------------------------------------------------
# Real repository shapes: compound feature names and repeated directory/file
# member slots.  These are general convention fixtures, not task-ID fixtures.
# ---------------------------------------------------------------------------


def test_compound_explicit_name_extends_existing_rule_member(tmp_path, monkeypatch):
    """A named rule variant extends ``rule_action.go``; ``lint`` is the
    category modifier, not the new member and must never become ``rule_lint.go``.
    """
    monkeypatch.setenv("GT_CHANGE_SURFACE", "1")
    _w(tmp_path, "rule_action.go", "package actionlint\ntype RuleAction struct{}\n")
    _w(tmp_path, "rule_expression.go", "package actionlint\ntype RuleExpression struct{}\n")
    _w(tmp_path, "rule_if.go", "package actionlint\ntype RuleIf struct{}\n")
    _w(
        tmp_path,
        "popular_actions.go",
        "package actionlint\n"
        "var PopularActions = []string{\n"
        "  \"action\",\n  \"action\",\n  \"expression\",\n"
        "  \"expression\",\n  \"if\",\n  \"if\",\n}\n",
    )
    _w(
        tmp_path,
        "linter.go",
        "package actionlint\nvar rules = []any{NewRuleAction(), "
        "NewRuleExpression(), NewRuleIf()}\n",
    )
    issue = (
        "Add a lint rule with error kind `action-pinning` that checks action "
        "references for version pinning.\n"
    )

    r = detect_change_surface(issue, str(tmp_path), None)

    paths = {d.suggested_path.replace("\\", "/") for d in r.destinations}
    assert "rule_action_pinning.go" in paths, r.destinations
    assert "rule_lint.go" not in paths, r.destinations
    assert "lint" not in r.entities, r.entities
    assert _roles_for(r, "action-pinning") == {
        ROLE_IMPLEMENTATION,
        ROLE_REGISTRATION,
    }
    reg = _mr(r, "action-pinning", ROLE_REGISTRATION)
    assert reg.registration_file == "linter.go"


def test_negated_compound_feature_name_does_not_extend_rule_family(
    tmp_path, monkeypatch
):
    """Quoted code spelling remains quiet when its local clause forbids it."""
    monkeypatch.setenv("GT_CHANGE_SURFACE", "1")
    for member in ("action", "expression", "if"):
        _w(
            tmp_path,
            f"rule_{member}.go",
            f"package rules\ntype Rule{member.title()} struct{{}}\n",
        )

    r = detect_change_surface(
        "Do not add a lint rule named `action-pinning`; repair rule_action.go instead.\n",
        str(tmp_path),
        None,
    )

    assert r.entities == []
    assert r.destinations == [] and r.missing_roles == []
    assert r.abstained is True


def test_repeated_member_directory_and_file_beats_flat_type_distractor(
    tmp_path, monkeypatch
):
    """When repo convention repeats the member in directory + filename, infer
    that structural family instead of a flat same-word type destination.
    """
    monkeypatch.setenv("GT_CHANGE_SURFACE", "1")
    for member in ("parse", "pipe", "fallback"):
        _w(tmp_path, f"library/src/methods/{member}/index.ts", "export {};\n")
        _w(
            tmp_path,
            f"library/src/methods/{member}/{member}.ts",
            f"export function {member}() {{}}\n",
        )
    for member in ("config", "issue", "schema"):
        _w(tmp_path, f"library/src/types/{member}.ts", f"export type {member} = unknown;\n")
    issue = (
        "Add first-class recursive schema composition. The public API should "
        "include recursive(...) and recursiveAsync(...) wrappers, all available "
        "from the public methods surface.\n"
    )

    r = detect_change_surface(issue, str(tmp_path), None)

    paths = {d.suggested_path.replace("\\", "/") for d in r.destinations}
    assert "library/src/methods/recursive/recursive.ts" in paths, r.destinations
    assert "library/src/types/recursive.ts" not in paths, r.destinations
