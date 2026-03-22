"""Tests for convention + state flow enrichment in IncubatorRuntime.

Real scenarios: obligation results with class targets, shared_state obligations
with state flow graphs, mixed obligations, files that don't exist.
"""

from __future__ import annotations

import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from groundtruth.incubator.runtime import IncubatorRuntime


SAMPLE_CLASS = '''
class UserService:
    def __init__(self, db):
        if db is None:
            raise ValueError("db required")
        self._db = db
        self._cache = {}

    def get_user(self, uid):
        if uid <= 0:
            raise ValueError("invalid uid")
        if uid in self._cache:
            return self._cache[uid]
        user = self._db.query(uid)
        self._cache[uid] = user
        return user

    def save_user(self, user):
        if user is None:
            raise ValueError("user required")
        self._db.save(user)
        self._cache[user.id] = user
'''


@pytest.fixture
def project_dir():
    """Create a temp dir with a Python source file."""
    with tempfile.TemporaryDirectory() as tmpdir:
        src_dir = os.path.join(tmpdir, "src")
        os.makedirs(src_dir)
        with open(os.path.join(src_dir, "service.py"), "w") as f:
            f.write(SAMPLE_CLASS)
        yield tmpdir


@pytest.fixture
def runtime(project_dir):
    store = MagicMock()
    return IncubatorRuntime(store, project_dir)


class TestConventionEnrichment:
    def test_conventions_added_when_flag_on(self, runtime, project_dir) -> None:
        """CONVENTION_FINGERPRINT flag ON → _incubator_conventions added."""
        result = {
            "obligations": [
                {"kind": "shared_state", "target": "UserService.get_user",
                 "file": "src/service.py", "confidence": 0.9},
            ],
        }
        with patch.dict(os.environ, {"GT_ENABLE_CONVENTION_FINGERPRINT": "1"}, clear=True):
            enriched = runtime.enrich("impact", result)

        assert "_incubator_conventions" in enriched
        convs = enriched["_incubator_conventions"]
        assert len(convs) == 1
        assert convs[0]["class"] == "UserService"
        assert convs[0]["file"] == "src/service.py"
        # UserService has guard clauses in every method
        assert convs[0]["guard_clause_freq"] > 0

    def test_conventions_not_added_when_flag_off(self, runtime) -> None:
        """Flag OFF → no _incubator_conventions key."""
        result = {
            "obligations": [
                {"kind": "shared_state", "target": "UserService.get_user",
                 "file": "src/service.py", "confidence": 0.9},
            ],
        }
        with patch.dict(os.environ, {}, clear=True):
            enriched = runtime.enrich("impact", result)

        assert enriched is result  # same object
        assert "_incubator_conventions" not in enriched

    def test_conventions_deduplicates_same_class(self, runtime) -> None:
        """Multiple obligations for same class → one fingerprint."""
        result = {
            "obligations": [
                {"kind": "shared_state", "target": "UserService.get_user",
                 "file": "src/service.py", "confidence": 0.9},
                {"kind": "caller_contract", "target": "UserService.save_user",
                 "file": "src/service.py", "confidence": 0.8},
            ],
        }
        with patch.dict(os.environ, {"GT_ENABLE_CONVENTION_FINGERPRINT": "1"}, clear=True):
            enriched = runtime.enrich("impact", result)

        assert len(enriched["_incubator_conventions"]) == 1

    def test_conventions_handles_missing_file(self, runtime) -> None:
        """Obligation points to nonexistent file → skipped gracefully."""
        result = {
            "obligations": [
                {"kind": "x", "target": "Missing.method",
                 "file": "does/not/exist.py", "confidence": 0.9},
            ],
        }
        with patch.dict(os.environ, {"GT_ENABLE_CONVENTION_FINGERPRINT": "1"}, clear=True):
            enriched = runtime.enrich("impact", result)

        assert enriched.get("_incubator_conventions", []) == []

    def test_conventions_handles_no_obligations(self, runtime) -> None:
        """Result with no obligations → empty conventions."""
        result = {"obligations": [], "contradictions": []}
        with patch.dict(os.environ, {"GT_ENABLE_CONVENTION_FINGERPRINT": "1"}, clear=True):
            enriched = runtime.enrich("impact", result)

        assert enriched.get("_incubator_conventions", []) == []


class TestStateFlowEnrichment:
    def test_state_flow_for_shared_state_obligation(self, runtime) -> None:
        """shared_state obligation → state flow graph added."""
        result = {
            "obligations": [
                {"kind": "shared_state", "target": "UserService.get_user",
                 "file": "src/service.py", "confidence": 0.9},
            ],
        }
        with patch.dict(os.environ, {"GT_ENABLE_STATE_FLOW": "1"}, clear=True):
            enriched = runtime.enrich("impact", result)

        assert "_incubator_state_flow" in enriched
        flows = enriched["_incubator_state_flow"]
        assert len(flows) == 1
        assert flows[0]["class"] == "UserService"
        # _cache and _db should be in the state flow
        attrs = flows[0]["attr_to_methods"]
        assert "_cache" in attrs or "_db" in attrs

    def test_state_flow_ignores_non_shared_state(self, runtime) -> None:
        """caller_contract obligations → no state flow."""
        result = {
            "obligations": [
                {"kind": "caller_contract", "target": "UserService.get_user",
                 "file": "src/service.py", "confidence": 0.9},
            ],
        }
        with patch.dict(os.environ, {"GT_ENABLE_STATE_FLOW": "1"}, clear=True):
            enriched = runtime.enrich("impact", result)

        assert enriched.get("_incubator_state_flow", []) == []

    def test_state_flow_not_added_when_flag_off(self, runtime) -> None:
        result = {
            "obligations": [
                {"kind": "shared_state", "target": "UserService.get_user",
                 "file": "src/service.py", "confidence": 0.9},
            ],
        }
        with patch.dict(os.environ, {}, clear=True):
            enriched = runtime.enrich("impact", result)

        assert "_incubator_state_flow" not in enriched


class TestBothEnrichmentsTogeher:
    def test_both_flags_on(self, runtime) -> None:
        """Both conventions + state flow flags ON → both keys present."""
        result = {
            "obligations": [
                {"kind": "shared_state", "target": "UserService.get_user",
                 "file": "src/service.py", "confidence": 0.9},
            ],
        }
        with patch.dict(os.environ, {
            "GT_ENABLE_CONVENTION_FINGERPRINT": "1",
            "GT_ENABLE_STATE_FLOW": "1",
        }, clear=True):
            enriched = runtime.enrich("impact", result)

        assert "_incubator_conventions" in enriched
        assert "_incubator_state_flow" in enriched

    def test_existing_keys_untouched(self, runtime) -> None:
        """Enrichment must never modify existing keys."""
        result = {
            "obligations": [
                {"kind": "shared_state", "target": "UserService.get_user",
                 "file": "src/service.py", "confidence": 0.9},
            ],
            "total": 1,
            "latency_ms": 5,
        }
        with patch.dict(os.environ, {
            "GT_ENABLE_CONVENTION_FINGERPRINT": "1",
            "GT_ENABLE_STATE_FLOW": "1",
        }, clear=True):
            enriched = runtime.enrich("impact", result)

        assert enriched["total"] == 1
        assert enriched["latency_ms"] == 5
        assert enriched["obligations"] == result["obligations"]
