from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from groundtruth import resolve
from groundtruth.utils.result import Ok


class _FakeClient:
    instances: list["_FakeClient"] = []

    def __init__(self, _command, _root_uri):
        self.notifications: list[tuple[str, dict]] = []
        self._request_id = 0
        self.probe_answered_ok = True
        self.project_ready = None
        self.empty_definition_lookups = 0
        self.__class__.instances.append(self)

    async def start(self):
        return Ok(None)

    async def send_request(self, _method, _params):
        self._request_id += 1
        return Ok({})

    async def send_notification(self, method, params):
        self.notifications.append((method, params))
        return Ok(None)

    async def drain(self, timeout):
        return None

    async def wait_for_progress_complete(self, timeout):
        return True

    async def probe_ready(self, timeout):
        self._request_id += 1
        return True

    def stderr_excerpt(self):
        return ""

    async def shutdown(self):
        return None


def _empty_graph(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE nodes(
              id INTEGER PRIMARY KEY, name TEXT, file_path TEXT, start_line INTEGER,
              signature TEXT, return_type TEXT, is_test INTEGER, label TEXT,
              language TEXT
            );
            CREATE TABLE edges(
              id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER,
              trust_tier TEXT
            );
            """
        )


@pytest.mark.asyncio
async def test_resolver_sends_configuration_before_document_requests(tmp_path, monkeypatch):
    graph = tmp_path / "graph.db"
    _empty_graph(graph)
    (tmp_path / "pyrightconfig.json").write_text("{}", encoding="utf-8")
    _FakeClient.instances.clear()
    monkeypatch.setattr("groundtruth.lsp.client.LSPClient", _FakeClient)

    stats = await resolve._resolve_edges(str(graph), str(tmp_path), [], "python")

    assert stats["failed"] == 0
    assert _FakeClient.instances[0].notifications[:2] == [
        ("initialized", {}),
        ("workspace/didChangeConfiguration", {"settings": {}}),
    ]


@pytest.mark.asyncio
async def test_resolver_closes_every_database_connection_when_enrichment_fails(
    tmp_path, monkeypatch
):
    graph = tmp_path / "graph.db"
    # Deliberately omit enrichment-only columns so its SELECT raises after opening.
    with sqlite3.connect(graph) as connection:
        connection.executescript(
            "CREATE TABLE nodes(id INTEGER PRIMARY KEY);"
            "CREATE TABLE edges(id INTEGER PRIMARY KEY, trust_tier TEXT);"
        )
    (tmp_path / "pyrightconfig.json").write_text("{}", encoding="utf-8")
    _FakeClient.instances.clear()
    monkeypatch.setattr("groundtruth.lsp.client.LSPClient", _FakeClient)

    real_connect = sqlite3.connect
    opened = []

    class TrackingConnection:
        def __init__(self, *args, **kwargs):
            object.__setattr__(self, "inner", real_connect(*args, **kwargs))
            object.__setattr__(self, "closed", False)
            opened.append(self)

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def __setattr__(self, name, value):
            if name in {"inner", "closed"}:
                object.__setattr__(self, name, value)
            else:
                setattr(self.inner, name, value)

        def close(self):
            self.inner.close()
            self.closed = True

    monkeypatch.setattr(resolve.sqlite3, "connect", TrackingConnection)

    await resolve._resolve_edges(str(graph), str(tmp_path), [], "python")

    assert len(opened) == 2
    assert all(connection.closed for connection in opened)


@pytest.mark.asyncio
async def test_resolver_reaps_client_and_shim_when_handshake_raises(tmp_path, monkeypatch):
    graph = tmp_path / "graph.db"
    _empty_graph(graph)

    class FailingClient(_FakeClient):
        shutdown_called = False

        async def send_request(self, _method, _params):
            raise RuntimeError("handshake_interrupted")

        async def shutdown(self):
            self.__class__.shutdown_called = True

    FailingClient.instances.clear()
    FailingClient.shutdown_called = False
    monkeypatch.setattr("groundtruth.lsp.client.LSPClient", FailingClient)

    stats = await resolve._resolve_edges(str(graph), str(tmp_path), [], "python")

    assert stats["failure_detail"] == "initialize: handshake_interrupted"
    assert FailingClient.shutdown_called is True
    assert not (tmp_path / "pyrightconfig.json").exists()
