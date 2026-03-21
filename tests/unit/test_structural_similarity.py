"""Tests for structural similarity — AST feature vectors + Jaccard search."""

from __future__ import annotations

from groundtruth.analysis.structural_similarity import (
    MethodCluster,
    SimilarityResult,
    StructuralFeatures,
    cluster_methods,
    extract_features_from_source,
    extract_method_features,
    find_similar,
)


# ---------------------------------------------------------------------------
# Feature extraction tests
# ---------------------------------------------------------------------------


SAMPLE_CLASS = '''
class Validator:
    def check_name(self, name):
        """Validate name."""
        if not name:
            raise ValueError("required")
        return {"name": name}

    def check_email(self, email):
        if not email:
            raise ValueError("required")
        return {"email": email}

    def transform(self, data):
        result = []
        for item in data:
            result.append(item.upper())
        return result

    def __init__(self, strict=False):
        self.strict = strict
'''


class TestFeatureExtraction:
    def test_extract_from_class(self) -> None:
        features = extract_features_from_source(SAMPLE_CLASS, "test.py", "Validator")
        assert len(features) == 4
        names = {f.name for f in features}
        assert names == {"check_name", "check_email", "transform", "__init__"}

    def test_guard_clause_detected(self) -> None:
        features = extract_features_from_source(SAMPLE_CLASS, "test.py", "Validator")
        check_name = next(f for f in features if f.name == "check_name")
        assert "has_guard_clause" in check_name.features
        assert "has_raise" in check_name.features
        assert "has_return_value" in check_name.features
        assert "has_dict_literal" in check_name.features

    def test_loop_detected(self) -> None:
        features = extract_features_from_source(SAMPLE_CLASS, "test.py", "Validator")
        transform = next(f for f in features if f.name == "transform")
        assert "has_loop" in transform.features
        assert "has_return_value" in transform.features

    def test_self_assign_detected(self) -> None:
        features = extract_features_from_source(SAMPLE_CLASS, "test.py", "Validator")
        init = next(f for f in features if f.name == "__init__")
        assert "has_self_assign" in init.features
        assert "accepts_self" in init.features

    def test_param_count(self) -> None:
        features = extract_features_from_source(SAMPLE_CLASS, "test.py", "Validator")
        check_name = next(f for f in features if f.name == "check_name")
        assert check_name.param_count == 1  # 'name', excluding self

    def test_empty_class(self) -> None:
        code = "class Empty:\n    pass\n"
        features = extract_features_from_source(code, "t.py", "Empty")
        assert features == []

    def test_syntax_error(self) -> None:
        features = extract_features_from_source("def foo(:", "t.py")
        assert features == []

    def test_module_level_functions(self) -> None:
        code = """
def foo(x):
    return x + 1

def bar(x, y):
    if x > y:
        raise ValueError("bad")
    return x - y
"""
        features = extract_features_from_source(code, "t.py")
        assert len(features) == 2


# ---------------------------------------------------------------------------
# Jaccard similarity tests
# ---------------------------------------------------------------------------


class TestJaccardSimilarity:
    def test_identical_features(self) -> None:
        a = StructuralFeatures("a", "f.py", frozenset({"has_return", "has_raise"}), 1, 3)
        b = StructuralFeatures("b", "f.py", frozenset({"has_return", "has_raise"}), 1, 3)
        assert a.jaccard_similarity(b) == 1.0

    def test_no_overlap(self) -> None:
        a = StructuralFeatures("a", "f.py", frozenset({"has_return"}), 1, 3)
        b = StructuralFeatures("b", "f.py", frozenset({"has_loop"}), 1, 3)
        assert a.jaccard_similarity(b) == 0.0

    def test_partial_overlap(self) -> None:
        a = StructuralFeatures("a", "f.py", frozenset({"has_return", "has_raise"}), 1, 3)
        b = StructuralFeatures("b", "f.py", frozenset({"has_return", "has_loop"}), 1, 3)
        # intersection = {has_return}, union = {has_return, has_raise, has_loop}
        assert abs(a.jaccard_similarity(b) - 1 / 3) < 0.01

    def test_empty_features(self) -> None:
        a = StructuralFeatures("a", "f.py", frozenset(), 1, 3)
        b = StructuralFeatures("b", "f.py", frozenset(), 1, 3)
        assert a.jaccard_similarity(b) == 1.0  # same param count

    def test_empty_features_diff_params(self) -> None:
        a = StructuralFeatures("a", "f.py", frozenset(), 1, 3)
        b = StructuralFeatures("b", "f.py", frozenset(), 2, 3)
        assert a.jaccard_similarity(b) == 0.0


# ---------------------------------------------------------------------------
# find_similar tests
# ---------------------------------------------------------------------------


class TestFindSimilar:
    def test_similar_methods_ranked(self) -> None:
        """check_name and check_email should be most similar to each other."""
        features = extract_features_from_source(SAMPLE_CLASS, "test.py", "Validator")
        check_name = next(f for f in features if f.name == "check_name")
        results = find_similar(check_name, features, top_k=3, min_score=0.1)
        assert len(results) > 0
        # check_email should be top match
        assert results[0].target.name == "check_email"
        assert results[0].score > 0.5

    def test_skips_self(self) -> None:
        """A method should not match itself."""
        features = extract_features_from_source(SAMPLE_CLASS, "test.py", "Validator")
        check_name = next(f for f in features if f.name == "check_name")
        results = find_similar(check_name, features, top_k=10, min_score=0.0)
        names = {r.target.name for r in results}
        assert "check_name" not in names

    def test_min_score_filters(self) -> None:
        """High min_score filters out low matches."""
        features = extract_features_from_source(SAMPLE_CLASS, "test.py", "Validator")
        check_name = next(f for f in features if f.name == "check_name")
        results = find_similar(check_name, features, top_k=10, min_score=0.99)
        # Only near-perfect matches pass
        assert len(results) <= 1

    def test_top_k_limits(self) -> None:
        features = extract_features_from_source(SAMPLE_CLASS, "test.py", "Validator")
        check_name = next(f for f in features if f.name == "check_name")
        results = find_similar(check_name, features, top_k=1, min_score=0.0)
        assert len(results) <= 1

    def test_empty_candidates(self) -> None:
        query = StructuralFeatures("q", "f.py", frozenset({"has_return"}), 1, 3)
        results = find_similar(query, [], top_k=5)
        assert results == []


# ---------------------------------------------------------------------------
# Clustering tests
# ---------------------------------------------------------------------------


class TestClustering:
    def test_similar_methods_clustered(self) -> None:
        """check_name and check_email should end up in the same cluster."""
        features = extract_features_from_source(SAMPLE_CLASS, "test.py", "Validator")
        clusters = cluster_methods(features, similarity_threshold=0.5)
        # At least one cluster should contain check_name + check_email
        found = False
        for c in clusters:
            names = {m.name for m in c.members}
            if "check_name" in names and "check_email" in names:
                found = True
                break
        assert found, f"Expected cluster with check_name+check_email, got: {clusters}"

    def test_shared_features_computed(self) -> None:
        features = extract_features_from_source(SAMPLE_CLASS, "test.py", "Validator")
        clusters = cluster_methods(features, similarity_threshold=0.5)
        for c in clusters:
            names = {m.name for m in c.members}
            if "check_name" in names and "check_email" in names:
                assert "has_guard_clause" in c.shared_features
                assert "has_raise" in c.shared_features
                assert "has_return_value" in c.shared_features
                break

    def test_singletons_excluded(self) -> None:
        """Methods that don't match anything should not form clusters."""
        features = extract_features_from_source(SAMPLE_CLASS, "test.py", "Validator")
        clusters = cluster_methods(features, similarity_threshold=0.99)
        for c in clusters:
            assert len(c.members) >= 2

    def test_empty_input(self) -> None:
        assert cluster_methods([], similarity_threshold=0.5) == []

    def test_high_threshold_fewer_clusters(self) -> None:
        features = extract_features_from_source(SAMPLE_CLASS, "test.py", "Validator")
        loose = cluster_methods(features, similarity_threshold=0.3)
        strict = cluster_methods(features, similarity_threshold=0.8)
        # Stricter threshold = same or fewer clusters
        assert len(strict) <= len(loose)


# ---------------------------------------------------------------------------
# Cross-file similarity test
# ---------------------------------------------------------------------------


SERIALIZER_CODE = '''
class UserSerializer:
    def serialize_name(self, user):
        if not user.name:
            raise ValueError("missing name")
        return {"name": user.name}

    def serialize_email(self, user):
        if not user.email:
            raise ValueError("missing email")
        return {"email": user.email}

    def serialize_age(self, user):
        if user.age < 0:
            raise ValueError("invalid age")
        return {"age": user.age}
'''


class TestCrossFileSimilarity:
    def test_cross_class_similarity(self) -> None:
        """Validator.check_name should be similar to UserSerializer.serialize_name."""
        validator_features = extract_features_from_source(SAMPLE_CLASS, "validator.py", "Validator")
        serializer_features = extract_features_from_source(SERIALIZER_CODE, "serializer.py", "UserSerializer")

        check_name = next(f for f in validator_features if f.name == "check_name")
        results = find_similar(check_name, serializer_features, top_k=3, min_score=0.3)

        # All serializer methods follow the same guard+return pattern
        assert len(results) >= 1
        assert results[0].score > 0.5

    def test_cluster_across_files(self) -> None:
        """Guard-clause methods from both classes should cluster together."""
        all_features = (
            extract_features_from_source(SAMPLE_CLASS, "validator.py", "Validator")
            + extract_features_from_source(SERIALIZER_CODE, "serializer.py", "UserSerializer")
        )
        clusters = cluster_methods(all_features, similarity_threshold=0.5)

        # Should find a cluster with guard+raise+return methods from both files
        found_cross_file = False
        for c in clusters:
            files = {m.file_path for m in c.members}
            if len(files) > 1:
                found_cross_file = True
                break
        assert found_cross_file, "Expected cross-file cluster"
