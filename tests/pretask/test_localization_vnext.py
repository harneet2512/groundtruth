from __future__ import annotations

import hashlib
import json
import sqlite3
import tracemalloc
from dataclasses import replace
from pathlib import Path

import pytest

from groundtruth.pretask.localization_vnext import (
    CandidateAction,
    CoverageState,
    EvidenceFamily,
    EvidenceUnit,
    LocalizationPolicy,
    LocalizationRequest,
    LocalizationState,
    ReasonCode,
    SourceRegion,
    build_structured_symbol_passages,
    census_capabilities,
    derive_certified_relationships,
    detect_ecosystem_adapter,
    discover_candidates,
    extract_behavior_facets,
    fuse_by_evidence_class,
    localize_vnext,
    merge_regions,
)
from groundtruth.pretask.localization_vnext import engine as vnext_engine


def _graph(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "parser.py").write_text(
        "class BaseParser:\n"
        "    def parse(self, value):\n"
        "        raise ParseError(value)\n"
        "\n"
        "class JsonParser(BaseParser):\n"
        "    def parse(self, value):\n"
        "        try:\n"
        "            return decode(value)\n"
        "        except ParseError:\n"
        "            return None\n",
        encoding="utf-8",
    )
    (repo / "src" / "config.py").write_text(
        "TIMEOUT = 10\nRETRIES = 2\n",
        encoding="utf-8",
    )
    db = tmp_path / "graph.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY, label TEXT, name TEXT, qualified_name TEXT,
            file_path TEXT, start_line INTEGER, end_line INTEGER, signature TEXT,
            return_type TEXT, is_exported INTEGER, is_test INTEGER, language TEXT,
            parent_id INTEGER
        );
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER, type TEXT,
            source_line INTEGER, source_file TEXT, resolution_method TEXT,
            confidence REAL, metadata TEXT, trust_tier TEXT, candidate_count INTEGER,
            evidence_type TEXT, verification_status TEXT
        );
        CREATE TABLE properties (
            id INTEGER PRIMARY KEY, node_id INTEGER, kind TEXT, value TEXT,
            line INTEGER, confidence REAL
        );
        """
    )
    con.executemany(
        """
        INSERT INTO nodes
        (id,label,name,qualified_name,file_path,start_line,end_line,signature,
         return_type,is_exported,is_test,language,parent_id)
        VALUES (?,?,?,?,?,?,?,?,?,1,0,'python',?)
        """,
        [
            (1, "Class", "BaseParser", "BaseParser", "src/parser.py", 1, 4, "class BaseParser", "", None),
            (2, "Method", "parse", "BaseParser.parse", "src/parser.py", 2, 3, "parse(self, value)", "", 1),
            (3, "Class", "JsonParser", "JsonParser", "src/parser.py", 5, 11, "class JsonParser", "", None),
            (4, "Method", "parse", "JsonParser.parse", "src/parser.py", 6, 11, "parse(self, value)", "", 3),
            (5, "Class", "ParseError", "ParseError", "src/parser.py", 1, 1, "class ParseError", "", None),
            (6, "Function", "load_config", "load_config", "src/config.py", 1, 2, "load_config()", "", None),
        ],
    )
    con.executemany(
        """
        INSERT INTO edges
        (id,source_id,target_id,type,source_line,source_file,resolution_method,
         confidence,metadata,trust_tier,candidate_count,evidence_type,verification_status)
        VALUES (?,?,?,?,?,?,'import',1.0,'','CERTIFIED',1,'structural','verified')
        """,
        [
            (1, 3, 1, "EXTENDS", 5, "src/parser.py"),
            (2, 4, 5, "RAISES", 8, "src/parser.py"),
            (3, 4, 6, "CALLS", 8, "src/parser.py"),
        ],
    )
    con.execute(
        "INSERT INTO properties VALUES (1,4,'exception_handler','ParseError',9,1.0)"
    )
    con.commit()
    con.close()
    return repo, db


def _request(repo: Path, db: Path, issue: str | None = None) -> LocalizationRequest:
    return LocalizationRequest(
        issue_text=issue
        or (
            "Actual behavior: JsonParser.parse returns a value after malformed JSON. "
            "Expected behavior: parsing must catch ParseError and return None. "
            "The configuration in src/config.py must remain unchanged."
        ),
        repository_root=str(repo),
        graph_db=str(db),
        revision_identity="fixture-rev",
        policy=LocalizationPolicy(max_candidates=500, max_source_tokens=16_000),
    )


def test_behavior_decomposition_keeps_anchors_and_obligations_separate(tmp_path):
    repo, db = _graph(tmp_path)
    facets = extract_behavior_facets(_request(repo, db))
    assert facets.actor == "JsonParser"
    assert "parse" in facets.operation
    assert "returns a value" in facets.observed_behavior
    assert "catch ParseError" in facets.expected_behavior
    assert "src/config.py" in facets.architectural_boundary
    assert {"parsing", "configuration"} <= set(facets.policies)
    assert facets.anchor_symbols
    assert facets.obligation_ids


@pytest.mark.parametrize(
    ("issue", "expected_mode"),
    [
        ("", "absent"),
        ("Something is wrong.", "sparse"),
        (
            'File "src/parser.py", line 8, in parse\nParseError: malformed',
            "traceback",
        ),
        ("Update src/parser.py so parsing returns None.", "explicit_path"),
        ("JsonParser.parse returns the wrong value.", "symbol_anchored"),
        (
            "Malformed input should return None instead of raising.",
            "behavior_described",
        ),
    ],
)
def test_issue_information_modes_are_explicit(
    tmp_path,
    issue,
    expected_mode,
):
    repo, db = _graph(tmp_path)
    request = LocalizationRequest(
        issue_text=issue,
        repository_root=str(repo),
        graph_db=str(db),
        revision_identity="fixture",
    )
    facets = extract_behavior_facets(request)
    assert facets.issue_mode == expected_mode


def test_absent_or_sparse_problem_abstains_instead_of_ranking_the_repository(tmp_path):
    repo, db = _graph(tmp_path)

    for issue in ("", "Something is wrong."):
        request = LocalizationRequest(
            issue_text=issue,
            repository_root=str(repo),
            graph_db=str(db),
            revision_identity="fixture",
        )
        result = localize_vnext(request)
        assert result.stopping_reason == "insufficient_issue_evidence"
        assert result.discoveries == ()
        assert result.admitted_regions == ()


def test_absent_problem_can_localize_from_certified_runtime_evidence(tmp_path):
    repo, db = _graph(tmp_path)
    runtime = EvidenceUnit.create(
        file_path="src/parser.py",
        symbol="JsonParser.parse",
        start_line=8,
        end_line=8,
        family=EvidenceFamily.TRACEBACK,
        confidence=1.0,
        provenance=("runtime_trace",),
        roles=("operation", "observed_behavior"),
        source_tokens=1,
        signal_class="runtime",
        signal_rank=1,
        fact_span=True,
        explicit_provenance=True,
    )
    request = LocalizationRequest(
        issue_text="",
        repository_root=str(repo),
        graph_db=str(db),
        revision_identity="fixture",
        new_evidence=(runtime,),
    )

    result = localize_vnext(request)

    assert result.facets.issue_mode == "evidence_only"
    assert {"operation", "observed_behavior"} <= set(result.coverage.required)
    assert result.admitted_regions
    assert result.stopping_reason != "insufficient_issue_evidence"


def test_absent_problem_without_new_evidence_does_not_invalidate_prior_state(tmp_path):
    repo, db = _graph(tmp_path)
    prior = LocalizationState(
        accepted=("accepted-id",),
        rejected=("rejected-id",),
        deferred=("deferred-id",),
        unresolved_roles=("operation",),
    )
    request = LocalizationRequest(
        issue_text="",
        repository_root=str(repo),
        graph_db=str(db),
        revision_identity="fixture",
        prior_state=prior,
    )

    result = localize_vnext(request)

    assert result.state == prior
    assert result.delta is not None
    assert result.delta.invalidated_evidence == ()
    assert result.coverage.unresolved == ("operation",)


def test_capability_census_is_explicit_and_never_invents_missing_surfaces(tmp_path):
    repo, db = _graph(tmp_path)
    caps = census_capabilities(_request(repo, db))
    assert caps.available["graph_schema"] is True
    assert caps.available["property_spans"] is True
    assert caps.available["typed_edges"] is True
    assert caps.available["body_fts"] is False
    assert caps.unavailable["body_fts"]
    assert caps.available["publishes"] is False
    assert caps.available["subscribes"] is False


def test_class_level_rrf_collapses_correlated_lexical_votes():
    evidence = [
        EvidenceUnit.create(
            file_path="lexical.py",
            symbol="parse",
            start_line=1,
            end_line=5,
            family=family,
            confidence=0.8,
            provenance=(family.value,),
            roles=("operation",),
            source_tokens=20,
            signal_class="lexical",
            signal_rank=rank,
        )
        for rank, family in enumerate(
            (EvidenceFamily.IDENTIFIER, EvidenceFamily.NODE_FTS, EvidenceFamily.BODY_BM25),
            start=1,
        )
    ]
    evidence += [
        EvidenceUnit.create(
            file_path="structural.py",
            symbol="decode",
            start_line=1,
            end_line=8,
            family=EvidenceFamily.GRAPH,
            relation="CALLS",
            confidence=1.0,
            provenance=("CALLS", "import"),
            roles=("operation",),
            source_tokens=30,
            signal_class="structural",
            signal_rank=1,
        ),
        EvidenceUnit.create(
            file_path="structural.py",
            symbol="decode",
            start_line=1,
            end_line=8,
            family=EvidenceFamily.SEMANTIC,
            confidence=0.75,
            provenance=("frozen-onnx",),
            roles=("expected_behavior",),
            source_tokens=30,
            signal_class="semantic",
            signal_rank=2,
        ),
    ]
    fused = fuse_by_evidence_class(evidence)
    assert fused["structural.py"] > fused["lexical.py"]


def test_class_level_rrf_preserves_independent_classes_after_region_consolidation():
    consolidated = [
        EvidenceUnit.create(
            file_path="independent.py",
            symbol="decode",
            start_line=1,
            end_line=8,
            family=EvidenceFamily.GRAPH,
            confidence=1.0,
            provenance=("consolidated",),
            roles=("operation",),
            source_tokens=30,
            signal_class="semantic+structural",
            signal_rank=1,
        ),
        EvidenceUnit.create(
            file_path="lexical.py",
            symbol="decode",
            start_line=1,
            end_line=8,
            family=EvidenceFamily.LEXICAL,
            confidence=0.7,
            provenance=("grep",),
            roles=("operation",),
            source_tokens=30,
            signal_class="lexical",
            signal_rank=1,
        ),
    ]

    fused = fuse_by_evidence_class(consolidated)

    assert fused["independent.py"] == pytest.approx(2.0 / 61.0)
    assert fused["independent.py"] > fused["lexical.py"]


def test_class_level_rrf_counts_each_consolidated_class_once_per_file():
    evidence = [
        EvidenceUnit.create(
            file_path="same.py",
            symbol=symbol,
            start_line=line,
            end_line=line + 2,
            family=EvidenceFamily.GRAPH,
            confidence=0.9,
            provenance=("consolidated",),
            roles=("operation",),
            source_tokens=10,
            signal_class=signal_class,
            signal_rank=rank,
        )
        for symbol, line, signal_class, rank in (
            ("first", 1, "semantic+structural", 1),
            ("second", 10, "lexical+structural", 2),
        )
    ]

    fused = fuse_by_evidence_class(evidence)

    assert fused["same.py"] == pytest.approx(2.0 / 61.0 + 1.0 / 62.0)


def test_structured_semantic_passages_have_fixed_fields_and_nonleaking_test_linkage(tmp_path):
    repo, db = _graph(tmp_path)
    con = sqlite3.connect(db)
    con.execute(
        """
        INSERT INTO nodes
        (id,label,name,qualified_name,file_path,start_line,end_line,signature,
         return_type,is_exported,is_test,language,parent_id)
        VALUES (20,'Function','test_secret_assertion','test_secret_assertion',
                'tests/test_parser.py',1,2,'test_secret_assertion()','',0,1,'python',NULL)
        """
    )
    con.execute(
        """
        INSERT INTO edges
        (id,source_id,target_id,type,source_line,source_file,resolution_method,
         confidence,metadata,trust_tier,candidate_count,evidence_type,verification_status)
        VALUES (20,20,4,'CALLS',2,'tests/test_parser.py','import',1.0,'',
                'CERTIFIED',1,'structural','verified')
        """
    )
    con.commit()
    con.close()
    passages = build_structured_symbol_passages(
        _request(repo, db), file_paths={"src/parser.py"}
    )
    passage = passages["src/parser.py::JsonParser.parse"]
    assert [line.split(":", 1)[0] for line in passage.splitlines()] == [
        "symbol",
        "role",
        "signature",
        "callers",
        "callees",
        "reads",
        "writes",
        "routes",
        "configuration",
        "exceptions",
        "serialization",
        "test_linkage",
    ]
    assert "linked_test_count=1" in passage
    assert "test_secret_assertion" not in passage


def test_only_certified_overrides_and_catches_are_derived(tmp_path):
    repo, db = _graph(tmp_path)
    con = sqlite3.connect(db)
    con.executemany(
        """
        INSERT INTO nodes
        (id,label,name,qualified_name,file_path,start_line,end_line,signature,
         return_type,is_exported,is_test,language,parent_id)
        VALUES (?,?,?,?,?,?,?,?,?,1,0,'python',?)
        """,
        [
            (30, "Class", "BaseNoise", "BaseNoise", "src/noise.py", 1, 3, "class BaseNoise", "", None),
            (31, "Method", "flush", "BaseNoise.flush", "src/noise.py", 2, 3, "flush()", "", 30),
            (32, "Class", "Noise", "Noise", "src/noise.py", 5, 8, "class Noise", "", None),
            (33, "Method", "flush", "Noise.flush", "src/noise.py", 6, 8, "flush()", "", 32),
        ],
    )
    con.execute(
        """
        INSERT INTO edges
        (id,source_id,target_id,type,source_line,source_file,resolution_method,
         confidence,metadata,trust_tier,candidate_count,evidence_type,verification_status)
        VALUES (30,32,30,'EXTENDS',5,'src/noise.py','import',1.0,'',
                'CERTIFIED',1,'structural','verified')
        """
    )
    con.commit()
    con.close()
    relations = derive_certified_relationships(_request(repo, db))
    got = {(e.relation, e.symbol, e.confidence) for e in relations}
    assert ("OVERRIDES", "JsonParser.parse", 1.0) in got
    assert ("CATCHES", "JsonParser.parse", 1.0) in got
    assert not any(e.symbol == "Noise.flush" for e in relations)
    assert not any(e.relation in {"PUBLISHES", "SUBSCRIBES", "CONFIGURES", "VALIDATES"} for e in relations)


def test_derived_relationships_preserve_certified_source_confidence(tmp_path):
    repo, db = _graph(tmp_path)
    con = sqlite3.connect(db)
    con.execute("UPDATE edges SET confidence=0.9 WHERE id=1")
    con.execute("UPDATE properties SET confidence=0.9 WHERE id=1")
    con.commit()
    con.close()

    relations = derive_certified_relationships(_request(repo, db))
    got = {
        (unit.relation, unit.symbol): unit.confidence
        for unit in relations
    }

    assert got[("OVERRIDES", "JsonParser.parse")] == 0.9
    assert got[("CATCHES", "JsonParser.parse")] == 0.9


def test_regions_merge_hash_exact_source_and_prefer_smallest_span(tmp_path):
    repo, _db = _graph(tmp_path)
    regions = [
        SourceRegion.from_source(repo, "src/parser.py", 6, 9, "JsonParser.parse", ("operation",), "symbol_span"),
        SourceRegion.from_source(repo, "src/parser.py", 9, 11, "JsonParser.parse", ("exception",), "property_span"),
    ]
    merged = merge_regions(regions)
    assert len(merged) == 1
    region = merged[0]
    # The graph's stale end-line 11 is clamped to the real ten-line source.
    assert (region.start_line, region.end_line) == (6, 10)
    source = "\n".join((repo / "src/parser.py").read_text(encoding="utf-8").splitlines()[5:10])
    assert region.content_sha256 == hashlib.sha256(source.encode("utf-8")).hexdigest()
    assert region.source_tokens == (len(source) + 3) // 4


def test_overlapping_parent_and_child_symbol_regions_merge_without_duplicate_source(tmp_path):
    repo, _db = _graph(tmp_path)
    regions = [
        SourceRegion.from_source(
            repo,
            "src/parser.py",
            5,
            10,
            "JsonParser",
            ("actor",),
            "symbol_span",
        ),
        SourceRegion.from_source(
            repo,
            "src/parser.py",
            6,
            10,
            "JsonParser.parse",
            ("operation",),
            "symbol_span",
        ),
    ]

    merged = merge_regions(regions)

    assert len(merged) == 1
    assert merged[0].symbol == "JsonParser | JsonParser.parse"
    assert merged[0].roles == ("actor", "operation")


def test_region_merge_orders_by_span_before_symbol_name(tmp_path):
    repo, _db = _graph(tmp_path)
    source = repo / "src" / "long.py"
    source.write_text(
        "\n".join(f"line_{line}" for line in range(1, 201)) + "\n",
        encoding="utf-8",
    )
    regions = [
        SourceRegion.from_source(
            repo, "src/long.py", 150, 155, "Alpha", ("actor",), "symbol_span"
        ),
        SourceRegion.from_source(
            repo, "src/long.py", 10, 15, "Zulu", ("operation",), "symbol_span"
        ),
    ]

    merged = merge_regions(regions)

    assert len(merged) == 2
    assert [(region.start_line, region.end_line) for region in merged] == [
        (10, 15),
        (150, 155),
    ]


def test_coverage_admission_stops_and_persists_negative_evidence(tmp_path):
    repo, db = _graph(tmp_path)
    low_confidence = EvidenceUnit.create(
        file_path="src/config.py",
        symbol="guess",
        start_line=1,
        end_line=1,
        family=EvidenceFamily.HISTORY,
        confidence=0.2,
        provenance=("weak_history_name_match",),
        roles=("configuration",),
        source_tokens=2,
        signal_class="history",
        signal_rank=50,
    )
    request = replace(_request(repo, db), new_evidence=(low_confidence,))
    first = localize_vnext(request)
    assert first.admitted_regions
    assert first.stopping_reason == "no_positive_marginal"
    assert first.coverage.unresolved
    assert any(d.action is CandidateAction.REJECT for d in first.decisions)

    rejected = tuple(d.evidence_id for d in first.decisions if d.action is CandidateAction.REJECT)
    prior = LocalizationState(
        accepted=tuple(d.evidence_id for d in first.decisions if d.action is CandidateAction.ADMIT),
        rejected=rejected,
        deferred=(),
        unresolved_roles=(),
        decision_reasons=tuple(
            (d.evidence_id, tuple(code.value for code in d.reason_codes))
            for d in first.decisions
        ),
    )
    second = localize_vnext(replace(request, prior_state=prior))
    assert second.delta is not None
    assert not set(rejected) & set(second.delta.newly_accepted)
    assert any(
        ReasonCode.PREVIOUSLY_REJECTED in d.reason_codes
        for d in second.decisions
        if d.evidence_id in rejected
    )


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        ({"pyproject.toml": '[project]\ndependencies=["fastapi"]\n'}, "python_web"),
        ({"pom.xml": "<dependency>spring-web</dependency>"}, "java_spring"),
        ({"package.json": '{"dependencies":{"express":"1"}}'}, "javascript_express"),
        ({"app.csproj": "<Project Sdk=\"Microsoft.NET.Sdk.Web\" />"}, "dotnet_aspnet"),
        ({"go.mod": "module example.test\nrequire github.com/gin-gonic/gin v1.0.0"}, "go_router"),
        ({"Cargo.toml": "[package]\nname='plain'"}, "generic"),
    ],
)
def test_ecosystem_adapters_are_selected_from_repository_evidence(tmp_path, files, expected):
    repo = tmp_path / "misleading-repository-name"
    repo.mkdir()
    for name, text in files.items():
        (repo / name).write_text(text, encoding="utf-8")
    assert detect_ecosystem_adapter(repo).name == expected


def test_result_hash_is_deterministic_and_excludes_runtime_metrics(tmp_path):
    repo, db = _graph(tmp_path)
    hashes = {localize_vnext(_request(repo, db)).deterministic_hash for _ in range(3)}
    assert len(hashes) == 1
    result = localize_vnext(_request(repo, db))
    mutated = replace(result, metrics={**result.metrics, "latency_ms": 99999.0})
    assert mutated.compute_deterministic_hash() == result.deterministic_hash


def test_operational_token_rail_returns_explicit_incomplete_coverage(tmp_path):
    repo, db = _graph(tmp_path)
    request = replace(
        _request(repo, db),
        policy=LocalizationPolicy(max_candidates=500, max_source_tokens=1, max_region_tokens=1),
    )
    result = localize_vnext(request)
    assert result.stopping_reason == "source_token_rail"
    assert result.coverage.unresolved


def test_source_token_rail_is_not_overwritten_by_later_candidate_exhaustion(
    tmp_path,
    monkeypatch,
):
    repo, db = _graph(tmp_path)
    request = replace(
        _request(repo, db, "parse completes"),
        policy=LocalizationPolicy(
            max_candidates=500,
            max_source_tokens=16_000,
            max_region_tokens=1,
        ),
    )
    relevant = EvidenceUnit.create(
        file_path="src/parser.py",
        symbol="JsonParser.parse",
        start_line=6,
        end_line=11,
        family=EvidenceFamily.IDENTIFIER,
        confidence=1.0,
        provenance=("relevant",),
        roles=("operation",),
        signal_class="identifier",
        signal_rank=1,
    )
    irrelevant = EvidenceUnit.create(
        file_path="src/config.py",
        symbol="load_config",
        start_line=1,
        end_line=2,
        family=EvidenceFamily.LEXICAL,
        confidence=0.8,
        provenance=("irrelevant",),
        roles=(),
        signal_class="lexical",
        signal_rank=2,
    )
    monkeypatch.setattr(
        vnext_engine,
        "discover_candidates",
        lambda *_args, **_kwargs: [relevant, irrelevant],
    )

    result = localize_vnext(request)

    assert result.stopping_reason == "source_token_rail"
    assert set(result.coverage.unresolved) == {"operation", "parsing"}


def test_serialized_schema_is_stable_and_eight_decimal(tmp_path):
    repo, db = _graph(tmp_path)
    payload = localize_vnext(_request(repo, db)).to_dict()
    assert payload["schema"] == "gt.localization.vnext.v1"
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    assert "gold" not in encoded.lower()
    assert isinstance(payload["metrics"]["latency_ms"], str)
    assert payload["metrics"]["latency_ms"].count(".") == 1
    assert len(payload["metrics"]["latency_ms"].split(".")[1]) == 8


def test_discovery_preserves_distinct_regions_in_one_file_without_role_laundering(
    tmp_path,
):
    repo, db = _graph(tmp_path)
    request = _request(repo, db)
    facets = extract_behavior_facets(request)

    discoveries = discover_candidates(request, facets)
    parser_regions = [
        unit
        for unit in discoveries
        if unit.file_path == "src/parser.py" and unit.symbol
    ]

    assert len(
        {(unit.symbol, unit.start_line, unit.end_line) for unit in parser_regions}
    ) >= 2


def test_missing_discovery_is_unresolved_not_unavailable_when_capability_exists(
    tmp_path,
):
    repo, db = _graph(tmp_path)
    request = LocalizationRequest(
        issue_text=(
            "Expected: parse must preserve an invariant that has no indexed matching guard"
        ),
        repository_root=str(repo),
        graph_db=str(db),
        revision_identity="fixture",
    )

    result = localize_vnext(request)

    assert "invariant" in result.coverage.unresolved
    assert "invariant" not in result.coverage.unavailable


def test_redundant_pass_through_wrapper_is_rejected_with_stable_reason(tmp_path):
    repo, db = _graph(tmp_path)
    (repo / "src" / "wrapper.py").write_text(
        "def parse(value):\n    return JsonParser().parse(value)\n",
        encoding="utf-8",
    )
    wrapper = EvidenceUnit.create(
        file_path="src/wrapper.py",
        symbol="parse",
        start_line=1,
        end_line=2,
        family=EvidenceFamily.GRAPH,
        relation="CALLS",
        confidence=1.0,
        provenance=("fixture",),
        roles=("operation",),
        signal_class="structural",
        signal_rank=1,
    )
    request = replace(_request(repo, db), new_evidence=(wrapper,))

    result = localize_vnext(request)
    decision = next(
        decision
        for decision in result.decisions
        if decision.evidence_id == wrapper.evidence_id
    )

    assert decision.action is CandidateAction.REJECT
    assert ReasonCode.WRAPPER_OR_PASS_THROUGH in decision.reason_codes


def test_live_property_kind_vocabulary_maps_to_behavioral_roles(tmp_path):
    repo, db = _graph(tmp_path)
    con = sqlite3.connect(db)
    con.executemany(
        "INSERT INTO properties VALUES (?,?,?,?,?,?)",
        [
            (2, 4, "guard_clause", "value is None", 8, 1.0),
            (3, 4, "data_flow", "value -> decode", 8, 1.0),
            (4, 4, "return_shape", "None | object", 10, 1.0),
        ],
    )
    con.commit()
    con.close()

    request = _request(repo, db)
    discoveries = discover_candidates(
        request,
        extract_behavior_facets(request),
    )
    roles = {
        role
        for unit in discoveries
        if unit.file_path == "src/parser.py"
        for role in unit.roles
    }

    assert {"invariant", "expected_behavior", "transition"} <= roles


def test_explicit_new_file_path_is_admitted_as_path_only_evidence(tmp_path):
    repo, db = _graph(tmp_path)
    request = _request(
        repo,
        db,
        issue=(
            "Expected: add src/new_parser.py to parse the new wire format. "
            "The new file must preserve malformed-input behavior."
        ),
    )

    result = localize_vnext(request)

    region = next(
        region
        for region in result.admitted_regions
        if region.file_path == "src/new_parser.py"
    )
    assert (region.start_line, region.end_line, region.source_tokens) == (0, 0, 0)
    assert region.selection_reason == "explicit_new_file_path"


def test_config_file_without_symbols_selects_bounded_matched_line_region(tmp_path):
    repo, db = _graph(tmp_path)
    config = repo / "deployment" / "settings.toml"
    config.parent.mkdir(parents=True)
    lines = [f"unrelated_{index} = {index}" for index in range(1, 301)]
    lines[19] = "timeout_seconds_extra = 5"
    lines[149] = "timeout_seconds = 30"
    config.write_text("\n".join(lines) + "\n", encoding="utf-8")
    request = LocalizationRequest(
        issue_text=(
            "In deployment/settings.toml, timeout_seconds must accept a zero value "
            "without falling back to the default."
        ),
        repository_root=str(repo),
        graph_db=str(db),
        revision_identity="fixture",
    )

    result = localize_vnext(request)
    region = next(
        region
        for region in result.admitted_regions
        if region.file_path == "deployment/settings.toml"
    )

    assert region.selection_reason == "config_matched_line_region"
    assert region.start_line <= 150 <= region.end_line
    assert region.line_count <= 9
    assert "configuration" in region.roles


def test_parent_traversal_is_not_normalized_into_an_in_repository_new_file(tmp_path):
    repo, db = _graph(tmp_path)
    request = _request(
        repo,
        db,
        issue="Expected: add ../../outside.py to parse the new wire format.",
    )

    result = localize_vnext(request)

    assert not any(
        region.file_path == "outside.py" for region in result.admitted_regions
    )


def test_vnext_preserves_caller_owned_allocation_tracing(tmp_path):
    repo, db = _graph(tmp_path)
    tracemalloc.start()
    try:
        localize_vnext(_request(repo, db))
        assert tracemalloc.is_tracing()
    finally:
        tracemalloc.stop()


def test_vnext_cleans_up_its_own_tracer_after_failure(tmp_path, monkeypatch):
    from groundtruth.pretask.localization_vnext import engine

    repo, db = _graph(tmp_path)
    if tracemalloc.is_tracing():
        tracemalloc.stop()
    monkeypatch.setattr(
        engine,
        "discover_candidates",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("forced")),
    )

    with pytest.raises(RuntimeError, match="forced"):
        localize_vnext(_request(repo, db))

    assert not tracemalloc.is_tracing()


def test_legacy_projection_preserves_independent_signal_classes_for_fusion(
    tmp_path,
):
    repo, db = _graph(tmp_path)
    request = _request(repo, db)
    discoveries = discover_candidates(
        request,
        extract_behavior_facets(request),
        legacy_discoveries=[
            {
                "path": "src/parser.py",
                "score": 0.8,
                "entered_via": "both",
                "components": {
                    "sem": 0.7,
                    "lex": 0.5,
                    "reach": 0.4,
                    "commit": 0.2,
                },
            }
        ],
    )

    supporting_classes = {
        value
        for unit in discoveries
        for key, value in unit.metadata
        if key == "supporting_signal_classes"
    }

    assert any(
        {"semantic", "structural", "lexical", "history"}
        <= set(value.split(","))
        for value in supporting_classes
    )


def test_loaded_frozen_embedder_scores_structured_symbol_passages(
    tmp_path,
    monkeypatch,
):
    from groundtruth.pretask import graph_localizer

    class FakeEmbedder:
        def encode(self, texts):
            vectors = [[1.0, 0.0]]
            vectors.extend(
                [1.0, 0.2]
                if "exceptions: ParseError" in text
                else [0.1, 1.0]
                for text in texts[1:]
            )
            return vectors

    monkeypatch.setattr(graph_localizer, "_EMBEDDER", FakeEmbedder())
    repo, db = _graph(tmp_path)
    request = _request(repo, db)

    discoveries = discover_candidates(
        request,
        extract_behavior_facets(request),
    )

    assert any(
        "semantic" in dict(unit.metadata).get(
            "supporting_signal_classes",
            "",
        ).split(",")
        for unit in discoveries
    )


def test_behavior_described_issue_without_file_uses_body_and_structured_semantics(
    tmp_path,
    monkeypatch,
):
    from groundtruth.pretask import graph_localizer

    class FakeEmbedder:
        def encode(self, texts):
            vectors = [[1.0, 0.0]]
            vectors.extend(
                [1.0, 0.1]
                if "JsonParser.parse" in text
                else [0.0, 1.0]
                for text in texts[1:]
            )
            return vectors

    monkeypatch.setattr(graph_localizer, "_EMBEDDER", FakeEmbedder())
    repo, db = _graph(tmp_path)
    con = sqlite3.connect(db)
    con.execute(
        "CREATE VIRTUAL TABLE nodes_fts USING "
        "fts5(name, qualified_name, file_path, signature)"
    )
    con.execute(
        """
        INSERT INTO nodes_fts(rowid,name,qualified_name,file_path,signature)
        SELECT id,name,qualified_name,file_path,signature FROM nodes
        WHERE COALESCE(is_test,0)=0
        """
    )
    con.execute(
        "CREATE VIRTUAL TABLE symbol_content_fts USING "
        "fts5(content, tokenize=\"unicode61 tokenchars '_'\")"
    )
    con.execute(
        """
        INSERT INTO symbol_content_fts(rowid,content)
        VALUES (4, 'decode malformed payload empty fallback preserve return')
        """
    )
    con.commit()
    con.close()
    request = LocalizationRequest(
        issue_text=(
            "Empty payloads are decoded as values. Malformed payloads should "
            "be rejected while preserving the fallback return."
        ),
        repository_root=str(repo),
        graph_db=str(db),
        revision_identity="fixture",
    )

    result = localize_vnext(request)

    assert result.facets.issue_mode == "behavior_described"
    assert result.facets.actor == ""
    assert result.facets.architectural_boundary == ""
    assert not any(
        unit.family is EvidenceFamily.EXPLICIT_PATH
        for unit in result.discoveries
    )
    target = [
        unit
        for unit in result.discoveries
        if unit.file_path == "src/parser.py"
        and unit.symbol == "JsonParser.parse"
    ]
    assert target
    classes = {
        signal_class
        for unit in target
        for signal_class in unit.signal_class.split("+")
    }
    assert {"lexical", "semantic"} <= classes
    assert any(
        region.file_path == "src/parser.py"
        and "JsonParser.parse" in region.symbol
        for region in result.admitted_regions
    )
    assert {"operation", "expected_behavior", "parsing"} <= set(
        result.coverage.covered
    )
    assert result.coverage.unresolved == ()


def test_behavior_described_issue_without_retrieval_capability_stays_unresolved(
    tmp_path,
):
    repo, db = _graph(tmp_path)
    request = LocalizationRequest(
        issue_text=(
            "Queued packets should publish atomically when a remote peer "
            "reconnects instead of silently acknowledging the batch."
        ),
        repository_root=str(repo),
        graph_db=str(db),
        revision_identity="fixture",
    )

    result = localize_vnext(request)

    assert result.facets.issue_mode == "behavior_described"
    assert not result.capabilities.available["node_fts"]
    assert not result.capabilities.available["body_fts"]
    assert result.discoveries == ()
    assert result.admitted_regions == ()
    assert result.coverage.covered == ()
    assert (
        set(result.coverage.unresolved) | set(result.coverage.unavailable)
        == set(result.coverage.required)
    )


def test_external_prior_art_paths_are_not_local_architectural_boundaries(
    tmp_path,
):
    repo, db = _graph(tmp_path)
    request = LocalizationRequest(
        issue_text=(
            "FORCE_COLOR should force color output. Prior art: "
            "https://github.com/pytest-dev/pytest/blob/main/"
            "src/_pytest/_io/terminalwriter.py#L43 and "
            "https://github.com/Textualize/rich/blob/main/rich/console.py#L952"
        ),
        repository_root=str(repo),
        graph_db=str(db),
        revision_identity="fixture",
    )

    facets = extract_behavior_facets(request)
    discoveries = discover_candidates(request, facets)

    assert facets.architectural_boundary == ""
    assert facets.issue_mode != "explicit_path"
    assert not any(
        unit.family is EvidenceFamily.EXPLICIT_PATH
        for unit in discoveries
    )


def test_url_path_is_retained_when_it_exists_in_the_current_repository(
    tmp_path,
):
    repo, db = _graph(tmp_path)
    request = LocalizationRequest(
        issue_text=(
            "Update https://github.com/example/project/blob/main/"
            "src/parser.py#L6 so parsing returns None."
        ),
        repository_root=str(repo),
        graph_db=str(db),
        revision_identity="fixture",
    )

    facets = extract_behavior_facets(request)

    assert facets.architectural_boundary == "src/parser.py"
    assert facets.issue_mode == "explicit_path"


def test_structured_semantic_passage_vectors_are_reused_without_changing_output(
    tmp_path,
    monkeypatch,
):
    from groundtruth.pretask import graph_localizer

    repo, db = _graph(tmp_path)
    request = _request(repo, db)

    class CountingEmbedder:
        def __init__(self):
            self.batch_sizes = []

        def encode(self, texts):
            self.batch_sizes.append(len(texts))
            return [
                [1.0, 0.0]
                if text == request.issue_text or "JsonParser.parse" in text
                else [0.0, 1.0]
                for text in texts
            ]

    embedder = CountingEmbedder()
    monkeypatch.setattr(graph_localizer, "_EMBEDDER", embedder)
    original_passages = vnext_engine.build_structured_symbol_passages

    def noisy_passages(*args, **kwargs):
        passages = original_passages(*args, **kwargs)
        passages.update(
            {
                f"vendor/noise_{index}.py::noise_{index}": (
                    f"symbol: noise_{index}\nrole: unrelated"
                )
                for index in range(600)
            }
        )
        return passages

    monkeypatch.setattr(
        vnext_engine,
        "build_structured_symbol_passages",
        noisy_passages,
    )
    facets = extract_behavior_facets(request)

    first = discover_candidates(request, facets)
    second = discover_candidates(request, facets)

    assert embedder.batch_sizes[0] == 1
    assert embedder.batch_sizes.count(1) == 2
    assert all(
        size == 1 or 1 < size <= vnext_engine._SEMANTIC_ENCODE_CHUNK_SIZE
        for size in embedder.batch_sizes
    )
    assert first == second


def test_semantic_passages_use_fixed_chunks_independent_of_cache_history():
    class BatchSensitiveEmbedder:
        def __init__(self):
            self.batch_sizes = []

        def encode(self, texts):
            self.batch_sizes.append(len(texts))
            return [
                [float(len(texts)), float(index)]
                for index, _text in enumerate(texts)
            ]

    embedder = BatchSensitiveEmbedder()
    passages = {
        f"src/module_{index:03d}.py::symbol_{index:03d}": (
            f"symbol: symbol_{index:03d}"
        )
        for index in range(130)
    }

    first = vnext_engine._encode_structured_semantics(
        embedder,
        "query",
        passages,
    )
    second = vnext_engine._encode_structured_semantics(
        embedder,
        "query",
        passages,
    )

    assert embedder.batch_sizes == [1, 64, 64, 2, 1]
    assert first == second


def test_asymmetric_embedder_encodes_every_symbol_as_passage():
    class AsymmetricEmbedder:
        def __init__(self):
            self.query_texts = []
            self.passage_batches = []

        def encode(self, _texts):
            raise AssertionError("generic positional encode must not be used")

        def encode_query(self, text):
            self.query_texts.append(text)
            return [[1.0, 0.0]]

        def encode_passages(self, texts):
            self.passage_batches.append(list(texts))
            return [[0.0, 1.0] for _text in texts]

    embedder = AsymmetricEmbedder()
    passages = {
        f"src/module_{index:03d}.py::symbol_{index:03d}": (
            f"symbol: symbol_{index:03d}"
        )
        for index in range(130)
    }

    query, encoded = vnext_engine._encode_structured_semantics(
        embedder,
        "behavioral issue",
        passages,
    )

    assert query == (1.0, 0.0)
    assert embedder.query_texts == ["behavioral issue"]
    assert [len(batch) for batch in embedder.passage_batches] == [64, 64, 2]
    assert [
        text
        for batch in embedder.passage_batches
        for text in batch
    ] == [passages[key] for key in sorted(passages)]
    assert set(encoded) == set(passages)


def test_onnx_adapter_exposes_non_positional_query_and_passage_modes():
    from groundtruth.pretask.graph_localizer import _OnnxEmbedderAdapter

    class FakeModel:
        dim = 2

        def __init__(self):
            self.calls = []

        def embed(self, text, *, is_query):
            self.calls.append(("one", text, is_query))
            return [1.0, 0.0]

        def embed_batch(self, texts, *, is_query):
            self.calls.append(("batch", tuple(texts), is_query))
            return [[0.0, 1.0] for _text in texts]

    model = FakeModel()
    adapter = _OnnxEmbedderAdapter(model)

    query = adapter.encode_query("issue")
    passages = adapter.encode_passages(["code one", "code two"])

    assert query.tolist() == [[1.0, 0.0]]
    assert passages.tolist() == [[0.0, 1.0], [0.0, 1.0]]
    assert model.calls == [
        ("one", "issue", True),
        ("batch", ("code one", "code two"), False),
    ]


def test_semantic_vector_cache_reuses_repository_sized_corpus():
    class CountingEmbedder:
        def __init__(self):
            self.batch_sizes = []

        def encode(self, texts):
            self.batch_sizes.append(len(texts))
            return [[float(index), 1.0] for index, _text in enumerate(texts)]

    embedder = CountingEmbedder()
    passages = {
        f"src/module_{index:04d}.py::symbol_{index:04d}": (
            f"symbol: symbol_{index:04d}"
        )
        for index in range(600)
    }

    first = vnext_engine._encode_structured_semantics(
        embedder,
        "query",
        passages,
    )
    first_call_count = len(embedder.batch_sizes)
    second = vnext_engine._encode_structured_semantics(
        embedder,
        "query",
        passages,
    )

    assert first == second
    assert embedder.batch_sizes[first_call_count:] == [1]


def test_semantic_vector_cache_obeys_byte_budget(monkeypatch):
    class WideEmbedder:
        def encode(self, texts):
            return [
                [float(index) / 1000.0 for index in range(100)]
                for _text in texts
            ]

    embedder = WideEmbedder()
    monkeypatch.setattr(
        vnext_engine,
        "_SEMANTIC_VECTOR_CACHE_MAX_BYTES",
        2_000,
    )
    passages = {
        f"src/module_{index}.py::symbol_{index}": f"symbol: {index}"
        for index in range(10)
    }

    vnext_engine._encode_structured_semantics(
        embedder,
        "query",
        passages,
    )

    cache = vnext_engine._SEMANTIC_VECTOR_CACHE[embedder]
    assert cache
    assert vnext_engine._SEMANTIC_VECTOR_CACHE_BYTES[embedder] <= 2_000
    assert len(cache) < len(passages)


def test_structured_passage_cache_reuses_graph_and_invalidates_on_change(
    tmp_path,
    monkeypatch,
):
    repo, db = _graph(tmp_path)
    request = _request(repo, db)
    with vnext_engine._STRUCTURED_PASSAGE_CACHE_LOCK:
        vnext_engine._STRUCTURED_PASSAGE_CACHE.clear()
    original_open_graph = vnext_engine._open_graph
    open_count = 0

    def counting_open_graph(graph_db):
        nonlocal open_count
        open_count += 1
        return original_open_graph(graph_db)

    monkeypatch.setattr(
        vnext_engine,
        "_open_graph",
        counting_open_graph,
    )

    first = build_structured_symbol_passages(request)
    second = build_structured_symbol_passages(request)
    assert first == second
    assert open_count == 1

    con = sqlite3.connect(db)
    con.execute(
        """
        INSERT INTO nodes(
            id,label,name,qualified_name,file_path,start_line,end_line,
            signature,language,is_test
        ) VALUES (99,'Function','new_symbol','new_symbol',
                  'src/parser.py',12,12,'new_symbol()','python',0)
        """
    )
    con.commit()
    con.close()

    changed = build_structured_symbol_passages(request)

    assert open_count == 2
    assert "src/parser.py::new_symbol" in changed


def test_structured_passage_cache_obeys_byte_budget(tmp_path, monkeypatch):
    repo, db = _graph(tmp_path)
    request = _request(repo, db)
    with vnext_engine._STRUCTURED_PASSAGE_CACHE_LOCK:
        vnext_engine._STRUCTURED_PASSAGE_CACHE.clear()
    monkeypatch.setattr(
        vnext_engine,
        "_STRUCTURED_PASSAGE_CACHE_MAX_BYTES",
        1,
    )

    assert build_structured_symbol_passages(request)

    with vnext_engine._STRUCTURED_PASSAGE_CACHE_LOCK:
        assert not vnext_engine._STRUCTURED_PASSAGE_CACHE


def test_semantic_near_ties_use_stable_path_symbol_order(tmp_path, monkeypatch):
    from groundtruth.pretask import graph_localizer

    repo, db = _graph(tmp_path)
    request = _request(repo, db)
    facets = extract_behavior_facets(request)

    class JitterEmbedder:
        def __init__(self, query_sign: float) -> None:
            self.query_sign = query_sign

        def encode(self, texts):
            vectors = []
            for text in texts:
                if text == request.issue_text:
                    vectors.append([1.0, self.query_sign * 0.001])
                elif "symbol: JsonParser.parse" in text:
                    vectors.append([1.0, 0.001])
                elif "symbol: BaseParser.parse" in text:
                    vectors.append([1.0, -0.001])
                else:
                    vectors.append([0.0, 1.0])
            return vectors

    def semantic_ranks(query_sign: float) -> dict[str, int]:
        monkeypatch.setattr(
            graph_localizer,
            "_EMBEDDER",
            JitterEmbedder(query_sign),
        )
        con = sqlite3.connect(db)
        con.row_factory = sqlite3.Row
        try:
            units, _node_ids = vnext_engine._node_evidence(
                con,
                facets,
                request,
            )
        finally:
            con.close()
        return {
            unit.symbol: unit.signal_rank
            for unit in units
            if unit.family is EvidenceFamily.SEMANTIC
            and unit.symbol in {"JsonParser.parse", "BaseParser.parse"}
        }

    assert semantic_ranks(1.0) == semantic_ranks(-1.0)


def test_irrelevant_property_facts_do_not_consume_the_candidate_rail(tmp_path):
    repo, db = _graph(tmp_path)
    con = sqlite3.connect(db)
    con.executemany(
        """
        INSERT INTO properties(id,node_id,kind,value,line,confidence)
        VALUES (?,?, 'field_read', ?, ?, 1.0)
        """,
        [
            (1000 + index, 4, f"unrelated_field_{index}", 100 + index)
            for index in range(600)
        ],
    )
    con.execute(
        """
        INSERT INTO properties(id,node_id,kind,value,line,confidence)
        VALUES (2000,4,'boundary_condition','value is malformed',7,1.0)
        """
    )
    con.commit()
    con.close()
    request = _request(repo, db)

    result = localize_vnext(request)
    issue_roles = set(result.facets.required_roles) | set(
        result.facets.expected_roles
    )
    property_units = [
        unit
        for unit in result.discoveries
        if unit.family is EvidenceFamily.PROPERTY
    ]

    assert len(result.discoveries) < request.policy.max_candidates
    assert property_units
    assert all(set(unit.roles) & issue_roles for unit in property_units)


def test_exact_candidate_limit_is_not_reported_as_a_truncated_rail(
    tmp_path, monkeypatch
):
    repo, db = _graph(tmp_path)
    request = replace(
        _request(repo, db),
        policy=LocalizationPolicy(max_candidates=1, max_source_tokens=16_000),
    )
    evidence = EvidenceUnit.create(
        file_path="src/parser.py",
        symbol="JsonParser.parse",
        start_line=6,
        end_line=10,
        family=EvidenceFamily.LEXICAL,
        confidence=1.0,
        provenance=("fixture",),
        roles=("operation",),
        source_tokens=10,
    )
    monkeypatch.setattr(
        vnext_engine,
        "discover_candidates",
        lambda *_args, **_kwargs: [evidence],
    )
    monkeypatch.setattr(
        vnext_engine,
        "_coverage_admit",
        lambda *_args, **_kwargs: (
            (),
            (),
            CoverageState(
                required=("operation",),
                covered=("operation",),
                unresolved=(),
                unavailable=(),
            ),
            "required_roles_covered",
        ),
    )

    result = localize_vnext(request)

    assert len(result.discoveries) == request.policy.max_candidates
    assert result.metrics["candidate_rail_hit"] is False
    assert result.stopping_reason == "required_roles_covered"
    assert result.metrics["stopping_reason"] == "required_roles_covered"


def test_role_certification_is_not_laundered_across_consolidated_signals(tmp_path):
    repo, db = _graph(tmp_path)
    con = sqlite3.connect(db)
    con.execute("DELETE FROM edges")
    con.execute("DELETE FROM properties")
    con.commit()
    con.close()
    request = _request(
        repo,
        db,
        "Parsing must preserve state while JsonParser.parse handles malformed input.",
    )
    strong_identity = EvidenceUnit.create(
        file_path="src/parser.py",
        symbol="JsonParser.parse",
        start_line=6,
        end_line=11,
        family=EvidenceFamily.IDENTIFIER,
        confidence=1.0,
        provenance=("exact_identifier",),
        roles=("operation",),
        signal_class="identifier",
        signal_rank=1,
    )
    weak_transition = EvidenceUnit.create(
        file_path="src/parser.py",
        symbol="JsonParser.parse",
        start_line=6,
        end_line=11,
        family=EvidenceFamily.GRAPH,
        relation="DATA_FLOW",
        confidence=0.5,
        provenance=("name_match",),
        roles=("transition",),
        signal_class="structural",
        signal_rank=1,
    )

    discoveries = discover_candidates(
        request,
        extract_behavior_facets(request),
        legacy_discoveries=(strong_identity, weak_transition),
    )
    target = next(
        unit
        for unit in discoveries
        if unit.file_path == "src/parser.py"
        and unit.symbol == "JsonParser.parse"
        and unit.start_line == 6
        and unit.end_line == 11
    )

    assert "operation" in target.certified_roles
    assert "transition" not in target.certified_roles
    marginal = vnext_engine._marginal(
        target,
        covered=set(),
        required={"operation", "transition"},
        expected=set(),
        role_classes={},
        fused_score=0.0,
    )
    assert marginal[0] == 1


def test_exact_identifier_does_not_claim_observed_behavior(tmp_path):
    repo, db = _graph(tmp_path)
    request = _request(repo, db, "JsonParser.parse returns the wrong value.")

    discoveries = discover_candidates(
        request,
        extract_behavior_facets(request),
    )
    exact = next(
        unit
        for unit in discoveries
        if unit.file_path == "src/parser.py"
        and unit.symbol == "JsonParser.parse"
        and "identifier"
        in dict(unit.metadata)
        .get("supporting_signal_classes", "")
        .split(",")
    )

    assert "operation" in exact.roles
    assert "observed_behavior" not in exact.roles
    assert "observed_behavior" not in exact.certified_roles


def test_incremental_evidence_preserves_prior_state_and_only_resolves_proven_roles(
    tmp_path,
):
    repo, db = _graph(tmp_path)
    runtime = EvidenceUnit.create(
        file_path="src/parser.py",
        symbol="JsonParser.parse",
        start_line=8,
        end_line=8,
        family=EvidenceFamily.TRACEBACK,
        confidence=1.0,
        provenance=("new_runtime_trace",),
        roles=("operation",),
        signal_class="runtime",
        signal_rank=1,
        fact_span=True,
        explicit_provenance=True,
    )
    prior = LocalizationState(
        accepted=("accepted-id",),
        deferred=("deferred-id",),
        unresolved_roles=("operation", "state"),
    )
    request = LocalizationRequest(
        issue_text="",
        repository_root=str(repo),
        graph_db=str(db),
        revision_identity="fixture-rev",
        prior_state=prior,
        new_evidence=(runtime,),
    )

    result = localize_vnext(request)

    assert set(result.coverage.required) == {"operation", "state"}
    assert result.coverage.covered == ("operation",)
    assert result.coverage.unresolved == ("state",)
    assert result.delta is not None
    assert result.delta.newly_resolved_roles == ("operation",)
    assert result.delta.invalidated_evidence == ()
    assert "accepted-id" in result.state.accepted
    assert "deferred-id" in result.state.deferred


def test_negative_evidence_uses_stable_candidate_identity_across_signal_changes(
    tmp_path,
):
    repo, db = _graph(tmp_path)
    issue = "Malformed input should parse without losing state."
    weak_history = EvidenceUnit.create(
        file_path="src/config.py",
        symbol="load_config",
        start_line=1,
        end_line=2,
        family=EvidenceFamily.HISTORY,
        confidence=0.2,
        provenance=("old_history_guess",),
        roles=("state",),
        signal_class="history",
        signal_rank=10,
    )
    first_request = LocalizationRequest(
        issue_text=issue,
        repository_root=str(repo),
        graph_db=str(db),
        revision_identity="fixture-rev",
        new_evidence=(weak_history,),
    )
    first = localize_vnext(first_request)
    assert weak_history.candidate_key
    assert weak_history.candidate_key in first.state.rejected_candidates

    changed_signal = EvidenceUnit.create(
        file_path="src/config.py",
        symbol="load_config",
        start_line=1,
        end_line=2,
        family=EvidenceFamily.LEXICAL,
        confidence=0.8,
        provenance=("new_body_match",),
        roles=("operation", "state"),
        signal_class="lexical",
        signal_rank=1,
        fact_span=True,
    )
    assert changed_signal.evidence_id != weak_history.evidence_id
    assert changed_signal.candidate_key == weak_history.candidate_key

    second = localize_vnext(
        replace(
            first_request,
            prior_state=first.state,
            new_evidence=(changed_signal,),
        )
    )
    changed_decision = next(
        decision
        for decision in second.decisions
        if decision.evidence_id
        == next(
            unit.evidence_id
            for unit in second.discoveries
            if unit.candidate_key == changed_signal.candidate_key
        )
    )
    assert changed_decision.action is CandidateAction.REJECT
    assert ReasonCode.PREVIOUSLY_REJECTED in changed_decision.reason_codes


def test_negative_evidence_is_scoped_to_repository_revision(tmp_path):
    repo, db = _graph(tmp_path)
    candidate = EvidenceUnit.create(
        file_path="src/config.py",
        symbol="load_config",
        start_line=1,
        end_line=2,
        family=EvidenceFamily.LEXICAL,
        confidence=0.8,
        provenance=("new_body_match",),
        roles=("operation",),
        signal_class="lexical",
        signal_rank=1,
    )
    prior = LocalizationState(
        revision_identity="old-revision",
        rejected_candidates=(candidate.candidate_key,),
    )
    request = LocalizationRequest(
        issue_text="load_config returns the wrong value",
        repository_root=str(repo),
        graph_db=str(db),
        revision_identity="new-revision",
        prior_state=prior,
        new_evidence=(candidate,),
    )

    result = localize_vnext(request)
    decision = next(
        item
        for item in result.decisions
        if item.evidence_id
        == next(
            unit.evidence_id
            for unit in result.discoveries
            if unit.candidate_key == candidate.candidate_key
        )
    )

    assert ReasonCode.PREVIOUSLY_REJECTED not in decision.reason_codes
    assert result.state.revision_identity == "new-revision"
    assert candidate.candidate_key not in result.state.rejected_candidates


def test_current_disposition_replaces_prior_disposition_for_same_evidence(tmp_path):
    repo, db = _graph(tmp_path)
    candidate = EvidenceUnit.create(
        file_path="src/parser.py",
        symbol="JsonParser.parse",
        start_line=6,
        end_line=11,
        family=EvidenceFamily.IDENTIFIER,
        confidence=1.0,
        provenance=("exact_identifier",),
        roles=("operation",),
        signal_class="identifier",
        signal_rank=1,
    )
    prior = LocalizationState(
        revision_identity="fixture-rev",
        deferred=(candidate.evidence_id,),
        deferred_candidates=(candidate.candidate_key,),
        evidence_candidates=((candidate.evidence_id, candidate.candidate_key),),
    )
    request = LocalizationRequest(
        issue_text="JsonParser.parse returns the wrong value",
        repository_root=str(repo),
        graph_db=str(db),
        revision_identity="fixture-rev",
        prior_state=prior,
        new_evidence=(candidate,),
    )

    result = localize_vnext(request)

    current = next(
        unit
        for unit in result.discoveries
        if unit.candidate_key == candidate.candidate_key
    )
    assert current.evidence_id in result.state.accepted
    assert candidate.evidence_id not in result.state.deferred
    assert candidate.candidate_key in result.state.accepted_candidates
    assert candidate.candidate_key not in result.state.deferred_candidates


def test_substantive_transition_without_file_is_behavior_described(tmp_path):
    repo, db = _graph(tmp_path)
    request = _request(
        repo,
        db,
        "Inherited colors display a blank legend when themes cascade.",
    )

    facets = extract_behavior_facets(request)
    result = localize_vnext(request)

    assert facets.transition
    assert facets.issue_mode == "behavior_described"
    assert "transition" in result.coverage.required
    assert result.stopping_reason != "insufficient_issue_evidence"


def test_structured_semantics_can_discover_without_lexical_or_fts_seed(
    tmp_path,
    monkeypatch,
):
    from groundtruth.pretask import graph_localizer

    repo, db = _graph(tmp_path)
    issue = "Inherited colors display a blank legend when themes cascade."

    class RepositorySemanticEmbedder:
        def encode(self, texts):
            return [
                [1.0, 0.0]
                if text == issue or "symbol: JsonParser.parse" in text
                else [0.0, 1.0]
                for text in texts
            ]

    monkeypatch.setattr(
        graph_localizer,
        "_EMBEDDER",
        RepositorySemanticEmbedder(),
    )
    request = _request(repo, db, issue)
    result = localize_vnext(request)

    assert any(
        unit.symbol == "JsonParser.parse"
        and "semantic"
        in dict(unit.metadata)
        .get("supporting_signal_classes", "")
        .split(",")
        for unit in result.discoveries
    )


def test_absolute_traceback_resolves_unique_repo_suffix_and_enclosing_symbol(
    tmp_path,
):
    repo, db = _graph(tmp_path)
    absolute = (
        "/home/runner/work/project/repository/src/parser.py"
    )
    request = _request(
        repo,
        db,
        f'File "{absolute}", line 8, in parse\nParseError: malformed',
    )

    result = localize_vnext(request)
    traceback_unit = next(
        unit
        for unit in result.discoveries
        if "traceback" in dict(unit.metadata)
        .get("supporting_families", "")
        .split(",")
    )

    assert traceback_unit.file_path == "src/parser.py"
    assert traceback_unit.symbol == "JsonParser.parse"
    assert traceback_unit.start_line == 8
    assert traceback_unit.end_line == 8
    assert any(
        region.file_path == "src/parser.py"
        and region.start_line <= 8 <= region.end_line
        for region in result.admitted_regions
    )


def test_existing_host_absolute_traceback_is_canonicalized_to_repo_path(
    tmp_path,
):
    repo, db = _graph(tmp_path)
    absolute = str(repo / "src" / "parser.py")
    request = _request(
        repo,
        db,
        f'File "{absolute}", line 8, in parse\nParseError: malformed',
    )

    result = localize_vnext(request)
    traceback_unit = next(
        unit
        for unit in result.discoveries
        if "traceback" in dict(unit.metadata)
        .get("supporting_families", "")
        .split(",")
    )

    assert traceback_unit.file_path == "src/parser.py"
    assert traceback_unit.symbol == "JsonParser.parse"


def test_actual_candidate_truncation_has_rail_precedence(tmp_path):
    repo, db = _graph(tmp_path)
    request = replace(
        _request(repo, db),
        policy=LocalizationPolicy(max_candidates=1, max_source_tokens=16_000),
    )
    first = EvidenceUnit.create(
        file_path="src/parser.py",
        symbol="JsonParser.parse",
        start_line=6,
        end_line=11,
        family=EvidenceFamily.IDENTIFIER,
        confidence=1.0,
        provenance=("first",),
        roles=("operation",),
        signal_class="identifier",
        signal_rank=1,
    )
    second = EvidenceUnit.create(
        file_path="src/config.py",
        symbol="load_config",
        start_line=1,
        end_line=2,
        family=EvidenceFamily.LEXICAL,
        confidence=0.8,
        provenance=("second",),
        roles=("operation",),
        signal_class="lexical",
        signal_rank=2,
    )

    result = localize_vnext(
        request,
        legacy_discoveries=(first, second),
    )

    assert len(result.discoveries) == 1
    assert result.metrics["candidate_rail_hit"] is True
    assert result.stopping_reason == "candidate_rail"


def test_repository_semantic_pool_truncation_reports_candidate_rail(
    tmp_path,
    monkeypatch,
):
    from groundtruth.pretask import graph_localizer

    repo, db = _graph(tmp_path)

    class PositiveEmbedder:
        def encode(self, texts):
            return [[1.0, 0.0] for _text in texts]

    monkeypatch.setattr(graph_localizer, "_EMBEDDER", PositiveEmbedder())
    request = replace(
        _request(
            repo,
            db,
            "Inherited values display incorrectly when state transitions.",
        ),
        policy=LocalizationPolicy(
            max_candidates=1,
            max_source_tokens=16_000,
        ),
    )

    result = localize_vnext(request)

    assert len(result.discoveries) == 1
    assert result.metrics["candidate_rail_hit"] is True
    assert result.stopping_reason == "candidate_rail"


def test_natural_candidate_exhaustion_reports_required_roles_covered(
    tmp_path,
    monkeypatch,
):
    repo, db = _graph(tmp_path)
    request = _request(
        repo,
        db,
        "parse completes",
    )
    sole = EvidenceUnit.create(
        file_path="src/parser.py",
        symbol="JsonParser.parse",
        start_line=6,
        end_line=11,
        family=EvidenceFamily.IDENTIFIER,
        confidence=1.0,
        provenance=("sole_candidate",),
        roles=("operation", "parsing"),
        signal_class="identifier",
        signal_rank=1,
    )
    monkeypatch.setattr(
        vnext_engine,
        "discover_candidates",
        lambda *_args, **_kwargs: [sole],
    )

    result = localize_vnext(request)

    assert result.coverage.unresolved == ()
    assert result.stopping_reason == "required_roles_covered"
