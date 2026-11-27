"""MongoDB persistence and the single telemetry change stream consumer."""
import asyncio
import logging
from datetime import UTC, datetime, timedelta
from time import monotonic

from dns.exception import DNSException, Timeout as DNSTimeout
from pymongo import AsyncMongoClient, InsertOne, timeout
from pymongo.errors import BulkWriteError, ConfigurationError, OperationFailure, PyMongoError

from hvac_mas.settings import Settings

LOG = logging.getLogger(__name__)
HISTORY = ("telemetry", "mas_messages", "mas_events", "simulation_truth", "equipment_actions", "experiment_results")
ABSOLUTE_TTL = ("simulation_runs", "simulation_state")


class MongoStartupError(RuntimeError):
    """A startup failure with a message safe to show in the terminal."""


def now():
    return datetime.now(UTC)


class Store:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = None
        self.db = None
        self.memory = {name: {} for name in (*HISTORY, *ABSOLUTE_TTL, "scenario_definitions", "building_configurations")}

    def collection(self, name):
        return self.db[self.settings.mongodb_telemetry_collection if name == "telemetry" else name]

    async def connect(self, *, indexes=True):
        if self.settings.storage == "memory":
            return
        if not self.settings.mongodb_uri:
            raise ValueError("Set MONGODB_URI or explicitly select STORAGE=memory")
        uri = self.settings.mongodb_uri.get_secret_value()
        # A brief DNS outage can clear before the second attempt. No database
        # writes occur until the topology check passes.
        for attempt in range(2):
            self.client = AsyncMongoClient(uri, tz_aware=True, connectTimeoutMS=10000,
                                           serverSelectionTimeoutMS=20000, timeoutMS=5000)
            self.db = self.client[self.settings.mongodb_database]
            try:
                with timeout(30):
                    hello = await self.client.admin.command("hello")
                break
            except ConfigurationError as exc:
                if not (uri.startswith("mongodb+srv://") and isinstance(exc.__cause__, DNSException)):
                    raise
                await self.close()
                if attempt == 0 and isinstance(exc.__cause__, DNSTimeout):
                    await asyncio.sleep(0.5)
                    continue
                raise MongoStartupError(
                    "MongoDB DNS lookup failed. Reconnect or switch networks and retry. "
                    "If it persists, use the Atlas-provided standard mongodb:// URI in .env, "
                    "or set STORAGE=memory to run the dashboard without MongoDB."
                ) from None
        if not (hello.get("setName") or hello.get("msg") == "isdbgrid"):
            raise ValueError("MongoDB change streams require a replica set or sharded cluster")
        if not indexes:
            return
        for name in HISTORY:
            col = self.collection(name)
            await col.create_index("ingestedAt", expireAfterSeconds=self.settings.mongodb_ttl_seconds)
            await col.create_index([("runId", 1), ("simulationStep", -1)])
        for name in ABSOLUTE_TTL:
            await self.collection(name).create_index("expiresAt", expireAfterSeconds=0)
        await self.collection("telemetry").create_index("packetId", unique=True)
        await self.collection("telemetry").create_index([("runId", 1), ("unitId", 1), ("simulationStep", -1)])

    async def close(self):
        if self.client is not None:
            await self.client.close()

    async def insert(self, name, rows):
        stamp = now()
        documents = [{**row, "ingestedAt": stamp} for row in rows]
        if not documents:
            return
        if self.db is None:
            self.prune(name)
            for row in documents:
                self.memory[name].setdefault(row["_id"], row)
            return
        try:
            await self.collection(name).bulk_write([InsertOne(r) for r in documents], ordered=False)
        except BulkWriteError as e:
            details = e.details
            if details.get("writeConcernErrors") or any(r["code"] != 11000 for r in details["writeErrors"]):
                raise

    async def commit_interval(self, action, messages, events, truth, state):
        """Publish an interval and its evidence together, keyed by run and step."""
        stamp = now()
        key = f"{action['runId']}:{action['simulationStep']}"
        groups = {
            "equipment_actions": [action | {"_id": key}],
            "simulation_truth": [truth | {"_id": key}],
            "mas_messages": messages,
            "mas_events": events,
        }
        documents = {name: [row | {"ingestedAt": stamp} for row in rows]
                     for name, rows in groups.items()}
        state = state | {"_id": action["runId"], "updatedAt": stamp}
        if self.db is None:
            # No awaits between validation and publication in the in-process store.
            for name, rows in documents.items():
                self.prune(name)
                if any(row["_id"] in self.memory[name] for row in rows):
                    raise ValueError("interval or evidence already committed")
            for name, rows in documents.items():
                self.memory[name].update({row["_id"]: row for row in rows})
            self.memory["simulation_state"][state["_id"]] = state
            return

        async def write(session):
            for name, rows in documents.items():
                if rows:
                    await self.collection(name).insert_many(rows, session=session)
            await self.collection("simulation_state").replace_one(
                {"_id": state["_id"]}, state, upsert=True, session=session)

        async with self.client.start_session() as session:
            await session.with_transaction(write)

    async def put(self, name, identity, row):
        doc = {**row, "_id": identity}
        if self.db is None:
            self.prune(name)
            self.memory[name][identity] = doc
        else:
            await self.collection(name).replace_one({"_id": identity}, doc, upsert=True)

    def prune(self, name):
        stamp = now()
        cutoff = stamp - timedelta(seconds=self.settings.mongodb_ttl_seconds)
        data = self.memory[name]
        for key, row in list(data.items()):
            if (name in HISTORY and row["ingestedAt"] < cutoff) or (
                name in ABSOLUTE_TTL and (row.get("expiresAt") or stamp) < stamp
            ):
                del data[key]

    async def get(self, name, identity):
        if self.db is None:
            self.prune(name)
            row = self.memory[name].get(identity)
        else:
            row = await self.collection(name).find_one({"_id": identity})
        return clean(row) if row else None

    async def latest_simulation_state(self):
        """Load the most recently committed, unexpired plant snapshot on startup."""
        if self.db is None:
            self.prune("simulation_state")
            rows = (row for row in self.memory["simulation_state"].values() if row.get("restoreCandidate"))
            latest = max(rows, key=lambda row: row.get("updatedAt", datetime.min.replace(tzinfo=UTC)),
                         default=None)
        else:
            latest = await self.collection("simulation_state").find_one(
                {"expiresAt": {"$gt": now()}, "restoreCandidate": True}, sort=[("updatedAt", -1)])
        return clean(latest) if latest else None

    async def query(self, name, filters, limit=300):
        if self.db is None:
            self.prune(name)
            rows = (r for r in self.memory[name].values() if all(r.get(k) == v for k, v in filters.items()))
            rows = sorted(rows, key=lambda r: (r.get("simulationStep", -1), r.get("ingestedAt"), r["_id"]),
                          reverse=True)[:limit]
        else:
            rows = await self.collection(name).find(filters).sort(
                [("simulationStep", -1), ("ingestedAt", -1), ("_id", -1)]).limit(limit).to_list()
        return [clean(r) for r in rows]

    async def watch_telemetry(self, receive, ready):
        """Checkpoint only after the receiver acknowledges processing."""
        checkpoints = self.db.change_stream_checkpoints
        col = self.collection("telemetry")
        token = None
        recover = True
        loaded = False
        last_checkpoint = 0.0
        while True:
            try:
                if not loaded:
                    checkpoint = await checkpoints.find_one({"_id": "telemetry-gateway"})
                    token = checkpoint["token"] if checkpoint else None
                    loaded = True
                async with await col.watch([{"$match": {"operationType": "insert"}}],
                                           **({"resume_after": token} if token else {}),
                                           max_await_time_ms=500) as stream:
                    if recover:
                        cutoff = now() - timedelta(seconds=self.settings.mongodb_ttl_seconds)
                        async for row in col.find({"ingestedAt": {"$gte": cutoff}}).sort(
                                [("ingestedAt", 1), ("_id", 1)]):
                            await receive(clean(row))
                        recover = False
                    ready.set()
                    while stream.alive:
                        event = await stream.try_next()
                        if event is not None:
                            await receive(clean(event["fullDocument"]))
                        next_token = stream.resume_token
                        if next_token is None:
                            continue
                        token = next_token
                        if event is not None or monotonic() - last_checkpoint >= 60:
                            await checkpoints.replace_one({"_id": "telemetry-gateway"},
                                                          {"token": token}, upsert=True)
                            last_checkpoint = monotonic()
                    ready.clear()
                    recover = True
            except PyMongoError as exc:
                ready.clear()
                # A stale resume position can time out even while new streams work.
                # Reopen first, then reconcile retained packets to cover the gap.
                if exc.timeout or (isinstance(exc, OperationFailure) and exc.code in (136, 260, 280, 286)):
                    token, recover = None, True
                LOG.warning("Telemetry stream disconnected (%s); retrying", type(exc).__name__)
                await asyncio.sleep(1)


def clean(row):
    return {k: v for k, v in row.items() if k != "_id"}
