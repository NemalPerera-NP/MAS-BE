# Verification

Run the focused backend regression suite from `HVAC_MAS_SYS` with the project's Python environment:

```sh
python -m pytest -q
```

The suite uses in-memory storage and mocked MongoDB startup paths. It does not require or alter an Atlas database. The MongoDB integration script is separate and creates synthetic records plus a short-retention probe collection; use it only with a disposable database.

For an end-to-end local demo without MongoDB, start the backend and SPADE worker with `STORAGE=memory ./scripts/dev.sh`, build the frontend with `npm run build` in `MAS_FE`, and serve its `dist/` directory. This verifies the HTTP API and agent runtime path, but memory storage does not verify persistence or change-stream recovery.

Run frontend checks with `npm test` and `npm run build` from `MAS_FE`.

The dashboard polls the live API state every three seconds. Its `VITE_API_URL` is embedded at build time. Browser CORS is limited by the backend's `CORS_ORIGINS`; Hosted deployments require a distinct evaluator key. The API checks the key on reads and writes, and the SPADE worker uses its own credential. To run the integration check in hosted mode, set `EVALUATOR_TOKEN` in the script environment. The deployment currently exposes one shared run and one active experiment, and should run as one API process plus one SPADE worker.

MongoDB change streams require a replica set or sharded cluster such as MongoDB Atlas. Active experiment task state, agent waiting history, and an in-flight decision remain process-local; an API restart during an experiment interrupts that experiment. MongoDB TTL deletion is asynchronous.


## Interpreting the demo

The [backend README](../README.md) describes installation, five-minute simulation intervals, occupancy schedules, target setbacks, available stages, live controls, and history retention. Check each feature in the dashboard against its supporting messages, allocations, negotiation outcomes, and comparison trials. The butterfly experiment compares matched runs under identical starting inputs with and without the disturbance for each controller. Emergence uses measured collective outcomes under the same cap, including a trial with negotiation disabled. The dashboard reports whether the results support each claim.

Stage powers, heat transfer, and occupancy loads are synthetic. FAN represents recirculation and its electrical demand. The model does not estimate outside air ventilation, indoor air quality, or equipment performance at a real building.

## Database history read deadlines

MongoDB operations use a configurable `MONGODB_OPERATION_TIMEOUT_MS` deadline (default 30000 ms, range 1000–120000 ms). The previous fixed five-second deadline was reproduced timing out on dashboard message and equipment-action history reads while smaller database queries succeeded. Raising the bounded deadline allows cursor reads to complete on slower Atlas connections; it does not bypass database errors or change the selected storage mode. Feature-status queries project only the fields needed for classification rather than loading telemetry payloads and full equipment snapshots.

Database failures remain HTTP 503 responses. Timeouts have a specific safe explanation and a `Retry-After` header. Server logs record the exception class without credentials or connection strings. Regression tests cover configurable deadlines, retryable errors, projected feature classifications and source identifiers. Memory-mode unit tests do not establish Atlas connectivity; verify `/health`, `/mas/features`, `/agents/messages` and `/evidence` against the live service after restarting both the API and runtime.

Live follow-up on the configured Atlas backend: `/health`, `/mas/features`, `/agents/messages?limit=1000`, and `/evidence?limit=1000` returned HTTP 200 after the fix, with eight Postman status/schema assertions passing. The same retained run advanced from 40 to 45 simulated minutes in the browser; its action recorded controller `mas`, outcome `committed`, and six room readings. A subsequent health check reported MongoDB storage and agent readiness. This validates one real interval and the repaired history reads, not every experiment or long-term network availability. The backend regression suite passed 74 tests.

## Dashboard fixes verified on 2026-11-27

- Automated backend regressions cover export lifecycle metrics, preserving completed comparative classifications, independent busy-worker heartbeat data, twelve-agent directory connectivity, five-minute visit validation with legacy restore compatibility, interval occupancy at expiry, identity-validated telemetry handoff and rejecting invalid worker allocations without leaving the commit acknowledgement hanging.
- Frontend regressions cover delivery grouping, exact lifecycle evidence links, conversation identity, room-specific chart markers, interval occupancy and agent directory explanations. Entry script syntax and the static build were checked.
- Live target: local dashboard on port 5173, MongoDB storage, twelve real SPADE agents over embedded XMPP. Eight consecutive interactive intervals (steps 15–22) committed with MAS control and no additional backup interval. The historical five backups remain recorded. This sample establishes the tested behavior, not guaranteed reliability over every scenario.
- Two short sensor-only comparisons each completed two matched trials of three intervals with real MAS commits and no capacity violation. Source-run association and reports without message payloads by default were asserted. Sensor-only comparisons are not butterfly-effect evidence.
- Browser checks covered selecting agent routes, exact communication evidence links, grouped activity rows, load-more history, future occupancy scheduling, comparison progress/results, automatic playback/pause and a real export download. A visit accepted for step 18 remained queued after step 17 and applied during step 18, with the result committed at minute 95. The live run was left paused after minute 115; the temporary test visit expired naturally.
- The 390px-wide comparison and agent-directory views had no page-width overflow; the communication graph scrolls within its own container. Desktop viewport was restored.
- Startup and interval events were observed in both terminal output and log files. Starting the launcher again safely refused the occupied API port. One initial agent startup attempt failed; retry succeeded and the completed live checks above used the restarted worker. This remains a reason to inspect startup logs after service restarts.
