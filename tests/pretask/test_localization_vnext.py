from __future__ import annotations

import math

import hashlib
import json
import sqlite3
import subprocess
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
    # The rail is now what ends delivery, and saying so is the truthful label:
    # admission walks the ranked order and stops at the scale-aware file ceiling
    # rather than at the first zero-marginal candidate.
    assert first.stopping_reason == "admitted_region_rail"
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


def test_redundant_pass_through_wrapper_is_deferred_with_stable_reason(tmp_path):
    """A subsumed pass-through is DEFERRED, not permanently rejected.

    REJECT writes into state.rejected_candidates and poisons every later turn of
    the session; a wrapper that adds nothing THIS turn may matter next turn. The
    redundancy test now also requires evidenced span subsumption - a shared role
    LABEL is true by construction once query broadening runs, so the old test was
    satisfied whenever any second candidate existed.
    """
    repo, db = _graph(tmp_path)
    (repo / "src" / "wrapper.py").write_text(
        "class Outer:\n"
        "    def parse(self, value):\n"
        "        return JsonParser().parse(value)\n"
        "\n"
        "    def check(self, value):\n"
        "        if not value:\n"
        "            raise ValueError(value)\n"
        "        return value\n",
        encoding="utf-8",
    )
    wrapper = EvidenceUnit.create(
        file_path="src/wrapper.py",
        symbol="Outer.parse",
        start_line=2,
        end_line=3,
        family=EvidenceFamily.GRAPH,
        relation="CALLS",
        confidence=1.0,
        provenance=("fixture",),
        roles=("operation",),
        signal_class="structural",
        signal_rank=2,
    )
    # The enclosing class CONTAINS the wrapper span - an actual subsumption
    # witness, which is what the filter now requires instead of a shared label.
    container = EvidenceUnit.create(
        file_path="src/wrapper.py",
        symbol="Outer",
        start_line=1,
        end_line=8,
        family=EvidenceFamily.LEXICAL,
        confidence=0.6,
        provenance=("structured_lexical",),
        roles=("operation",),
        signal_class="lexical",
        signal_rank=1,
    )
    request = replace(_request(repo, db), new_evidence=(wrapper, container))

    result = localize_vnext(request)
    decision = next(
        decision
        for decision in result.decisions
        if decision.evidence_id == wrapper.evidence_id
    )

    assert decision.action is CandidateAction.DEFER
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

    # Structure proves structure: guard_clause -> invariant, data_flow ->
    # transition. `expected_behavior` is a claim about the ISSUE and is reachable
    # only through issue-driven retrieval, never from a typed property.
    assert {"invariant", "transition"} <= roles
    assert "expected_behavior" not in {
        role
        for unit in discoveries
        if unit.file_path == "src/parser.py"
        for role in unit.certified_roles
    }


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
    # slot 3 is `certified`: (contributes, fused_rank, retrieval_rank, certified, ...)
    assert marginal[3] == 1


def test_unrelated_typed_property_cannot_certify_issue_specific_behavior(tmp_path):
    repo, db = _graph(tmp_path)
    request = _request(
        repo,
        db,
        "Malformed payloads should return None instead of raising an exception.",
    )
    facets = extract_behavior_facets(request)
    con = sqlite3.connect(db)
    con.execute("INSERT INTO properties VALUES (2,6,'conditional_return','RETRIES > 0',2,1.0)")
    con.commit()
    con.row_factory = sqlite3.Row
    try:
        emitted = vnext_engine._property_evidence(con, facets, {6})
    finally:
        con.close()

    # The issue demands expected_behavior; a conditional_return guard proves an
    # invariant, which this issue never asked for. The generic fact is therefore
    # not merely uncertified for the issue - it is not evidence for it at all.
    assert "expected_behavior" in facets.required_roles
    assert "invariant" not in facets.required_roles
    assert emitted == []


def test_query_conditioned_candidate_beats_unrelated_generic_fact_in_admission(
    tmp_path,
):
    repo, db = _graph(tmp_path)
    request = _request(
        repo,
        db,
        "Malformed payloads should return None instead of raising an exception.",
    )
    facets = extract_behavior_facets(request)
    # A certified typed fact on an unrelated symbol, carrying only the structural
    # role its span actually proves.
    unrelated = EvidenceUnit.create(
        file_path="src/config.py",
        symbol="load_config",
        start_line=1,
        end_line=2,
        family=EvidenceFamily.PROPERTY,
        confidence=1.0,
        provenance=("properties", "conditional_return", "RETRIES > 0"),
        roles=("invariant",),
        signal_class="property",
        signal_rank=1,
        fact_span=True,
    )
    relevant = EvidenceUnit.create(
        file_path="src/parser.py",
        symbol="JsonParser.parse",
        start_line=6,
        end_line=11,
        family=EvidenceFamily.SEMANTIC,
        confidence=0.85,
        provenance=("query_conditioned_fixture",),
        roles=tuple(facets.required_roles),
        certified_roles=(),
        signal_class="lexical+semantic",
        signal_rank=1,
    )

    decisions, regions, _coverage, _stopping = vnext_engine._coverage_admit(
        request,
        facets,
        (unrelated, relevant),
        census_capabilities(request),
    )
    by_id = {decision.evidence_id: decision for decision in decisions}

    assert by_id[relevant.evidence_id].action is CandidateAction.ADMIT
    assert regions[0].file_path == "src/parser.py"
    assert by_id[unrelated.evidence_id].action is CandidateAction.DEFER
    assert ReasonCode.NO_ISSUE_CONTRIBUTION in by_id[unrelated.evidence_id].reason_codes


def test_certification_is_never_lent_across_units_at_one_region(tmp_path):
    """Certification is a per-unit fact, not a property of a shared span.

    A certified fact that is NOT eligible for this issue sits at the same
    consolidation key as an eligible-but-uncertified row. Consolidation unions
    roles, so the region is eligible - but the certification must not travel,
    or the wrong region wins a `new_mandatory_certified` admit on evidence that
    proved nothing about the issue.
    """
    repo, db = _graph(tmp_path)
    request = replace(
        _request(
            repo,
            db,
            "Malformed payloads should return None instead of raising an exception.",
        ),
        new_evidence=(
            EvidenceUnit.create(
                file_path="src/parser.py",
                symbol="JsonParser.parse",
                start_line=6,
                end_line=11,
                family=EvidenceFamily.SEMANTIC,
                confidence=0.85,
                provenance=("query_conditioned_fixture",),
                roles=("expected_behavior",),
                issue_roles=("expected_behavior",),
                certified_roles=(),
                signal_class="semantic",
                signal_rank=1,
            ),
            EvidenceUnit.create(
                file_path="src/parser.py",
                symbol="JsonParser.parse",
                start_line=6,
                end_line=11,
                family=EvidenceFamily.GRAPH,
                relation="RAISES",
                confidence=1.0,
                provenance=("certified_graph_fixture",),
                roles=("expected_behavior",),
                issue_roles=(),
                signal_class="structural",
                signal_rank=1,
            ),
        ),
    )
    target = next(
        unit
        for unit in discover_candidates(
            request,
            extract_behavior_facets(request),
        )
        if unit.file_path == "src/parser.py"
        and unit.symbol == "JsonParser.parse"
        and unit.start_line == 6
        and unit.end_line == 11
    )

    assert "expected_behavior" in target.issue_roles
    assert "expected_behavior" not in target.certified_roles
    marginal = vnext_engine._marginal(
        target,
        covered=set(),
        required={"expected_behavior"},
        expected=set(),
        role_classes={},
        fused_score=0.0,
    )
    # slot 3 is `certified`: it must be 0, certification was not lent
    assert marginal[3] == 0


def test_incremental_relevant_evidence_resolves_role_after_generic_deferral(
    tmp_path,
):
    repo, db = _graph(tmp_path)
    issue = "Malformed payloads should return None instead of raising an exception."
    generic = EvidenceUnit.create(
        file_path="src/config.py",
        symbol="load_config",
        start_line=1,
        end_line=2,
        family=EvidenceFamily.PROPERTY,
        confidence=1.0,
        provenance=("generic_fact_fixture",),
        roles=("expected_behavior",),
        issue_roles=(),
        signal_class="property",
        signal_rank=1,
        fact_span=True,
    )
    first_request = replace(
        _request(repo, db, issue),
        new_evidence=(generic,),
    )
    first = localize_vnext(first_request)
    relevant = EvidenceUnit.create(
        file_path="src/parser.py",
        symbol="JsonParser.parse",
        start_line=6,
        end_line=11,
        family=EvidenceFamily.SEMANTIC,
        confidence=0.85,
        provenance=("new_search_evidence",),
        roles=("expected_behavior",),
        issue_roles=("expected_behavior",),
        certified_roles=(),
        signal_class="lexical+semantic",
        signal_rank=1,
    )
    second = localize_vnext(
        replace(
            first_request,
            prior_state=first.state,
            new_evidence=(generic, relevant),
        )
    )
    decision = next(item for item in second.decisions if item.evidence_id == relevant.evidence_id)

    assert decision.action is CandidateAction.ADMIT
    assert "expected_behavior" in second.coverage.covered
    assert second.delta is not None
    assert relevant.evidence_id in second.delta.newly_accepted


def test_model_visible_legacy_rank_is_floor_against_uncertified_new_signals(
    tmp_path,
):
    repo, db = _graph(tmp_path)
    (repo / "src" / "legacy.py").write_text(
        "def candidate(value):\n    return value\n",
        encoding="utf-8",
    )
    (repo / "src" / "novel.py").write_text(
        "def possible(value):\n    return value\n",
        encoding="utf-8",
    )
    request = replace(
        _request(
            repo,
            db,
            "Malformed payloads should return None instead of raising an exception.",
        ),
        new_evidence=(
            EvidenceUnit.create(
                file_path="src/novel.py",
                symbol="possible",
                start_line=1,
                end_line=2,
                family=EvidenceFamily.SEMANTIC,
                confidence=0.85,
                provenance=("uncertified_new_signal",),
                roles=("expected_behavior",),
                certified_roles=(),
                signal_class="lexical+semantic+structural",
                signal_rank=1,
            ),
        ),
    )

    discoveries = discover_candidates(
        request,
        extract_behavior_facets(request),
        legacy_discoveries=(
            {
                "path": "src/legacy.py",
                "symbol": "candidate",
                "score": 0.8,
                "components": {"lex": 0.8},
                "legacy_rank": 1,
                "ranking_prior_only": True,
            },
        ),
    )
    order = [unit.file_path for unit in discoveries]

    assert order.index("src/legacy.py") < order.index("src/novel.py")


def test_model_visible_legacy_top_eight_survive_support_only_novel_files(tmp_path):
    repo, db = _graph(tmp_path)
    legacy_paths = [f"src/legacy_{index}.py" for index in range(1, 9)]
    for path in legacy_paths:
        (repo / path).write_text(
            "def candidate(value):\n    return value\n",
            encoding="utf-8",
        )
    novel = []
    for index in range(1, 10):
        path = f"src/novel_{index}.py"
        (repo / path).write_text(
            "def possible(value):\n    return value\n",
            encoding="utf-8",
        )
        novel.append(
            EvidenceUnit.create(
                file_path=path,
                symbol="possible",
                start_line=1,
                end_line=2,
                family=EvidenceFamily.SEMANTIC,
                confidence=0.85,
                provenance=("support_only_novel",),
                roles=("expected_behavior",),
                issue_roles=(),
                certified_roles=(),
                signal_class="lexical+semantic+structural",
                signal_rank=index,
            )
        )
    request = replace(
        _request(
            repo,
            db,
            "Malformed payloads should return None instead of raising an exception.",
        ),
        new_evidence=tuple(novel),
    )
    priors = tuple(
        {
            "path": path,
            "score": 0.5,
            "components": {"lex": 0.5},
            "legacy_rank": rank,
            "ranking_prior_only": True,
        }
        for rank, path in enumerate(legacy_paths, start=1)
    )

    discoveries = discover_candidates(
        request,
        extract_behavior_facets(request),
        legacy_discoveries=priors,
    )
    ranked_files = list(dict.fromkeys(unit.file_path for unit in discoveries))

    assert ranked_files[:8] == legacy_paths


def test_hard_provenance_can_override_model_visible_legacy_rank(tmp_path):
    repo, db = _graph(tmp_path)
    request = replace(
        _request(
            repo,
            db,
            "Malformed payloads should return None instead of raising an exception.",
        ),
        new_evidence=(
            EvidenceUnit.create(
                file_path="src/parser.py",
                symbol="JsonParser.parse",
                start_line=8,
                end_line=8,
                family=EvidenceFamily.TRACEBACK,
                confidence=1.0,
                provenance=("runtime_trace",),
                roles=("observed_behavior",),
                issue_roles=("observed_behavior",),
                signal_class="runtime",
                signal_rank=1,
                fact_span=True,
                explicit_provenance=True,
            ),
        ),
    )
    discoveries = discover_candidates(
        request,
        extract_behavior_facets(request),
        legacy_discoveries=(
            {
                "path": "src/config.py",
                "score": 0.5,
                "components": {"lex": 0.5},
                "legacy_rank": 1,
                "ranking_prior_only": True,
            },
        ),
    )

    assert discoveries[0].file_path == "src/parser.py"
    assert discoveries[0].explicit_provenance is True


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


def test_repository_semantic_pool_is_not_reported_as_a_candidate_rail(
    tmp_path,
    monkeypatch,
):
    """A repository-wide semantic RANKING is not a truncated candidate pool.

    This test previously asserted the opposite - that the semantic leg scoring
    the whole repository reports `candidate_rail`.  That was the defect, not the
    contract: `node_pool_total` was rebuilt as |every symbol whose cosine
    exceeded 0.0|, i.e. the entire repository, so `truncated` measured
    repository SIZE instead of an actual cut and the rail fired on 49/60 cases.
    `score > 0.0` is not a discriminating filter, so the tail of that ranking is
    an ORDER, never a pool that was truncated.

    Real truncation still reports the rail - see
    test_actual_candidate_truncation_has_rail_precedence (region pool) and
    test_a_query_matched_pool_cut_by_the_candidate_cap_is_still_reported
    (node pool).
    """
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
    assert any(
        unit.family is EvidenceFamily.SEMANTIC for unit in result.discoveries
    ), "the semantic leg never ran; the assertion would prove nothing"
    assert result.metrics["candidate_rail_hit"] is False
    assert result.stopping_reason != "candidate_rail"


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


def test_issue_roles_outside_descriptive_roles_do_not_change_identity():
    """Identity must match the normalized state, or state-identical units split."""
    narrow = EvidenceUnit.create(
        file_path="src/parser.py",
        symbol="JsonParser.parse",
        start_line=6,
        end_line=11,
        family=EvidenceFamily.LEXICAL,
        confidence=0.6,
        provenance=("identity_fixture",),
        roles=("expected_behavior",),
        issue_roles=("expected_behavior",),
    )
    overreaching = EvidenceUnit.create(
        file_path="src/parser.py",
        symbol="JsonParser.parse",
        start_line=6,
        end_line=11,
        family=EvidenceFamily.LEXICAL,
        confidence=0.6,
        provenance=("identity_fixture",),
        roles=("expected_behavior",),
        issue_roles=("expected_behavior", "role_not_described_by_this_evidence"),
    )

    assert narrow.issue_roles == overreaching.issue_roles
    assert narrow.evidence_id == overreaching.evidence_id


def test_prior_only_region_is_marked_and_real_evidence_region_is_not(tmp_path):
    """Shadow ranking must stay attributable: mark the legacy-prior-only rows."""
    repo, db = _graph(tmp_path)
    (repo / "src" / "legacy_only.py").write_text(
        "def candidate(value):\n    return value\n",
        encoding="utf-8",
    )
    request = replace(
        _request(
            repo,
            db,
            "Malformed payloads should return None instead of raising an exception.",
        ),
        new_evidence=(
            # Shares the prior's consolidation key AND sorts behind it, so the
            # marker cannot be inherited from whichever row sorts first.
            EvidenceUnit.create(
                file_path="src/parser.py",
                symbol="",
                start_line=0,
                end_line=0,
                family=EvidenceFamily.SEMANTIC,
                confidence=0.5,
                provenance=("shadow_discovery_fixture",),
                roles=("expected_behavior",),
                issue_roles=("expected_behavior",),
                signal_class="semantic",
                signal_rank=9,
            ),
        ),
    )

    discoveries = discover_candidates(
        request,
        extract_behavior_facets(request),
        legacy_discoveries=(
            {
                "path": "src/legacy_only.py",
                "score": 0.5,
                "components": {"lex": 0.5},
                "legacy_rank": 1,
                "ranking_prior_only": True,
            },
            {
                "path": "src/parser.py",
                "score": 0.5,
                "components": {"lex": 0.5},
                "legacy_rank": 2,
                "ranking_prior_only": True,
            },
        ),
    )
    flags = {
        unit.file_path: dict(unit.metadata).get("ranking_prior_only")
        for unit in discoveries
    }

    assert flags["src/legacy_only.py"] == "1"
    assert flags["src/parser.py"] is None


def test_structural_facts_never_claim_the_issue_expected_behavior():
    """Structure proves structure. Only the issue can name expected behavior.

    A typed edge or property proves what the code DOES at that span; it does not
    prove that this is the behavior the issue is asking about. Granting
    `expected_behavior` from pure structure is what let one unrelated certified
    fact close a mandatory role and defer the relevant region as redundant.
    """
    request = LocalizationRequest(
        issue_text="Malformed payloads should return None instead of raising an exception.",
        repository_root=".",
        graph_db="",
        revision_identity="r",
    )
    facets = extract_behavior_facets(request)
    assert "expected_behavior" in facets.required_roles

    def roles(**kwargs):
        return vnext_engine._roles_for(
            facets,
            symbol="unrelated_helper",
            file_path="src/unrelated.py",
            **kwargs,
        )

    for kwargs in (
        {"relation": "RAISES"},
        {"relation": "CATCHES"},
        {"property_kind": "guard"},
        {"property_kind": "boundary_condition"},
        {"property_kind": "conditional_return"},
        {"property_kind": "return_shape"},
        {"property_kind": "exception_type"},
    ):
        assert "expected_behavior" not in roles(**kwargs), kwargs

    # The structural roles themselves survive - this narrows a claim, not a signal.
    # Raising IS control flow, so `transition` is structural truth and must stay;
    # dropping it would silently cost recall on the 32/60 cases that require it.
    assert "exception" in roles(relation="RAISES")
    assert "transition" in roles(relation="RAISES")
    assert "transition" in roles(relation="CATCHES")
    assert "exception" in roles(property_kind="exception_type")
    assert "invariant" in roles(property_kind="guard")
    assert "state" in roles(relation="READS")
    assert "transition" in roles(relation="DATA_FLOW")


def test_ranking_prior_survives_dedup_against_the_same_paths_legacy_row():
    """The floor pin must not share an identity with an ordinary v7.4 row.

    `evidence_id` excludes metadata, confidence and signal_rank, so a prior and
    a v7.4 lexical row for one path were byte-identical: dedup kept the
    higher-confidence v7.4 row and silently deleted the floor.
    """
    request = LocalizationRequest(
        issue_text="Requests to the resolver builder are dropped when the scheme is unknown.",
        repository_root=".",
        graph_db="",
        revision_identity="r",
    )
    facets = extract_behavior_facets(request)
    units = vnext_engine._legacy_evidence(
        [
            {
                "path": "resolver/resolver.go",
                "score": 0.5,
                "components": {"lex": 0.5},
                "legacy_rank": 1,
                "ranking_prior_only": True,
            },
            {"path": "resolver/resolver.go", "score": 0.8, "components": {"lex": 0.8}},
        ],
        facets,
        LocalizationPolicy(),
    )

    assert len({unit.evidence_id for unit in units}) == len(units) == 2
    pinned = [unit for unit in units if dict(unit.metadata).get("ranking_prior_only") == "1"]
    assert len(pinned) == 1
    assert dict(pinned[0].metadata)["legacy_rank"] == "1"


def test_history_never_regrants_roles_the_source_evidence_was_not_eligible_for(tmp_path):
    """Co-change is file-granular support; it must not launder ineligible roles."""
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "thing.py").write_text("x = 1\n", encoding="utf-8")
    for command in (
        ["git", "init", "--quiet"],
        ["git", "config", "user.email", "t@example.com"],
        ["git", "config", "user.name", "t"],
        ["git", "add", "-A"],
        ["git", "commit", "--quiet", "-m", "seed"],
    ):
        if subprocess.run(command, cwd=repo, capture_output=True).returncode:
            pytest.skip("git unavailable")
    request = LocalizationRequest(
        issue_text="Malformed payloads should return None instead of raising an exception.",
        repository_root=str(repo),
        graph_db="",
        revision_identity="r",
    )
    source = EvidenceUnit.create(
        file_path="src/thing.py",
        symbol="thing",
        start_line=1,
        end_line=1,
        family=EvidenceFamily.GRAPH,
        relation="RAISES",
        confidence=1.0,
        provenance=("certified_but_ineligible",),
        roles=("expected_behavior", "exception"),
        issue_roles=("exception",),
        signal_class="structural",
        signal_rank=1,
    )

    produced = vnext_engine._history_evidence(request, (source,))

    # Assert the leg actually fired, or every role assertion below is vacuous.
    assert produced, "history evidence did not fire; the test would prove nothing"
    for unit in produced:
        assert "exception" in unit.roles
        assert "expected_behavior" not in unit.roles
        assert "expected_behavior" not in unit.issue_roles


@pytest.mark.parametrize(
    "dropped",
    [
        "structured_semantics",
        "relation_policy",
        "history",
        "derived_relationships",
        "class_fusion",
        "marginal_coverage",
        "source_regions",
        "behavioral_facets",
    ],
)
def test_engine_degrades_honestly_when_a_capability_is_missing(tmp_path, dropped):
    """Industry-grade generality: no capability may be load-bearing for safety.

    The 60-case corpus has FULL graph capability on every case, so degraded
    regimes are otherwise unexercised. Dropping any single component must keep
    the engine deterministic, leak-free and honest about what it could not
    resolve - never crash, and never claim coverage it did not earn.
    """
    repo, db = _graph(tmp_path)
    issue = "Malformed payloads should return None instead of raising an exception."
    full = localize_vnext(_request(repo, db, issue))
    degraded = localize_vnext(
        replace(
            _request(repo, db, issue),
            policy=LocalizationPolicy(disabled_components=frozenset({dropped})),
        )
    )

    assert degraded.deterministic_hash == localize_vnext(
        replace(
            _request(repo, db, issue),
            policy=LocalizationPolicy(disabled_components=frozenset({dropped})),
        )
    ).deterministic_hash
    assert int(degraded.metrics.get("leakage_count") or 0) == 0
    # Coverage must stay internally consistent: nothing may be reported covered
    # that is not required, and unresolved must be the honest remainder.
    covered = set(degraded.coverage.covered)
    required = set(degraded.coverage.required)
    assert covered <= required
    assert set(degraded.coverage.unresolved) <= required - covered
    # Admitted regions must still be real spans in the repository.
    for region in degraded.admitted_regions:
        assert region.file_path
        assert region.end_line >= region.start_line
    assert degraded.stopping_reason
    assert full.stopping_reason


def test_engine_survives_a_graph_with_no_edges_properties_or_fts(tmp_path):
    """A thin graph is the common real-world case, not an exotic one."""
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "thin.py").write_text(
        "def parse(value):\n    return value\n", encoding="utf-8"
    )
    db = tmp_path / "thin.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY, label TEXT, name TEXT, qualified_name TEXT,
            file_path TEXT, start_line INTEGER, end_line INTEGER, signature TEXT,
            return_type TEXT, is_exported INTEGER, is_test INTEGER, language TEXT,
            parent_id INTEGER
        );
        """
    )
    con.execute(
        "INSERT INTO nodes VALUES (1,'Function','parse','parse','src/thin.py',1,2,"
        "'parse(value)','',1,0,'python',NULL)"
    )
    con.commit()
    con.close()

    result = localize_vnext(
        _request(repo, db, "parse should return None for malformed input.")
    )

    assert int(result.metrics.get("leakage_count") or 0) == 0
    assert result.stopping_reason
    assert set(result.coverage.covered) <= set(result.coverage.required)


def test_query_retrieval_can_cover_expected_behavior_in_every_issue_mode(
    tmp_path, monkeypatch
):
    """`expected_behavior` must be coverable wherever it is required.

    It is a required role in 55/60 corpus cases, and no purely structural fact
    may grant it. If the only source is gated on issue_mode == behavior_described
    then the 27/60 symbol_anchored / explicit_path / traceback cases can never
    satisfy their own required role - the engine starves and stops at the rail
    with expected_behavior unresolved. Measured: gold admission fell 21/52 ->
    16/52 when that was the case.
    """
    repo, db = _graph(tmp_path)
    # A retrieval hit on the gold symbol, identical in both modes.
    monkeypatch.setattr(
        vnext_engine,
        "_fts_candidate_signals",
        lambda con, request: {4: ((EvidenceFamily.BODY_BM25, 1, 9.5),)},
    )

    covered = {}
    for mode, issue in (
        ("symbol_anchored", "JsonParser.parse() should return None for malformed payloads."),
        ("behavior_described", "Malformed payloads should return None instead of raising an exception."),
    ):
        request = _request(repo, db, issue)
        facets = extract_behavior_facets(request)
        assert facets.issue_mode == mode
        assert "expected_behavior" in facets.required_roles
        con = sqlite3.connect(db)
        con.row_factory = sqlite3.Row
        try:
            units, _ids = vnext_engine._node_evidence(con, facets, request)
        finally:
            con.close()
        covered[mode] = any(
            "expected_behavior" in unit.issue_roles
            and unit.confidence < 0.9  # uncertified: it may cover, never certify
            for unit in units
        )

    assert covered["behavior_described"], "regression: the described mode lost its cover"
    assert covered["symbol_anchored"], (
        "a query-retrieved node cannot cover expected_behavior in symbol_anchored "
        "mode, so 27/60 corpus cases can never satisfy their own required role"
    )


def test_admission_prefers_the_more_relevant_region_over_a_labelled_one(tmp_path):
    """Relevance leads; role coverage gates. The MMR shape, not label-first.

    Measured failure this encodes (run 30191986149, ext2_py_geopandas_file_io_driver):
    the engine RANKED gold #1 and then admitted `versioneer.py:1823-1824` - a
    vendored build script - because that span carried a certified role LABEL and
    `fused_rank` sat last in the lexicographic marginal. Coverage then reported
    unresolved=[] and all 493 remaining candidates, gold included, were deferred
    as redundant. Selection, not retrieval, is the defect.
    """
    repo, db = _graph(tmp_path)
    (repo / "vendored.py").write_text("def _v():\n    raise E()\n", encoding="utf-8")
    (repo / "src" / "target.py").write_text(
        "def parse(value):\n    if not value:\n        raise ParseError(value)\n    return value\n",
        encoding="utf-8",
    )
    request = _request(
        repo,
        db,
        "Malformed payloads should return None instead of raising an exception.",
    )
    facets = extract_behavior_facets(request)
    roles = tuple(r for r in facets.required_roles if r not in {"actor"})
    assert roles, "fixture must require at least one coverable role"

    # A tiny vendored span carrying the role LABEL, certified, but retrieved by
    # exactly one weak signal class.
    labelled_noise = EvidenceUnit.create(
        file_path="vendored.py",
        symbol="_v",
        start_line=1,
        end_line=2,
        family=EvidenceFamily.GRAPH,
        relation="RAISES",
        confidence=1.0,
        provenance=("RAISES", "typed"),
        roles=roles,
        # TWO signal classes: this is what slams the `independent_confirmation`
        # escape valve shut (it needs len(role_classes[role]) == 1), which is why
        # gold at rank #1 was deferred as redundant in the real run.
        signal_class="lexical+structural",
        signal_rank=90,
        fact_span=True,
    )
    # The region retrieval actually ranks first, across three independent classes.
    relevant = EvidenceUnit.create(
        file_path="src/target.py",
        symbol="parse",
        start_line=1,
        end_line=4,
        family=EvidenceFamily.BODY_BM25,
        confidence=0.6,
        provenance=("native_body_bm25",),
        roles=roles,
        signal_class="lexical+semantic+identifier",
        signal_rank=1,
    )

    decisions, regions, _coverage, _stop = vnext_engine._coverage_admit(
        request,
        facets,
        # `relevant` FIRST: the fixture's own comment says it "actually ranks
        # first", and discovery order IS the engine's published fused ranking.
        # Listing it second made the tuple contradict the intent under test.
        (relevant, labelled_noise),
        census_capabilities(request),
    )
    by_id = {d.evidence_id: d for d in decisions}

    assert by_id[relevant.evidence_id].action is CandidateAction.ADMIT, (
        "the region retrieval ranked first was not admitted; a labelled vendored "
        "span took the slot"
    )
    assert [r.file_path for r in regions][:1] == ["src/target.py"]


def test_semantic_capability_is_execution_backed_not_file_presence(tmp_path, monkeypatch):
    """A capability may only be reported available if it actually RAN.

    Production failure this encodes: `census_capabilities` derives
    `frozen_semantic` from the presence of an .onnx file on disk
    (engine.py:626-628). Across three sealed runs the embedder encoded ZERO
    passages on 0/60, 16/60 and 8/60 cases while the artifact still reported
    frozen_semantic=True - so roles only the semantic leg could cover were
    reported as an ordinary retrieval miss instead of missing instrumentation,
    and the legacy control arm moved between runs undetected.
    """
    repo, db = _graph(tmp_path)
    models = tmp_path / "models"
    models.mkdir()
    (models / "fake.onnx").write_bytes(b"not a real model")
    monkeypatch.setenv("GT_MODELS_ROOT", str(models))
    # The census must claim it, on file presence alone.
    request = _request(repo, db, "Malformed payloads should return None instead of raising.")
    assert census_capabilities(request).available["frozen_semantic"] is True

    # ... but nothing can load it, so the leg never executes.
    from groundtruth.pretask import graph_localizer as legacy_localizer

    monkeypatch.setattr(legacy_localizer, "_EMBEDDER", None, raising=False)
    result = localize_vnext(request)

    assert int(result.metrics.get("structured_semantic_encoded_count") or 0) == 0
    assert result.capabilities.available["frozen_semantic"] is False, (
        "the run reported a semantic capability that never executed"
    )


def test_explicit_provenance_does_not_swamp_the_relevance_signal():
    """Hard provenance gets its own ranking tier; it must not also flood RRF.

    `fuse_by_evidence_class` adds a FLAT +1.0 for any file with explicit
    provenance, while an RRF class term is capped at 1/(60+1) = 0.0164. Measured
    across 27,536 real regions: explicit median fused 1.032787 vs ordinary
    0.044404 - a 23x gap, worth 61 class-agreements against an achievable max of
    0.079. That was inert while fused_rank was the LAST key in the marginal; once
    relevance leads admission it means any file the issue merely mentions wins the
    slot. Production case held_rust_serde_2950: a whole-file
    `my-binary/src/main.rs:0-0` span carrying only architectural_boundary took the
    admission slot from gold sitting at rank 9.

    region_order already ranks explicit_provenance in its own top tier
    (engine.py region_order slot 0), so the additive bonus is double-counting.
    """
    mentioned = EvidenceUnit.create(
        file_path="my-binary/src/main.rs",
        symbol="",
        start_line=0,
        end_line=0,
        family=EvidenceFamily.LEXICAL,
        confidence=0.6,
        provenance=("issue_path",),
        roles=("architectural_boundary",),
        signal_class="path",
        signal_rank=1,
        explicit_provenance=True,
    )
    # A region four independent retrieval classes agree on - the strongest
    # relevance evidence this scorer can express.
    corroborated = EvidenceUnit.create(
        file_path="serde_derive/src/ser.rs",
        symbol="serialize_body",
        start_line=100,
        end_line=140,
        family=EvidenceFamily.BODY_BM25,
        confidence=0.6,
        provenance=("native_body_bm25",),
        roles=("expected_behavior", "operation"),
        signal_class="lexical+semantic+identifier+structural",
        signal_rank=1,
    )
    fused = fuse_by_evidence_class([mentioned, corroborated])

    assert fused["serde_derive/src/ser.rs"] > fused["my-binary/src/main.rs"], (
        f"a merely-mentioned path outscores four agreeing retrieval classes: "
        f"{fused}"
    )


def _admit_probe(repo, db, issue, units):
    request = _request(repo, db, issue)
    facets = extract_behavior_facets(request)
    decisions, regions, coverage, stopping = vnext_engine._coverage_admit(
        request, facets, tuple(units), census_capabilities(request)
    )
    return {d.evidence_id: d for d in decisions}, regions, coverage, stopping


def test_intra_file_order_follows_retrieval_rank_not_region_size(tmp_path):
    """Within one file, cost must not decide. Relevance must.

    fused_rrf_score is FILE-granular, so every region in a file ties on the
    relevance slot. With nothing below it discriminating, `token_utility` (the
    LAST slot) decides and the SMALLEST span wins - the versioneer pathology one
    level down. MMR (Carbonell & Goldstein 1998) puts relevance first and treats
    cost as a budget, never as a preference.
    """
    repo, db = _graph(tmp_path)
    (repo / "src" / "target.py").write_text(
        "def parse(value):\n"
        "    if not value:\n"
        "        raise ParseError(value)\n"
        "    return decode(value)\n"
        "\n"
        "def helper(v):\n"
        "    return v\n",
        encoding="utf-8",
    )
    issue = "Malformed payloads should return None instead of raising an exception."
    facets = extract_behavior_facets(_request(repo, db, issue))
    roles = tuple(r for r in facets.required_roles if r != "actor")

    ranked_first = EvidenceUnit.create(
        file_path="src/target.py", symbol="parse", start_line=1, end_line=4,
        family=EvidenceFamily.BODY_BM25, confidence=0.6,
        provenance=("native_body_bm25",), roles=roles,
        signal_class="lexical+semantic", signal_rank=1,
    )
    tiny_but_worse = EvidenceUnit.create(
        file_path="src/target.py", symbol="helper", start_line=6, end_line=9,
        family=EvidenceFamily.BODY_BM25, confidence=0.6,
        provenance=("native_body_bm25",), roles=roles,
        signal_class="lexical+semantic", signal_rank=40,
    )

    by_id, regions, _cov, _stop = _admit_probe(repo, db, issue, [tiny_but_worse, ranked_first])

    assert by_id[ranked_first.evidence_id].action is CandidateAction.ADMIT, (
        "the region retrieval ranked FIRST lost its slot to a smaller, worse-ranked "
        "span in the same file"
    )
    assert regions[0].symbol == "parse"


def test_a_region_covering_a_mandatory_role_can_be_admitted(tmp_path):
    """Covering a new required role IS a contribution.

    `contributes` was certified|independent|new_expected|new_fact. For a
    lexical-only region all four are structurally 0: certified needs >=0.9
    confidence, independent needs >=2 signal classes, new_expected covers only
    exception/test_link/alternate_path, new_fact needs relation|fact_span|
    explicit_provenance. So a 0.6-confidence single-class region carrying a
    MANDATORY role could never be admitted, and the reason code
    `no_issue_conditioned_contribution` was false on its face.
    """
    repo, db = _graph(tmp_path)
    (repo / "src" / "only.py").write_text(
        "def parse(value):\n    return decode(value)\n", encoding="utf-8"
    )
    issue = "Malformed payloads should return None instead of raising an exception."
    facets = extract_behavior_facets(_request(repo, db, issue))
    required = tuple(r for r in facets.required_roles if r != "actor")
    assert required, "fixture must require a coverable role"

    lexical_only = EvidenceUnit.create(
        file_path="src/only.py", symbol="parse", start_line=1, end_line=4,
        family=EvidenceFamily.LEXICAL, confidence=0.6,
        provenance=("structured_lexical",), roles=required,
        signal_class="lexical", signal_rank=1,
    )

    by_id, regions, coverage, _stop = _admit_probe(repo, db, issue, [lexical_only])

    assert by_id[lexical_only.evidence_id].action is CandidateAction.ADMIT
    assert regions and regions[0].file_path == "src/only.py"
    assert set(required) & set(coverage.covered)


def test_an_oversized_candidate_is_skipped_not_used_to_end_selection(tmp_path):
    """A budget overflow skips the element; it does not terminate the greedy.

    Budgeted maximum coverage (Khuller, Moss & Naor 1999) skips an element that
    exceeds the remaining budget and continues. `_coverage_admit` used `break`,
    so one oversized candidate deleted every admissible region behind it - and
    stamped them NO_ISSUE_CONTRIBUTION with an all-zero marginal, recording
    "contributed nothing" for candidates that were never evaluated.
    """
    repo, db = _graph(tmp_path)
    (repo / "src" / "huge.py").write_text("\n".join(f"# pad {i}" * 40 for i in range(400)), encoding="utf-8")
    # A real body, not a one-line delegation: _looks_like_pass_through would
    # otherwise REJECT it and the test would prove nothing about the token rail.
    (repo / "src" / "small.py").write_text(
        "def parse(value):\n"
        "    if not value:\n"
        "        raise ParseError(value)\n"
        "    return decode(value)\n",
        encoding="utf-8",
    )
    issue = "Malformed payloads should return None instead of raising an exception."
    facets = extract_behavior_facets(_request(repo, db, issue))
    roles = tuple(r for r in facets.required_roles if r != "actor")

    oversized = EvidenceUnit.create(
        file_path="src/huge.py", symbol="", start_line=1, end_line=400,
        family=EvidenceFamily.LEXICAL, confidence=0.6,
        provenance=("structured_lexical",), roles=roles,
        signal_class="lexical+semantic", signal_rank=1,
    )
    fits = EvidenceUnit.create(
        file_path="src/small.py", symbol="parse", start_line=1, end_line=4,
        family=EvidenceFamily.LEXICAL, confidence=0.6,
        provenance=("structured_lexical",), roles=roles,
        signal_class="lexical+semantic", signal_rank=2,
    )
    request = replace(
        _request(repo, db, issue),
        policy=LocalizationPolicy(max_source_tokens=200, max_region_tokens=100_000),
    )
    decisions, regions, _cov, _stop = vnext_engine._coverage_admit(
        request, facets, (oversized, fits), census_capabilities(request)
    )
    by_id = {d.evidence_id: d for d in decisions}

    assert by_id[fits.evidence_id].action is CandidateAction.ADMIT, (
        "an oversized candidate ended selection and deleted a region that fits"
    )
    assert any(r.file_path == "src/small.py" for r in regions)


def test_exception_handler_evidence_does_not_grant_issue_expected_behavior(tmp_path):
    """A catch block proves control flow, not the issue's expected behavior.

    derive_certified_relationships unconditionally ORed
    {'expected_behavior', 'exception', 'transition'} into the roles of a CATCHES
    handler at confidence >= 0.9, so EvidenceUnit.create auto-certified all three.
    `expected_behavior` is required in 55/60 corpus cases, so one unrelated handler
    could close it certified - the exact laundering the role map was fixed to stop,
    re-entering through a producer that bypasses _roles_for.
    """
    repo, db = _graph(tmp_path)
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO properties VALUES (90,4,'exception_handler','except ParseError as exc',8,1.0)"
    )
    con.commit()
    con.close()
    request = _request(
        repo, db, "JsonParser.parse should return None instead of raising an exception."
    )

    units = vnext_engine.derive_certified_relationships(request)
    handlers = [u for u in units if u.relation == "CATCHES"]

    # Assert the leg FIRED, or every assertion below is vacuous. It was dark:
    # `tokens[-1]` on "except ParseError as exc" yields the alias `exc`, which
    # resolves to no type node, so this producer emitted nothing at all.
    assert handlers, "no CATCHES evidence produced; the assertions would prove nothing"
    for unit in handlers:
        assert "expected_behavior" not in unit.roles, (
            f"a catch handler granted expected_behavior: {unit.file_path} {unit.roles}"
        )
        assert "expected_behavior" not in unit.certified_roles


def test_exact_identifier_certifies_identity_not_broadened_behaviour(tmp_path, monkeypatch):
    """An exact name match proves WHICH symbol, never WHAT it does.

    Exact-identifier node evidence is emitted at confidence 1.0, and
    EvidenceUnit.create auto-certifies every role at >= 0.9. In
    behavior_described mode the role set has already been broadened to the issue's
    full required-role set, so an exact name match CERTIFIED every behavioural
    role the issue asked for - laundering identity into behaviour.
    """
    repo, db = _graph(tmp_path)
    monkeypatch.setattr(
        vnext_engine,
        "_fts_candidate_signals",
        lambda con, request: {4: ((EvidenceFamily.BODY_BM25, 1, 9.5),)},
    )
    request = _request(repo, db, "JsonParser.parse returns the wrong value for malformed payloads.")
    facets = extract_behavior_facets(request)
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        units, _ids = vnext_engine._node_evidence(con, facets, request)
    finally:
        con.close()

    exact = [u for u in units if u.family is EvidenceFamily.IDENTIFIER]
    assert exact, "fixture must produce an exact-identifier unit"
    for unit in exact:
        structural = set(
            vnext_engine._roles_for(facets, symbol=unit.symbol, file_path=unit.file_path)
        )
        assert set(unit.certified_roles) <= structural, (
            f"exact identifier certified roles it never proved: "
            f"{sorted(set(unit.certified_roles) - structural)}"
        )


def test_a_small_real_function_is_not_discarded_for_sharing_a_role_label(tmp_path):
    """The wrapper filter must need evidence of subsumption, not a shared label.

    `redundant_wrappers` discards a pass-through when ANY other non-wrapper
    candidate shares one issue role. Query-driven broadening hands every retrieved
    node the issue's full required-role set, so that condition is true whenever a
    second retrieved node exists - the redundancy test is satisfied by
    construction. It also REJECTs, which is permanent and poisons
    state.rejected_candidates, rather than DEFERring.
    """
    repo, db = _graph(tmp_path)
    (repo / "src" / "accessor.py").write_text(
        "def charset(self):\n    return self._charset\n", encoding="utf-8"
    )
    (repo / "src" / "elsewhere.py").write_text(
        "def other(v):\n    if v:\n        raise ValueError(v)\n    return v\n",
        encoding="utf-8",
    )
    issue = "Malformed payloads should return None instead of raising an exception."
    facets = extract_behavior_facets(_request(repo, db, issue))
    roles = tuple(r for r in facets.required_roles if r != "actor")

    accessor = EvidenceUnit.create(
        file_path="src/accessor.py", symbol="charset", start_line=1, end_line=2,
        family=EvidenceFamily.IDENTIFIER, confidence=1.0, provenance=("nodes", "exact_identifier"),
        roles=roles, signal_class="identifier", signal_rank=1,
    )
    unrelated = EvidenceUnit.create(
        file_path="src/elsewhere.py", symbol="other", start_line=1, end_line=4,
        family=EvidenceFamily.LEXICAL, confidence=0.6, provenance=("structured_lexical",),
        roles=roles, signal_class="lexical", signal_rank=9,
    )
    request = _request(repo, db, issue)
    decisions, _regions, _cov, _stop = vnext_engine._coverage_admit(
        request, facets, (accessor, unrelated), census_capabilities(request)
    )
    by_id = {d.evidence_id: d for d in decisions}

    assert by_id[accessor.evidence_id].action is not CandidateAction.REJECT, (
        "a small real accessor was permanently REJECTED because an unrelated "
        "candidate in a different file happened to carry the same role label"
    )


# ---------------------------------------------------------------------------
# Confirmed-defect repairs (2026-07-26)
# ---------------------------------------------------------------------------


def _covering_unit(file_path, symbol, start, end, roles, signal_class, rank=1):
    return EvidenceUnit.create(
        file_path=file_path,
        symbol=symbol,
        start_line=start,
        end_line=end,
        family=EvidenceFamily.BODY_BM25,
        confidence=0.6,
        provenance=("native_body_bm25",),
        roles=roles,
        signal_class=signal_class,
        signal_rank=rank,
    )


def test_role_classes_is_empty_for_every_uncovered_required_role(
    tmp_path, monkeypatch
):
    """Premise proof for the `independent` repair - it is dead by construction.

    A role enters `covered` and `role_classes` in the SAME admit step, so a role
    that is still in `new_required` (required, not covered) has never had a class
    recorded.  `role_classes[role]` is therefore ALWAYS empty at that point and
    the `len(role_classes[role] | unit_classes) >= 2` term can only ever read the
    candidate's own classes.
    """
    repo, db = _graph(tmp_path)
    (repo / "src" / "target.py").write_text(
        "def parse(value):\n"
        "    if not value:\n"
        "        raise ParseError(value)\n"
        "    return decode(value)\n",
        encoding="utf-8",
    )
    (repo / "src" / "second.py").write_text(
        "def convert(value):\n"
        "    if value is None:\n"
        "        raise ParseError(value)\n"
        "    return value\n",
        encoding="utf-8",
    )
    issue = "Malformed payloads should return None instead of raising an exception."
    facets = extract_behavior_facets(_request(repo, db, issue))
    roles = tuple(r for r in facets.required_roles if r != "actor")
    assert roles, "fixture must require a coverable role"

    seen: list[tuple[str, frozenset]] = []
    real_marginal = vnext_engine._marginal

    def spy(unit, covered, required, expected, role_classes, fused_score):
        for role in (set(unit.issue_roles) & required) - covered:
            seen.append((role, frozenset(role_classes.get(role, set()))))
        return real_marginal(
            unit, covered, required, expected, role_classes, fused_score
        )

    monkeypatch.setattr(vnext_engine, "_marginal", spy)
    request = _request(repo, db, issue)
    vnext_engine._coverage_admit(
        request,
        facets,
        (
            # The rank-1 unit carries ONE role, so a required role remains
            # UNCOVERED after the top-ranked anchor is admitted and the greedy
            # still scores it - without this the spy observes nothing and the
            # assertion below passes vacuously.
            _covering_unit("src/target.py", "parse", 1, 4, roles[:1], "lexical", 1),
            _covering_unit("src/second.py", "convert", 1, 4, roles, "semantic", 2),
        ),
        census_capabilities(request),
    )

    assert seen, "no new-required role was ever scored; the assertion is vacuous"
    assert all(not classes for _role, classes in seen), (
        f"role_classes was non-empty for an uncovered required role: {seen}"
    )


def test_independent_is_corroboration_not_a_second_count_of_role_breadth():
    """`independent` must not scale with how many roles a candidate labels.

    `role_classes[role]` is empty for every role in `new_required` (proved by
    test_role_classes_is_empty_for_every_uncovered_required_role), so the term
    collapsed to `len(new_required)` whenever the candidate carried >= 2 signal
    classes - re-counting the class breadth `fused_rank` already scores and the
    role breadth `contributes`/`new_expected` already carry, and making
    NEW_MANDATORY_INDEPENDENT a claim about ROLES that nothing measured.
    """
    corroborated = _covering_unit(
        "src/target.py",
        "parse",
        1,
        4,
        ("expected_behavior", "operation", "parsing"),
        "lexical+semantic",
    )
    single_class = _covering_unit(
        "src/target.py",
        "parse",
        1,
        4,
        ("expected_behavior", "operation", "parsing"),
        "lexical",
    )
    required = {"expected_behavior", "operation", "parsing"}
    assert len(set(corroborated.issue_roles) & required) == 3, (
        "fixture must offer more than one new mandatory role or the count "
        "collapse is unobservable"
    )

    corroborated_marginal = vnext_engine._marginal(
        corroborated, set(), required, set(), {}, 0.0
    )
    single_marginal = vnext_engine._marginal(
        single_class, set(), required, set(), {}, 0.0
    )

    assert corroborated_marginal[4] == 1, (
        "independent multiplied cross-class corroboration by the number of new "
        f"roles: {corroborated_marginal[4]}"
    )
    assert single_marginal[4] == 0, (
        "a single-class candidate was reported as independently corroborated"
    )


def test_independent_confirmation_ignores_the_first_regions_class_count():
    """The escape valve must not hinge on an accident of the first admission.

    `len(role_classes[role]) == 1` meant a covered role whose FIRST admitted
    region happened to carry two signal classes could never be independently
    confirmed again, while an otherwise identical role covered by a one-class
    region could.  Same candidate, same new class, opposite answer.
    """
    unit = _covering_unit(
        "src/target.py", "parse", 1, 4, ("operation",), "semantic"
    )
    required = {"operation"}
    covered = {"operation"}

    one_class = vnext_engine._marginal(
        unit, covered, required, set(), {"operation": {"lexical"}}, 0.0
    )
    two_classes = vnext_engine._marginal(
        unit, covered, required, set(), {"operation": {"lexical", "structural"}}, 0.0
    )

    assert one_class[6] == 1, "fixture is vacuous: the one-class case never fired"
    assert two_classes[6] == one_class[6], (
        "independent confirmation opened or closed on how many classes the first "
        f"admitted region carried: {two_classes[6]} vs {one_class[6]}"
    )


def test_bounded_region_respects_the_token_rail_that_judges_it(tmp_path):
    """The region builder must bound on MEASURED tokens, not guessed lines.

    `_bounded_region` capped at `max_region_tokens * 4 // 20` LINES - a guess of
    20 chars/line - while the rail that judges the region counts
    `(len(content)+3)//4` TOKENS on the real bytes.  Measured real source is
    ~42.4 chars/line (Python) and ~37.8 (Go), so the engine built regions its own
    rail then refused.
    """
    repo, db = _graph(tmp_path)
    line = "    result = transform(value, option_name, other_option)  # note"
    assert len(line) > 40, "fixture must use realistic line width"
    (repo / "src" / "wide.py").write_text(
        "def transform_all(values):\n" + "\n".join(line for _ in range(200)) + "\n",
        encoding="utf-8",
    )
    request = replace(
        _request(repo, db),
        policy=LocalizationPolicy(
            max_candidates=500, max_source_tokens=16_000, max_region_tokens=200
        ),
    )
    unit = EvidenceUnit.create(
        file_path="src/wide.py",
        symbol="transform_all",
        start_line=1,
        end_line=201,
        family=EvidenceFamily.LEXICAL,
        confidence=0.6,
        provenance=("structured_lexical",),
        roles=("operation",),
        signal_class="lexical",
        signal_rank=1,
    )

    region = vnext_engine._bounded_region(request, unit)

    assert region is not None, "no region was built; the assertion is vacuous"
    assert region.line_count > 1, "fixture collapsed to one line; nothing bounded"
    assert region.source_tokens <= request.policy.max_region_tokens, (
        "the region builder produced a region the region rail rejects: "
        f"{region.source_tokens} tokens > {request.policy.max_region_tokens}"
    )


def test_region_over_the_token_rail_is_deferred_not_permanently_rejected(tmp_path):
    """A budget is a per-turn feasibility constraint, never a permanent verdict.

    REJECT writes `state.rejected_candidates`, which poisons every later turn of
    the session.  A region that cannot be shrunk under THIS turn's
    `max_region_tokens` may fit the next turn's policy, so DEFER is the correct
    action - the same conclusion already reached for `max_source_tokens`.
    """
    repo, db = _graph(tmp_path)
    (repo / "src" / "oneline.py").write_text(
        "def packed(value):\n    return " + " + ".join(["value"] * 400) + "\n",
        encoding="utf-8",
    )
    issue = "Malformed payloads should return None instead of raising an exception."
    facets = extract_behavior_facets(_request(repo, db, issue))
    roles = tuple(r for r in facets.required_roles if r != "actor")
    unbounded = EvidenceUnit.create(
        file_path="src/oneline.py",
        symbol="packed",
        start_line=2,
        end_line=2,
        family=EvidenceFamily.LEXICAL,
        confidence=0.6,
        provenance=("structured_lexical",),
        roles=roles,
        signal_class="lexical",
        signal_rank=1,
    )
    request = replace(
        _request(repo, db, issue),
        policy=LocalizationPolicy(
            max_candidates=500, max_source_tokens=16_000, max_region_tokens=10
        ),
    )
    decisions, _regions, _coverage, _stop = vnext_engine._coverage_admit(
        request, facets, (unbounded,), census_capabilities(request)
    )
    by_id = {d.evidence_id: d for d in decisions}
    decision = by_id[unbounded.evidence_id]

    assert ReasonCode.TOKEN_RAIL in decision.reason_codes, (
        f"the region rail never fired; the assertion is vacuous: {decision}"
    )
    assert decision.action is CandidateAction.DEFER, (
        "an over-budget region was permanently REJECTED into "
        "state.rejected_candidates instead of deferred"
    )


def test_completed_coverage_is_not_relabelled_as_a_source_token_rail(tmp_path):
    """A skipped element is not a stopping reason.

    `source_token_rail_hit` overwrote `stopping_reason` unconditionally, so a run
    that stopped because every required role was covered reported the same string
    as one the budget actually stopped.  The rail must be reported on its own
    channel.
    """
    repo, db = _graph(tmp_path)
    (repo / "src" / "huge.py").write_text(
        "\n".join(f"# pad {i}" * 40 for i in range(400)), encoding="utf-8"
    )
    (repo / "src" / "small.py").write_text(
        "def parse(value):\n"
        "    if not value:\n"
        "        raise ParseError(value)\n"
        "    return decode(value)\n",
        encoding="utf-8",
    )
    issue = "Malformed payloads should return None instead of raising an exception."
    request = replace(
        _request(repo, db, issue),
        policy=LocalizationPolicy(max_source_tokens=200, max_region_tokens=100_000),
    )
    facets = extract_behavior_facets(request)
    roles = tuple(r for r in facets.required_roles if r != "actor")
    oversized = _covering_unit(
        "src/huge.py", "", 1, 400, roles, "lexical+semantic", 1
    )
    fits = _covering_unit(
        "src/small.py", "parse", 1, 4, roles, "lexical+semantic", 2
    )
    decisions, _regions, coverage, stopping = vnext_engine._coverage_admit(
        request, facets, (oversized, fits), census_capabilities(request)
    )

    assert any(
        ReasonCode.TOKEN_RAIL in decision.reason_codes for decision in decisions
    ), "no token-rail skip happened; the assertion is vacuous"
    assert coverage.unresolved == (), (
        f"fixture did not reach full coverage: {coverage}"
    )
    assert stopping == "required_roles_covered", (
        f"a completed run was relabelled by a skipped element: {stopping}"
    )


def test_completed_coverage_is_not_relabelled_as_a_candidate_rail(
    tmp_path, monkeypatch
):
    """A truncated candidate pool is not what ended a run that finished.

    `candidate_rail_hit` overwrote `stopping_reason` unconditionally, discarding
    the reason selection actually ended.  The flag already exists in metrics as
    its own field, so the overwrite was pure information destruction.
    """
    repo, db = _graph(tmp_path)
    (repo / "src" / "target.py").write_text(
        "def parse(value):\n"
        "    if not value:\n"
        "        raise ParseError(value)\n"
        "    return decode(value)\n",
        encoding="utf-8",
    )
    issue = "Malformed payloads should return None instead of raising an exception."
    request = _request(repo, db, issue)
    facets = extract_behavior_facets(request)
    roles = tuple(r for r in facets.required_roles if r != "actor")
    covering = _covering_unit(
        "src/target.py", "parse", 1, 4, roles, "lexical+semantic", 1
    )
    monkeypatch.setattr(
        vnext_engine,
        "discover_candidates",
        lambda *_a, **_k: vnext_engine._DiscoveredCandidates(
            [covering], total_count=99
        ),
    )

    result = localize_vnext(request)

    assert result.metrics["candidate_rail_hit"] is True, (
        "the rail never fired; the assertion is vacuous"
    )
    assert result.coverage.unresolved == (), (
        f"fixture did not reach full coverage: {result.coverage}"
    )
    assert result.stopping_reason == "required_roles_covered", (
        f"a completed run was relabelled by the candidate rail: "
        f"{result.stopping_reason}"
    )


def test_source_token_rail_is_reported_on_its_own_metric_channel(tmp_path):
    """The rail must stay observable once it no longer hijacks stopping_reason."""
    repo, db = _graph(tmp_path)
    request = replace(
        _request(repo, db),
        policy=LocalizationPolicy(
            max_candidates=500, max_source_tokens=1, max_region_tokens=1
        ),
    )

    result = localize_vnext(request)

    assert result.metrics["source_token_rail_hit"] is True
    assert result.coverage.unresolved


def test_lexical_capability_is_execution_backed_not_table_presence(
    tmp_path, monkeypatch
):
    """A capability may only be reported available if it actually RAN.

    `census_capabilities` derives `node_fts`/`body_fts` from table NAMES, while
    `_fts_candidate_signals` swallows every exception and returns `{}`.  A dark
    lexical leg therefore read as an ordinary retrieval miss - exactly the
    failure `frozen_semantic` already carries an execution witness for.
    """
    repo, db = _graph(tmp_path)
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE nodes_fts (node_id INTEGER, name TEXT);
        CREATE TABLE symbol_content_fts (node_id INTEGER, body TEXT);
        """
    )
    con.commit()
    con.close()
    request = _request(repo, db)

    # The census must claim both legs, from table presence alone.
    claimed = census_capabilities(request)
    assert claimed.available["node_fts"] is True
    assert claimed.available["body_fts"] is True

    from groundtruth.pretask import graph_localizer as legacy_localizer

    def _explode(*_args, **_kwargs):
        raise sqlite3.OperationalError("no such module: fts5")

    monkeypatch.setattr(legacy_localizer, "_fts5_candidates", _explode)

    result = localize_vnext(request)

    assert result.capabilities.available["node_fts"] is False, (
        "the run reported a lexical capability that never executed"
    )
    assert (
        result.capabilities.unavailable["node_fts"] == "declared_but_never_executed"
    )
    assert result.capabilities.available["body_fts"] is False
    assert (
        result.capabilities.unavailable["body_fts"] == "declared_but_never_executed"
    )


def test_executed_lexical_legs_stay_available(tmp_path, monkeypatch):
    """The witness must not downgrade a leg that really ran."""
    repo, db = _graph(tmp_path)
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE nodes_fts (node_id INTEGER, name TEXT);
        CREATE TABLE symbol_content_fts (node_id INTEGER, body TEXT);
        """
    )
    con.commit()
    con.close()
    request = _request(repo, db)

    from groundtruth.pretask import graph_localizer as legacy_localizer

    monkeypatch.setattr(
        legacy_localizer,
        "_fts5_candidates",
        lambda *_a, **_k: [(4, "parse", "src/parser.py", 9.5)],
    )
    monkeypatch.setattr(
        legacy_localizer,
        "_content_fts_candidates",
        lambda *_a, **_k: [(4, "parse", "src/parser.py", 8.5)],
    )

    result = localize_vnext(request)

    assert result.capabilities.available["node_fts"] is True
    assert result.capabilities.available["body_fts"] is True


def test_semantic_corpus_scan_is_not_reported_as_a_truncated_pool(
    tmp_path, monkeypatch
):
    """Ranking the whole repository is not a truncated candidate pool.

    `node_pool_total` was replaced by |every symbol whose cosine exceeded 0.0|,
    i.e. the whole repository, so `truncated` became a function of repository
    size rather than of an actual cut - `candidate_rail` fired on 49/60 cases.
    A dense ranker scores the entire corpus by construction; its tail is an
    ORDER, not a pool that was cut.
    """
    from groundtruth.pretask import graph_localizer

    repo, db = _graph(tmp_path)
    con = sqlite3.connect(db)
    con.executemany(
        "INSERT INTO nodes (id,label,name,qualified_name,file_path,start_line,"
        "end_line,signature,return_type,is_exported,is_test,language,parent_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,1,0,'python',NULL)",
        [
            (
                100 + i,
                "Function",
                f"zz{i}",
                f"zz{i}",
                "src/parser.py",
                1,
                2,
                f"zz{i}()",
                "",
            )
            for i in range(60)
        ],
    )
    con.commit()
    con.close()

    class PositiveEmbedder:
        def encode(self, texts):
            return [[1.0, 0.0] for _text in texts]

    monkeypatch.setattr(graph_localizer, "_EMBEDDER", PositiveEmbedder())
    request = replace(
        _request(
            repo, db, "Inherited values display incorrectly when state transitions."
        ),
        policy=LocalizationPolicy(max_candidates=10, max_source_tokens=16_000),
    )
    facets = extract_behavior_facets(request)
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        units, node_ids = vnext_engine._node_evidence(con, facets, request)
        surface = vnext_engine._candidate_node_rows(con, facets, request)
        fts = vnext_engine._fts_candidate_signals(con, request)
    finally:
        con.close()

    assert any(unit.family is EvidenceFamily.SEMANTIC for unit in units), (
        "the semantic leg never ran; the assertion is vacuous"
    )
    assert surface.total_count == 0 and not fts, (
        "fixture must have NO query-matched pool, or the truncation would be real"
    )
    assert len(node_ids) < 66, "fixture must actually drop repository symbols"
    assert units.truncated is False, (
        "a whole-repository semantic ranking was reported as a truncated pool"
    )


def test_a_query_matched_pool_cut_by_the_candidate_cap_is_still_reported(tmp_path):
    """The repair must not silence REAL truncation of a query-matched pool."""
    repo, db = _graph(tmp_path)
    request = replace(
        _request(repo, db, "parse the ParseError raised by JsonParser"),
        policy=LocalizationPolicy(max_candidates=1, max_source_tokens=16_000),
    )
    facets = extract_behavior_facets(request)
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        surface = vnext_engine._candidate_node_rows(con, facets, request)
        units, node_ids = vnext_engine._node_evidence(con, facets, request)
    finally:
        con.close()

    assert surface.total_count > 1, (
        "fixture must match more than one node, or nothing was cut"
    )
    assert len(node_ids) == 1
    assert units.truncated is True, (
        "a query-matched candidate pool cut by max_candidates was not reported"
    )


def test_top_ranked_candidate_is_admitted_even_when_its_roles_are_covered(tmp_path):
    """The best single element must survive coverage-greedy termination.

    Budgeted maximum coverage (Khuller, Moss & Naor 1999) attains its (1-1/e)
    guarantee from `max(greedy_solution, best_single_element)`.  GT shipped the
    greedy arm only, so once a lower-ranked candidate covered the required
    roles the loop stopped at `required_roles_covered` and the top-ranked
    candidate was deferred REDUNDANT - the engine ranked the right file first
    and then declined to deliver it.

    Measured on run 30221830560 (oss-60): the gold file was ranked but dropped
    on 26/60 cases, and on 10 of those it was ranked #1.  Concretely,
    `ext2_go_gozero_rest_engine` ranked gold `rest/engine.go` at #1, admitted
    `rest/server.go` at #3, and stopped.  Role coverage is not the product's
    objective; the edit target is.
    """
    repo, db = _graph(tmp_path)
    (repo / "src" / "engine.py").write_text(
        "def serve(request):\n"
        "    if request is None:\n"
        "        raise ParseError(request)\n"
        "    return dispatch(request)\n",
        encoding="utf-8",
    )
    (repo / "src" / "server.py").write_text(
        "def listen(port):\n"
        "    if port is None:\n"
        "        raise ParseError(port)\n"
        "    return bind(port)\n",
        encoding="utf-8",
    )
    issue = "Malformed payloads should return None instead of raising an exception."
    facets = extract_behavior_facets(_request(repo, db, issue))
    roles = tuple(r for r in facets.required_roles if r != "actor")

    # Same roles on both. The rank-1 unit therefore adds NO new role once the
    # rank-2 unit is admitted - exactly the state that ended the greedy.
    # ONE signal class at rank 1: RRF gives it 1/61 = 0.0164. (A
    # "lexical+semantic" unit would split into two class votes and win outright,
    # which is why the single strongest signal is the case that loses.)
    top = EvidenceUnit.create(
        file_path="src/engine.py", symbol="serve", start_line=1, end_line=4,
        family=EvidenceFamily.LEXICAL, confidence=0.6,
        provenance=("structured_lexical",), roles=roles,
        signal_class="lexical", signal_rank=1,
    )
    # Two INDEPENDENT class agreements on the lower-ranked file. RRF scores it
    # 1/63 + 1/64 = 0.0315 against the rank-1 file's single class 1/61 = 0.0164,
    # so retrieval AGREEMENT outranks the single strongest signal - the real
    # ordering that puts the rank-1 file second in the greedy.
    lower_a = EvidenceUnit.create(
        file_path="src/server.py", symbol="listen", start_line=1, end_line=4,
        family=EvidenceFamily.GRAPH, confidence=0.9,
        provenance=("graph_edge",), roles=roles,
        signal_class="structural", signal_rank=3,
    )
    lower_b = EvidenceUnit.create(
        file_path="src/server.py", symbol="listen", start_line=1, end_line=4,
        family=EvidenceFamily.LEXICAL, confidence=0.9,
        provenance=("structured_lexical",), roles=roles,
        signal_class="lexical", signal_rank=4,
    )
    request = _request(repo, db, issue)

    decisions, regions, _coverage, stopping = vnext_engine._coverage_admit(
        # `top` FIRST: in the real drops the gold file was discovery-#1 and the
        # greedy still deferred it once a fused-higher file covered the roles.
        request, facets, (top, lower_a, lower_b), census_capabilities(request)
    )
    by_id = {d.evidence_id: d for d in decisions}

    assert by_id[top.evidence_id].action is CandidateAction.ADMIT, (
        "the rank-1 candidate was dropped once a lower-ranked one covered the "
        f"roles: {by_id[top.evidence_id].action} "
        f"{[getattr(r, 'value', r) for r in by_id[top.evidence_id].reason_codes]}"
    )
    assert any(region.file_path == "src/engine.py" for region in regions), (
        f"delivered files: {sorted({r.file_path for r in regions})} "
        f"stopping_reason={stopping}"
    )


def test_anchor_follows_the_fused_discovery_order_not_raw_signal_rank(tmp_path):
    """The anchor must be the top of the ranking the engine actually publishes.

    `LocalizationResult.discoveries` IS `tuple(evidence)`, so the incoming
    evidence order is the fused discovery ranking - the same order that becomes
    `ranked_discovery_files`. The anchor re-sorted it by `signal_rank`, a raw
    per-leg retrieval rank that predates fusion, so it anchored on a different
    file than the one the engine ranked first.

    Measured on run 30226219339: the anchor admitted the discovery-#1 file on
    49/60 cases, and on 6 of the remaining cases gold WAS discovery-#1 and was
    still dropped (`ext2_ts_vue_renderer_patch`: discovery #1 `renderer.ts`,
    admitted `Suspense.ts`). That gap is why the anchor recovered 4 of the 10
    rank-1 drops instead of all 10.
    """
    repo, db = _graph(tmp_path)
    (repo / "src" / "engine.py").write_text(
        "def serve(request):\n"
        "    if request is None:\n"
        "        raise ParseError(request)\n"
        "    return dispatch(request)\n",
        encoding="utf-8",
    )
    (repo / "src" / "server.py").write_text(
        "def listen(port):\n"
        "    if port is None:\n"
        "        raise ParseError(port)\n"
        "    return bind(port)\n",
        encoding="utf-8",
    )
    issue = "Malformed payloads should return None instead of raising an exception."
    facets = extract_behavior_facets(_request(repo, db, issue))
    roles = tuple(r for r in facets.required_roles if r != "actor")

    # FIRST in discovery order, but a WORSE raw signal_rank. Fusion promoted it;
    # anchoring on signal_rank would pick the other one.
    discovery_first = EvidenceUnit.create(
        file_path="src/engine.py", symbol="serve", start_line=1, end_line=4,
        family=EvidenceFamily.LEXICAL, confidence=0.6,
        provenance=("structured_lexical",), roles=roles,
        signal_class="lexical", signal_rank=9,
    )
    discovery_second = EvidenceUnit.create(
        file_path="src/server.py", symbol="listen", start_line=1, end_line=4,
        family=EvidenceFamily.GRAPH, confidence=0.9,
        provenance=("graph_edge",), roles=roles,
        signal_class="structural", signal_rank=1,
    )
    request = _request(repo, db, issue)

    decisions, regions, _coverage, _stopping = vnext_engine._coverage_admit(
        request, facets, (discovery_first, discovery_second),
        census_capabilities(request),
    )
    by_id = {d.evidence_id: d for d in decisions}

    assert by_id[discovery_first.evidence_id].reason_codes == (
        ReasonCode.TOP_RANKED_ANCHOR,
    ), (
        "the anchor followed raw signal_rank instead of the published discovery "
        f"order: {[getattr(r, 'value', r) for r in by_id[discovery_first.evidence_id].reason_codes]}"
    )
    assert any(region.file_path == "src/engine.py" for region in regions)


def test_zero_marginal_candidate_is_skipped_not_used_to_end_admission(tmp_path):
    """A covered role ends that CANDIDATE, never the whole admission.

    `_coverage_admit` broke out of the greedy the moment the best remaining
    candidate added no new role, and stamped every candidate behind it DEFER
    without evaluating any of them. Role coverage is a stopping rule for
    coverage; it is not a stopping rule for DELIVERY.

    Measured on run 30226219339 (oss-60, 60 cases): GT admits 1.93 files and
    names the gold file 34/60 = 0.567. Taking the first 2 files off GT's OWN
    ranked list names it 42/60 = 0.700, and the first 3 name it 48/60 = 0.800.
    The admission logic scores 0.121 BELOW blind truncation of its own output
    at the same delivery size - it is destroying value, not adding it. Nine
    dynamic cut rules (score knee, class corroboration, plateau, token budget,
    role count) were measured against that null and none beat it, because the
    RRF fused score is a deterministic function of rank and carries no
    independent confidence to threshold.

    So admission proceeds down the ranked order and stops on the RAILS -
    `max_source_tokens` and the admitted-region cap - which is what bounds it
    on a million-file repository.
    """
    repo, db = _graph(tmp_path)
    for name, fn in (("first", "alpha"), ("second", "beta"), ("third", "gamma")):
        (repo / "src" / f"{name}.py").write_text(
            f"def {fn}(value):\n"
            f"    if not value:\n"
            f"        raise ParseError(value)\n"
            f"    return decode(value)\n",
            encoding="utf-8",
        )
    issue = "Malformed payloads should return None instead of raising an exception."
    facets = extract_behavior_facets(_request(repo, db, issue))
    roles = tuple(r for r in facets.required_roles if r != "actor")

    # All three carry the SAME roles: after the first is admitted the other two
    # have zero marginal role contribution and were both deleted by the break.
    units = tuple(
        EvidenceUnit.create(
            file_path=f"src/{name}.py", symbol=fn, start_line=1, end_line=4,
            family=EvidenceFamily.LEXICAL, confidence=0.6,
            provenance=("structured_lexical",), roles=roles,
            signal_class="lexical", signal_rank=rank,
        )
        for rank, (name, fn) in enumerate(
            (("first", "alpha"), ("second", "beta"), ("third", "gamma")), start=1
        )
    )
    request = _request(repo, db, issue)

    decisions, regions, _coverage, _stopping = vnext_engine._coverage_admit(
        request, facets, units, census_capabilities(request)
    )
    delivered = {region.file_path for region in regions}

    # Three candidate files -> the scale-aware ceiling is ceil(log2(3)) = 2, so
    # the top TWO are delivered. The point under test is that admission did not
    # stop at ONE on a covered role; the ceiling, not the coverage rule, bounds it.
    assert delivered == {"src/first.py", "src/second.py"}, (
        "admission ended on a covered role rather than at the scale-aware "
        f"ceiling: delivered {sorted(delivered)}"
    )


def test_admission_rail_counts_distinct_files_not_regions(tmp_path):
    """The delivery rail must bound FILES, the unit the agent pays attention in.

    Capping admitted REGIONS looked right and measured wrong. On run
    30232420179 the rail bound exactly: 288 ADMITs over 36 cases = 8.00 per
    case. But 2.64 regions land in the SAME file, so 8 regions delivered only
    1.94 distinct files - the delivery size did not move at all (1.92 -> 1.94)
    and delivered-gold stayed at 0.556 against a predicted 0.90.

    Naming a NEW file costs the agent a file to open. A second region inside a
    file it is already reading costs nearly nothing, and is bounded by
    `max_source_tokens` regardless. So the rail counts distinct files.
    """
    repo, db = _graph(tmp_path)
    names = [f"mod{i}" for i in range(12)]
    for name in names:
        (repo / "src" / f"{name}.py").write_text(
            f"def handle_{name}(value):\n"
            f"    if not value:\n"
            f"        raise ParseError(value)\n"
            f"    return decode(value)\n",
            encoding="utf-8",
        )
    issue = "Malformed payloads should return None instead of raising an exception."
    facets = extract_behavior_facets(_request(repo, db, issue))
    roles = tuple(r for r in facets.required_roles if r != "actor")
    units = tuple(
        EvidenceUnit.create(
            file_path=f"src/{name}.py", symbol=f"handle_{name}",
            start_line=1, end_line=4,
            family=EvidenceFamily.LEXICAL, confidence=0.6,
            provenance=("structured_lexical",), roles=roles,
            signal_class="lexical", signal_rank=rank,
        )
        for rank, name in enumerate(names, start=1)
    )
    request = _request(repo, db, issue)

    _decisions, regions, _coverage, _stopping = vnext_engine._coverage_admit(
        request, facets, units, census_capabilities(request)
    )
    delivered = {region.file_path for region in regions}

    # Twelve candidate files -> ceil(log2(12)) = 4. The rail counts FILES, so
    # four DISTINCT files come back; counting regions returned 1.94 on real data
    # because 2.64 regions share a file.
    expected = math.ceil(math.log2(12))
    assert len(delivered) == expected, (
        f"rail delivered {len(delivered)} distinct files, expected {expected} "
        f"= ceil(log2(12)): {sorted(delivered)}"
    )
