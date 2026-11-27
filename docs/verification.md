# Verification

Run backend unit and API tests from `HVAC_MAS_SYS` with the project's Python environment:

```sh
python -m pytest -q
```

The suite uses in-memory storage and mocked MongoDB startup paths. It does not require or alter an Atlas database. The MongoDB integration script is separate and creates synthetic records plus a short-retention probe collection; use it only with a disposable database.

For an end-to-end local demo without MongoDB, start the backend and SPADE worker with `STORAGE=memory ./scripts/dev.sh`, build the frontend with `npm run build` in `MAS_FE`, and serve its `dist/` directory. This verifies the HTTP API and agent runtime path, but memory storage does not verify persistence or change-stream recovery.

The dashboard polls the live API state every three seconds. Its `VITE_API_URL` is embedded at build time. Browser CORS is limited by the backend's `CORS_ORIGINS`; CORS does not authenticate callers. The deployment currently exposes one shared run and one active experiment, and should run as one API process plus one SPADE worker.

MongoDB change streams require a replica set or sharded cluster such as MongoDB Atlas. Active experiment task state, agent waiting history, and an in-flight decision remain process-local; an API restart during an experiment interrupts that experiment. MongoDB TTL deletion is asynchronous.
