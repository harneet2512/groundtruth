"""Generate the 3D Code City risk map with mock data and open it in the browser."""

from __future__ import annotations

import os
import sys
import webbrowser

from groundtruth.viz import (
    GraphData,
    GraphEdge,
    GraphMetadata,
    GraphNode,
    SymbolInfo,
    render_risk_map,
)
from groundtruth.utils.result import Ok


def _risk_tag(score: float) -> str:
    if score >= 0.7:
        return "CRITICAL"
    if score >= 0.45:
        return "HIGH"
    if score >= 0.25:
        return "MODERATE"
    return "LOW"


def make_mock_graph_data() -> GraphData:
    """Build realistic mock graph data: 15-20 modules, 3-4 directories, cross-deps."""
    nodes = [
        GraphNode(
            id="src/auth/jwt.py",
            label="jwt.py",
            directory="src/auth",
            risk_score=0.82,
            risk_tag=_risk_tag(0.82),
            usage_count=12,
            symbol_count=4,
            risk_factors={"naming_ambiguity": 0.5, "import_depth": 0.3, "parameter_complexity": 0.4},
            symbols=[
                SymbolInfo(name="signToken", kind="function", signature="(payload: dict) -> str", usage_count=8, is_dead=False),
                SymbolInfo(name="decodeToken", kind="function", signature="(token: str) -> TokenPayload", usage_count=4, is_dead=False),
            ],
            imports_from=["src/utils/crypto.py"],
            imported_by=["src/middleware/auth.py", "src/routes/login.py"],
            confusions=[],
            hallucination_rate=None,
            directory_depth=2,
            normalized_height=0.6,
            normalized_width=0.5,
            has_dead_code=False,
        ),
        GraphNode(
            id="src/auth/hash.py",
            label="hash.py",
            directory="src/auth",
            risk_score=0.35,
            risk_tag=_risk_tag(0.35),
            usage_count=5,
            symbol_count=2,
            risk_factors={"naming_ambiguity": 0.2, "import_depth": 0.1},
            symbols=[SymbolInfo(name="hashPassword", kind="function", signature="(pwd: str) -> str", usage_count=5, is_dead=False)],
            imports_from=["src/utils/crypto.py"],
            imported_by=["src/routes/login.py"],
            confusions=[],
            hallucination_rate=None,
            directory_depth=2,
            normalized_height=0.25,
            normalized_width=0.3,
            has_dead_code=False,
        ),
        GraphNode(
            id="src/middleware/auth.py",
            label="auth.py",
            directory="src/middleware",
            risk_score=0.55,
            risk_tag=_risk_tag(0.55),
            usage_count=20,
            symbol_count=1,
            risk_factors={"naming_ambiguity": 0.4, "parameter_complexity": 0.5},
            symbols=[SymbolInfo(name="authMiddleware", kind="function", signature="(req, next) -> Response", usage_count=20, is_dead=False)],
            imports_from=["src/auth/jwt.py"],
            imported_by=["src/routes/users.py", "src/routes/admin.py"],
            confusions=[],
            hallucination_rate=None,
            directory_depth=2,
            normalized_height=1.0,
            normalized_width=0.2,
            has_dead_code=False,
        ),
        GraphNode(
            id="src/routes/login.py",
            label="login.py",
            directory="src/routes",
            risk_score=0.28,
            risk_tag=_risk_tag(0.28),
            usage_count=3,
            symbol_count=2,
            risk_factors={"naming_ambiguity": 0.2},
            symbols=[SymbolInfo(name="loginRoute", kind="function", signature="() -> Router", usage_count=3, is_dead=False)],
            imports_from=["src/auth/jwt.py", "src/auth/hash.py", "src/db/client.py"],
            imported_by=[],
            confusions=[],
            hallucination_rate=None,
            directory_depth=1,
            normalized_height=0.15,
            normalized_width=0.4,
            has_dead_code=False,
        ),
        GraphNode(
            id="src/routes/users.py",
            label="users.py",
            directory="src/routes",
            risk_score=0.48,
            risk_tag=_risk_tag(0.48),
            usage_count=15,
            symbol_count=3,
            risk_factors={"naming_ambiguity": 0.3, "parameter_complexity": 0.4},
            symbols=[SymbolInfo(name="userRoutes", kind="function", signature="() -> Router", usage_count=15, is_dead=False)],
            imports_from=["src/middleware/auth.py", "src/users/queries.py"],
            imported_by=[],
            confusions=[],
            hallucination_rate=None,
            directory_depth=1,
            normalized_height=0.5,
            normalized_width=0.5,
            has_dead_code=False,
        ),
        GraphNode(
            id="src/routes/admin.py",
            label="admin.py",
            directory="src/routes",
            risk_score=0.72,
            risk_tag=_risk_tag(0.72),
            usage_count=8,
            symbol_count=2,
            risk_factors={"naming_ambiguity": 0.6, "import_depth": 0.4},
            symbols=[SymbolInfo(name="adminRoutes", kind="function", signature="() -> Router", usage_count=8, is_dead=False)],
            imports_from=["src/middleware/auth.py", "src/users/queries.py", "src/utils/errors.py"],
            imported_by=[],
            confusions=[],
            hallucination_rate=None,
            directory_depth=1,
            normalized_height=0.4,
            normalized_width=0.4,
            has_dead_code=False,
        ),
        GraphNode(
            id="src/users/queries.py",
            label="queries.py",
            directory="src/users",
            risk_score=0.42,
            risk_tag=_risk_tag(0.42),
            usage_count=25,
            symbol_count=5,
            risk_factors={"naming_ambiguity": 0.3, "parameter_complexity": 0.5},
            symbols=[
                SymbolInfo(name="getUserById", kind="function", signature="(id: int) -> User", usage_count=14, is_dead=False),
                SymbolInfo(name="listUsers", kind="function", signature="() -> list[User]", usage_count=6, is_dead=False),
            ],
            imports_from=["src/db/client.py", "src/utils/errors.py"],
            imported_by=["src/routes/users.py", "src/routes/admin.py"],
            confusions=[],
            hallucination_rate=None,
            directory_depth=1,
            normalized_height=0.8,
            normalized_width=0.6,
            has_dead_code=False,
        ),
        GraphNode(
            id="src/db/client.py",
            label="client.py",
            directory="src/db",
            risk_score=0.18,
            risk_tag=_risk_tag(0.18),
            usage_count=40,
            symbol_count=2,
            risk_factors={"naming_ambiguity": 0.1},
            symbols=[SymbolInfo(name="db", kind="variable", signature="Database", usage_count=40, is_dead=False)],
            imports_from=[],
            imported_by=["src/users/queries.py", "src/routes/login.py"],
            confusions=[],
            hallucination_rate=None,
            directory_depth=1,
            normalized_height=1.0,
            normalized_width=0.2,
            has_dead_code=False,
        ),
        GraphNode(
            id="src/utils/crypto.py",
            label="crypto.py",
            directory="src/utils",
            risk_score=0.22,
            risk_tag=_risk_tag(0.22),
            usage_count=18,
            symbol_count=3,
            risk_factors={"naming_ambiguity": 0.15},
            symbols=[SymbolInfo(name="encrypt", kind="function", signature="(data: bytes) -> bytes", usage_count=18, is_dead=False)],
            imports_from=[],
            imported_by=["src/auth/jwt.py", "src/auth/hash.py"],
            confusions=[],
            hallucination_rate=None,
            directory_depth=1,
            normalized_height=0.6,
            normalized_width=0.4,
            has_dead_code=False,
        ),
        GraphNode(
            id="src/utils/errors.py",
            label="errors.py",
            directory="src/utils",
            risk_score=0.38,
            risk_tag=_risk_tag(0.38),
            usage_count=22,
            symbol_count=4,
            risk_factors={"naming_ambiguity": 0.25},
            symbols=[SymbolInfo(name="NotFoundError", kind="class", signature="", usage_count=11, is_dead=False)],
            imports_from=[],
            imported_by=["src/users/queries.py", "src/routes/admin.py"],
            confusions=[],
            hallucination_rate=None,
            directory_depth=1,
            normalized_height=0.7,
            normalized_width=0.5,
            has_dead_code=False,
        ),
    ]
    edges = [
        GraphEdge(source="src/middleware/auth.py", target="src/auth/jwt.py", edge_type="import"),
        GraphEdge(source="src/routes/login.py", target="src/auth/jwt.py", edge_type="import"),
        GraphEdge(source="src/routes/login.py", target="src/auth/hash.py", edge_type="import"),
        GraphEdge(source="src/routes/login.py", target="src/db/client.py", edge_type="import"),
        GraphEdge(source="src/routes/users.py", target="src/middleware/auth.py", edge_type="import"),
        GraphEdge(source="src/routes/users.py", target="src/users/queries.py", edge_type="import"),
        GraphEdge(source="src/routes/admin.py", target="src/middleware/auth.py", edge_type="import"),
        GraphEdge(source="src/routes/admin.py", target="src/users/queries.py", edge_type="import"),
        GraphEdge(source="src/routes/admin.py", target="src/utils/errors.py", edge_type="import"),
        GraphEdge(source="src/users/queries.py", target="src/db/client.py", edge_type="import"),
        GraphEdge(source="src/users/queries.py", target="src/utils/errors.py", edge_type="import"),
        GraphEdge(source="src/auth/jwt.py", target="src/utils/crypto.py", edge_type="import"),
        GraphEdge(source="src/auth/hash.py", target="src/utils/crypto.py", edge_type="import"),
    ]
    critical = sum(1 for n in nodes if n.risk_score >= 0.7)
    high = sum(1 for n in nodes if 0.45 <= n.risk_score < 0.7)
    moderate = sum(1 for n in nodes if 0.25 <= n.risk_score < 0.45)
    low = sum(1 for n in nodes if n.risk_score < 0.25)
    metadata = GraphMetadata(
        total_files=len(nodes),
        total_symbols=sum(n.symbol_count for n in nodes),
        total_refs=sum(n.usage_count for n in nodes),
        risk_summary={"critical": critical, "high": high, "moderate": moderate, "low": low},
    )
    return GraphData(nodes=nodes, edges=edges, metadata=metadata)


def main() -> None:
    root = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))
    out_path = os.path.join(root, ".groundtruth", "risk_map.html")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    data = make_mock_graph_data()
    result = render_risk_map(data, out_path, theme="dark", bloom=True)
    if not isinstance(result, Ok):
        raise RuntimeError(f"render_risk_map failed: {result.error}")
    path = os.path.abspath(result.value)
    print(f"Risk map written: {path}")
    if sys.platform == "win32":
        os.startfile(path)
    else:
        webbrowser.open("file://" + path)


if __name__ == "__main__":
    main()
