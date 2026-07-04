from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class QueryResult:
    """Represents the result of a database query."""

    rows: list[dict[str, Any]] = field(default_factory=list)
    row_count: int = 0
    last_insert_id: int | None = None


class DatabaseClient:
    """Simple database client wrapping query execution."""

    def __init__(self, connection_string: str = "sqlite:///app.db") -> None:
        self.connection_string = connection_string
        self._connected: bool = False

    def connect(self) -> None:
        """Establish a database connection."""
        self._connected = True

    def disconnect(self) -> None:
        """Close the database connection."""
        self._connected = False

    def query(self, sql: str, params: tuple[Any, ...] | None = None) -> QueryResult:
        """Execute a SQL query and return results."""
        if not self._connected:
            raise RuntimeError("Database is not connected")
        # Stub: in a real implementation this would execute the query
        return QueryResult(rows=[], row_count=0)

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> int:
        """Execute a SQL statement and return the number of affected rows."""
        if not self._connected:
            raise RuntimeError("Database is not connected")
        return 0

    def transaction(self) -> DatabaseTransaction:
        """Begin a new transaction."""
        return DatabaseTransaction(self)


class DatabaseTransaction:
    """Context manager for database transactions."""

    def __init__(self, client: DatabaseClient) -> None:
        self._client = client

    def __enter__(self) -> DatabaseTransaction:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        if exc_type is not None:
            self.rollback()
        else:
            self.commit()

    def commit(self) -> None:
        """Commit the transaction."""

    def rollback(self) -> None:
        """Roll back the transaction."""


# Module-level singleton
db = DatabaseClient()
