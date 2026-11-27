from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from dns.exception import Timeout
from pymongo.errors import ConfigurationError

from hvac_mas import storage
from hvac_mas.settings import Settings


@pytest.mark.asyncio
@pytest.mark.parametrize("recovers", [True, False])
async def test_srv_timeout_is_retried_and_reported_without_uri(monkeypatch, recovers):
    clients = []

    class Client:
        def __init__(self, uri, **options):
            self.admin = SimpleNamespace(command=self.command)
            self.close = AsyncMock()
            clients.append(self)

        def __getitem__(self, name):
            return object()

        async def command(self, name):
            if len(clients) == 2 and recovers:
                return {"setName": "replica"}
            raise ConfigurationError("internal resolver details") from Timeout()

    monkeypatch.setattr(storage, "AsyncMongoClient", Client)
    store = storage.Store(Settings(storage="mongo",
                                   mongodb_uri="mongodb+srv://user:sample-secret@example.com/"))
    try:
        if recovers:
            await store.connect(indexes=False)
        else:
            with pytest.raises(storage.MongoStartupError, match="MongoDB DNS lookup failed") as error:
                await store.connect(indexes=False)
            assert "sample-secret" not in str(error.value)
            assert error.value.__suppress_context__
        assert len(clients) == 2
        clients[0].close.assert_awaited_once()
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_stale_resume_timeout_reopens_stream_and_reconciles(monkeypatch):
    import asyncio
    from pymongo.errors import NetworkTimeout

    ready = asyncio.Event()
    received = []
    store = storage.Store(Settings(storage="memory"))
    checkpoints = SimpleNamespace(find_one=AsyncMock(return_value={"token": "old-token"}))
    store.db = SimpleNamespace(change_stream_checkpoints=checkpoints)

    class Cursor:
        def sort(self, ordering):
            return self

        async def __aiter__(self):
            yield {"_id": "retained", "packetId": "retained"}

    class Stream:
        alive = True

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def try_next(self):
            assert ready.is_set()
            raise asyncio.CancelledError

    collection = SimpleNamespace(watch=AsyncMock(side_effect=[NetworkTimeout("old resume"), Stream()]),
                                 find=lambda query: Cursor())
    monkeypatch.setattr(store, "collection", lambda name: collection)
    monkeypatch.setattr(storage.asyncio, "sleep", AsyncMock())

    async def receive(row):
        received.append(row["packetId"])

    with pytest.raises(asyncio.CancelledError):
        await store.watch_telemetry(receive, ready)
    assert collection.watch.await_args_list[0].kwargs["resume_after"] == "old-token"
    assert "resume_after" not in collection.watch.await_args_list[1].kwargs
    assert received == ["retained"]


@pytest.mark.asyncio
async def test_history_operation_deadline_is_configurable(monkeypatch):
    captured = {}
    class Client:
        admin = SimpleNamespace(command=AsyncMock(return_value={"setName":"replica"}))
        close = AsyncMock()
        def __init__(self, uri, **options):
            captured.update(options)
        def __getitem__(self, name):
            return object()
    monkeypatch.setattr(storage, "AsyncMongoClient", Client)
    store = storage.Store(Settings(storage="mongo", mongodb_uri="mongodb://example.com",
                                   mongodb_operation_timeout_ms=45000))
    try:
        await store.connect(indexes=False)
        assert captured["timeoutMS"] == 45000
    finally:
        await store.close()
