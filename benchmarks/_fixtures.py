"""Shared fixture data for benchmarks and tests.

Extracts the fixture population logic from test_cross_language.py into a
reusable module so both the test suite and the benchmark runner can share
the same symbol/ref/package definitions.
"""

from __future__ import annotations

import time
from typing import Any

from groundtruth.index.store import SymbolStore
from groundtruth.utils.result import Ok


# ---------------------------------------------------------------------------
# Symbol definitions per language
# ---------------------------------------------------------------------------

TS_SYMBOLS: list[dict[str, Any]] = [
    {"name": "getUserById", "kind": "function", "file_path": "src/users/queries.ts",
     "line_number": 5, "end_line": 15, "is_exported": True,
     "signature": "(userId: number) => Promise<User>", "return_type": "Promise<User>",
     "usage_count": 3},
    {"name": "createUser", "kind": "function", "file_path": "src/users/queries.ts",
     "line_number": 17, "end_line": 25, "is_exported": True,
     "signature": "(input: CreateUserInput) => Promise<User>", "return_type": "Promise<User>",
     "usage_count": 2},
    {"name": "updateUser", "kind": "function", "file_path": "src/users/queries.ts",
     "line_number": 27, "end_line": 35, "is_exported": True,
     "signature": "(userId: number, data: UpdateUserInput) => Promise<User>",
     "return_type": "Promise<User>", "usage_count": 1},
    {"name": "deleteUser", "kind": "function", "file_path": "src/users/queries.ts",
     "line_number": 37, "end_line": 42, "is_exported": True,
     "signature": "(userId: number) => Promise<void>", "return_type": "Promise<void>",
     "usage_count": 1},
    {"name": "User", "kind": "interface", "file_path": "src/users/types.ts",
     "line_number": 1, "end_line": 8, "is_exported": True, "usage_count": 5},
    {"name": "NotFoundError", "kind": "class", "file_path": "src/utils/errors.ts",
     "line_number": 10, "end_line": 18, "is_exported": True, "usage_count": 2},
    {"name": "AppError", "kind": "class", "file_path": "src/utils/errors.ts",
     "line_number": 1, "end_line": 9, "is_exported": True, "usage_count": 3},
    {"name": "hashPassword", "kind": "function", "file_path": "src/utils/crypto.ts",
     "line_number": 1, "end_line": 5, "is_exported": True,
     "signature": "(password: string) => Promise<string>", "usage_count": 1},
    {"name": "comparePassword", "kind": "function", "file_path": "src/utils/crypto.ts",
     "line_number": 7, "end_line": 12, "is_exported": True,
     "signature": "(password: string, hash: string) => Promise<boolean>", "usage_count": 1},
    {"name": "generateSalt", "kind": "function", "file_path": "src/utils/crypto.ts",
     "line_number": 14, "end_line": 18, "is_exported": True,
     "signature": "(rounds: number) => string", "usage_count": 0},
    {"name": "validateEmail", "kind": "function", "file_path": "src/utils/validation.ts",
     "line_number": 1, "end_line": 5, "is_exported": True,
     "signature": "(email: string) => boolean", "usage_count": 1},
    {"name": "validatePassword", "kind": "function", "file_path": "src/utils/validation.ts",
     "line_number": 7, "end_line": 12, "is_exported": True,
     "signature": "(password: string) => boolean", "usage_count": 1},
    {"name": "sanitizeInput", "kind": "function", "file_path": "src/utils/validation.ts",
     "line_number": 14, "end_line": 18, "is_exported": True,
     "signature": "(input: string) => string", "usage_count": 0},
    {"name": "authMiddleware", "kind": "function", "file_path": "src/middleware/auth.ts",
     "line_number": 1, "end_line": 10, "is_exported": True, "usage_count": 2},
    {"name": "errorHandler", "kind": "function", "file_path": "src/middleware/errorHandler.ts",
     "line_number": 1, "end_line": 8, "is_exported": True,
     "signature": "(err: Error, req: Request, res: Response) => void", "usage_count": 1},
    {"name": "db", "kind": "variable", "file_path": "src/db/client.ts",
     "line_number": 1, "end_line": 5, "is_exported": True, "usage_count": 4},
    {"name": "formatLegacyDate", "kind": "function", "file_path": "src/utils/dates.ts",
     "line_number": 1, "end_line": 5, "is_exported": True, "usage_count": 0},
    {"name": "login", "kind": "function", "file_path": "src/auth/login.ts",
     "line_number": 1, "end_line": 10, "is_exported": True,
     "signature": "(email: string, password: string) => Promise<AuthResult>", "usage_count": 2},
    {"name": "logout", "kind": "function", "file_path": "src/auth/login.ts",
     "line_number": 12, "end_line": 18, "is_exported": True,
     "signature": "(sessionId: string) => Promise<void>", "usage_count": 1},
    {"name": "signToken", "kind": "function", "file_path": "src/auth/jwt.ts",
     "line_number": 1, "end_line": 8, "is_exported": True,
     "signature": "(payload: TokenPayload) => string", "usage_count": 1},
    {"name": "verifyToken", "kind": "function", "file_path": "src/auth/jwt.ts",
     "line_number": 10, "end_line": 16, "is_exported": True,
     "signature": "(token: string) => TokenPayload", "usage_count": 1},
    {"name": "decodeToken", "kind": "function", "file_path": "src/auth/jwt.ts",
     "line_number": 18, "end_line": 24, "is_exported": True,
     "signature": "(token: string) => TokenPayload", "usage_count": 0},
]

PY_SYMBOLS: list[dict[str, Any]] = [
    {"name": "get_user_by_id", "kind": "function", "file_path": "src/users/queries.py",
     "line_number": 5, "end_line": 15, "is_exported": True,
     "signature": "(user_id: int) -> User", "return_type": "User",
     "usage_count": 3},
    {"name": "create_user", "kind": "function", "file_path": "src/users/queries.py",
     "line_number": 17, "end_line": 25, "is_exported": True,
     "signature": "(input: CreateUserInput) -> User", "return_type": "User",
     "usage_count": 2},
    {"name": "update_user", "kind": "function", "file_path": "src/users/queries.py",
     "line_number": 27, "end_line": 35, "is_exported": True,
     "signature": "(user_id: int, data: dict) -> User", "return_type": "User",
     "usage_count": 1},
    {"name": "delete_user", "kind": "function", "file_path": "src/users/queries.py",
     "line_number": 37, "end_line": 42, "is_exported": True,
     "signature": "(user_id: int) -> None", "return_type": "None",
     "usage_count": 1},
    {"name": "User", "kind": "class", "file_path": "src/users/types.py",
     "line_number": 1, "end_line": 8, "is_exported": True, "usage_count": 5},
    {"name": "NotFoundError", "kind": "class", "file_path": "src/utils/errors.py",
     "line_number": 10, "end_line": 18, "is_exported": True, "usage_count": 2},
    {"name": "AppError", "kind": "class", "file_path": "src/utils/errors.py",
     "line_number": 1, "end_line": 9, "is_exported": True, "usage_count": 3},
    {"name": "hash_password", "kind": "function", "file_path": "src/utils/crypto.py",
     "line_number": 1, "end_line": 5, "is_exported": True,
     "signature": "(password: str) -> str", "usage_count": 1},
    {"name": "compare_password", "kind": "function", "file_path": "src/utils/crypto.py",
     "line_number": 7, "end_line": 12, "is_exported": True,
     "signature": "(password: str, hashed: str) -> bool", "usage_count": 1},
    {"name": "generate_salt", "kind": "function", "file_path": "src/utils/crypto.py",
     "line_number": 14, "end_line": 18, "is_exported": True,
     "signature": "(rounds: int) -> str", "usage_count": 0},
    {"name": "validate_email", "kind": "function", "file_path": "src/utils/validation.py",
     "line_number": 1, "end_line": 5, "is_exported": True,
     "signature": "(email: str) -> bool", "usage_count": 1},
    {"name": "validate_password", "kind": "function", "file_path": "src/utils/validation.py",
     "line_number": 7, "end_line": 12, "is_exported": True,
     "signature": "(password: str) -> bool", "usage_count": 1},
    {"name": "sanitize_input", "kind": "function", "file_path": "src/utils/validation.py",
     "line_number": 14, "end_line": 18, "is_exported": True,
     "signature": "(text: str) -> str", "usage_count": 0},
    {"name": "auth_middleware", "kind": "function", "file_path": "src/middleware/auth.py",
     "line_number": 1, "end_line": 10, "is_exported": True, "usage_count": 2},
    {"name": "db", "kind": "variable", "file_path": "src/db/client.py",
     "line_number": 1, "end_line": 5, "is_exported": True, "usage_count": 4},
    {"name": "format_legacy_date", "kind": "function", "file_path": "src/utils/dates.py",
     "line_number": 1, "end_line": 5, "is_exported": True, "usage_count": 0},
    {"name": "login", "kind": "function", "file_path": "src/auth/login.py",
     "line_number": 1, "end_line": 10, "is_exported": True,
     "signature": "(email: str, password: str) -> AuthResult", "usage_count": 2},
    {"name": "logout", "kind": "function", "file_path": "src/auth/login.py",
     "line_number": 12, "end_line": 18, "is_exported": True,
     "signature": "(session_id: str) -> None", "usage_count": 1},
    {"name": "sign_token", "kind": "function", "file_path": "src/auth/jwt.py",
     "line_number": 1, "end_line": 8, "is_exported": True,
     "signature": "(payload: dict) -> str", "usage_count": 1},
    {"name": "verify_token", "kind": "function", "file_path": "src/auth/jwt.py",
     "line_number": 10, "end_line": 16, "is_exported": True,
     "signature": "(token: str) -> dict", "usage_count": 1},
    {"name": "decode_token", "kind": "function", "file_path": "src/auth/jwt.py",
     "line_number": 18, "end_line": 24, "is_exported": True,
     "signature": "(token: str) -> dict", "usage_count": 0},
]

GO_SYMBOLS: list[dict[str, Any]] = [
    {"name": "GetUserByID", "kind": "function", "file_path": "users/queries.go",
     "line_number": 5, "end_line": 15, "is_exported": True,
     "signature": "(userID int) (*User, error)", "return_type": "*User",
     "usage_count": 3},
    {"name": "CreateUser", "kind": "function", "file_path": "users/queries.go",
     "line_number": 17, "end_line": 25, "is_exported": True,
     "signature": "(input CreateUserInput) (*User, error)", "return_type": "*User",
     "usage_count": 2},
    {"name": "User", "kind": "type", "file_path": "users/types.go",
     "line_number": 1, "end_line": 8, "is_exported": True, "usage_count": 5},
    {"name": "NotFoundError", "kind": "type", "file_path": "utils/errors.go",
     "line_number": 10, "end_line": 18, "is_exported": True, "usage_count": 2},
    {"name": "AppError", "kind": "type", "file_path": "utils/errors.go",
     "line_number": 1, "end_line": 9, "is_exported": True, "usage_count": 3},
    {"name": "HashPassword", "kind": "function", "file_path": "utils/crypto.go",
     "line_number": 1, "end_line": 5, "is_exported": True,
     "signature": "(password string) (string, error)", "usage_count": 1},
    {"name": "AuthMiddleware", "kind": "function", "file_path": "middleware/auth.go",
     "line_number": 1, "end_line": 10, "is_exported": True, "usage_count": 2},
    {"name": "DB", "kind": "variable", "file_path": "db/client.go",
     "line_number": 1, "end_line": 5, "is_exported": True, "usage_count": 4},
    {"name": "FormatLegacyDate", "kind": "function", "file_path": "utils/dates.go",
     "line_number": 1, "end_line": 5, "is_exported": True, "usage_count": 0},
    {"name": "Login", "kind": "function", "file_path": "auth/login.go",
     "line_number": 1, "end_line": 10, "is_exported": True,
     "signature": "(email string, password string) (*AuthResult, error)", "usage_count": 2},
    {"name": "SignToken", "kind": "function", "file_path": "auth/jwt.go",
     "line_number": 1, "end_line": 8, "is_exported": True,
     "signature": "(payload TokenPayload) (string, error)", "usage_count": 1},
]

# ---------------------------------------------------------------------------
# Reference definitions per language
# ---------------------------------------------------------------------------

TS_REFS: list[dict[str, Any]] = [
    {"symbol_name": "getUserById", "referenced_in_file": "src/routes/users.ts",
     "referenced_at_line": 3, "reference_type": "import"},
    {"symbol_name": "getUserById", "referenced_in_file": "src/routes/users.ts",
     "referenced_at_line": 15, "reference_type": "call"},
    {"symbol_name": "getUserById", "referenced_in_file": "src/index.ts",
     "referenced_at_line": 10, "reference_type": "call"},
    {"symbol_name": "NotFoundError", "referenced_in_file": "src/users/queries.ts",
     "referenced_at_line": 2, "reference_type": "import"},
    {"symbol_name": "db", "referenced_in_file": "src/users/queries.ts",
     "referenced_at_line": 1, "reference_type": "import"},
    {"symbol_name": "AppError", "referenced_in_file": "src/middleware/errorHandler.ts",
     "referenced_at_line": 1, "reference_type": "import"},
]

PY_REFS: list[dict[str, Any]] = [
    {"symbol_name": "get_user_by_id", "referenced_in_file": "src/routes/users.py",
     "referenced_at_line": 3, "reference_type": "import"},
    {"symbol_name": "get_user_by_id", "referenced_in_file": "src/routes/users.py",
     "referenced_at_line": 15, "reference_type": "call"},
    {"symbol_name": "get_user_by_id", "referenced_in_file": "src/app.py",
     "referenced_at_line": 10, "reference_type": "call"},
    {"symbol_name": "NotFoundError", "referenced_in_file": "src/users/queries.py",
     "referenced_at_line": 2, "reference_type": "import"},
    {"symbol_name": "db", "referenced_in_file": "src/users/queries.py",
     "referenced_at_line": 1, "reference_type": "import"},
    {"symbol_name": "AppError", "referenced_in_file": "src/middleware/error_handler.py",
     "referenced_at_line": 1, "reference_type": "import"},
]

GO_REFS: list[dict[str, Any]] = [
    {"symbol_name": "GetUserByID", "referenced_in_file": "handlers/users.go",
     "referenced_at_line": 3, "reference_type": "import"},
    {"symbol_name": "GetUserByID", "referenced_in_file": "handlers/users.go",
     "referenced_at_line": 15, "reference_type": "call"},
    {"symbol_name": "GetUserByID", "referenced_in_file": "main.go",
     "referenced_at_line": 10, "reference_type": "call"},
    {"symbol_name": "NotFoundError", "referenced_in_file": "users/queries.go",
     "referenced_at_line": 2, "reference_type": "import"},
    {"symbol_name": "DB", "referenced_in_file": "users/queries.go",
     "referenced_at_line": 1, "reference_type": "import"},
    {"symbol_name": "AppError", "referenced_in_file": "middleware/error_handler.go",
     "referenced_at_line": 1, "reference_type": "import"},
]

# ---------------------------------------------------------------------------
# Language configs
# ---------------------------------------------------------------------------

LANG_CONFIG: dict[str, dict[str, Any]] = {
    "typescript": {
        "language": "typescript",
        "symbols": TS_SYMBOLS,
        "refs": TS_REFS,
        "packages": [
            ("express", "4.18.0", "npm"),
            ("zod", "3.22.0", "npm"),
            ("axios", "1.6.0", "npm"),  # unused
        ],
        "query_func": "getUserById",
        "error_class": "NotFoundError",
        "dead_symbol": "formatLegacyDate",
        "unused_pkg": "axios",
        "queries_file": "src/users/queries.ts",
        "errors_file": "src/utils/errors.ts",
        "db_file": "src/db/client.ts",
    },
    "python": {
        "language": "python",
        "symbols": PY_SYMBOLS,
        "refs": PY_REFS,
        "packages": [
            ("flask", "3.0.0", "pip"),
            ("pydantic", "2.0.0", "pip"),
            ("requests", "2.31.0", "pip"),  # unused
        ],
        "query_func": "get_user_by_id",
        "error_class": "NotFoundError",
        "dead_symbol": "format_legacy_date",
        "unused_pkg": "requests",
        "queries_file": "src/users/queries.py",
        "errors_file": "src/utils/errors.py",
        "db_file": "src/db/client.py",
    },
    "go": {
        "language": "go",
        "symbols": GO_SYMBOLS,
        "refs": GO_REFS,
        "packages": [
            ("gin", "1.9.0", "go"),
            ("gorm", "1.25.0", "go"),
            ("fiber", "2.0.0", "go"),  # unused
        ],
        "query_func": "GetUserByID",
        "error_class": "NotFoundError",
        "dead_symbol": "FormatLegacyDate",
        "unused_pkg": "fiber",
        "queries_file": "users/queries.go",
        "errors_file": "utils/errors.go",
        "db_file": "db/client.go",
    },
}


def populate_store(store: SymbolStore, config: dict[str, Any]) -> dict[str, int]:
    """Populate a store with symbols, refs, and packages.

    Returns a name -> symbol_id mapping.
    """
    now = int(time.time())
    name_to_id: dict[str, int] = {}
    lang = config["language"]

    for sym in config["symbols"]:
        result = store.insert_symbol(
            name=sym["name"],
            kind=sym["kind"],
            language=lang,
            file_path=sym["file_path"],
            line_number=sym["line_number"],
            end_line=sym["end_line"],
            is_exported=sym["is_exported"],
            signature=sym.get("signature"),
            params=None,
            return_type=sym.get("return_type"),
            documentation=None,
            last_indexed_at=now,
        )
        assert isinstance(result, Ok)
        sid = result.value
        name_to_id[sym["name"]] = sid
        if sym.get("usage_count", 0) > 0:
            store.update_usage_count(sid, sym["usage_count"])

    for ref in config["refs"]:
        sym_id = name_to_id[ref["symbol_name"]]
        store.insert_ref(
            sym_id, ref["referenced_in_file"],
            ref["referenced_at_line"], ref["reference_type"],
        )

    for pkg_name, pkg_version, pkg_manager in config["packages"]:
        store.insert_package(pkg_name, pkg_version, pkg_manager)

    return name_to_id
