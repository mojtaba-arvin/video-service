# ADR 0003: Session-scoped playback QoE telemetry

- **Status:** Accepted
- **Date:** 2026-11-18
- **Scope:** Playback sessions for completed VOD playlist outputs

## Context

The workflow state now records the cost/resource inputs for each published output, but it does not show whether viewers can start playback reliably or how buffering/decoder failures vary by output. QoE signals need a durable session identity and a trustworthy relationship to the manifest and its processing cost.

## Decisions

### Session identity and manifest correlation

- A playback session can be created only for a completed Stage with an available `streaming_manifest` Artifact.
- The session row references the Job, Stage and exact manifest Artifact. The client receives a random bearer token once; PostgreSQL stores only its SHA-256 hash.
- Tokens are scoped to one session and expire after 24 hours. An ended or expired session cannot accept new events.
- No IP address, account identity, advertising identifier or other user profile data is collected. Optional client version and decoder name are bounded strings.

### Event contract and idempotency

The typed gRPC contract accepts:
- `PLAYBACK_STARTED` with startup-to-first-frame milliseconds.
- `BUFFERING_STARTED` and `BUFFERING_ENDED`.
- `DECODER_ERROR` with a bounded error code and optional decoder identifier.
- `PLAYBACK_ENDED`.

Every event requires a client-generated stable `event_id`. A unique constraint on `(session_id, event_id)` makes retries idempotent. A canonical SHA-256 fingerprint of the event type and semantic payload is stored, so an exact replay is accepted but reuse of the key with a different event type or payload is rejected.

Client timestamps are not used to calculate durations. The server receive clock measures buffering intervals, avoiding device-clock skew. An unfinished buffering interval is closed when the session ends.

### Durable reporting and cost correlation

- Playback sessions and append-only playback events are stored in PostgreSQL.
- Session aggregates include startup time, rebuffer count, total buffering duration, decoder errors and session status.
- The output-level QoE report joins those aggregates to the exact Stage and aggregates its Attempt CPU seconds and resource-cost estimate.
- Cost model ID and currency/unit are returned with the QoE report. `RUNIT` remains a normalized resource index, not a provider invoice or monetary charge.

## Consequences

- QoE can be compared across published outputs without relying on Redis TTL.
- Event replay does not double-count starts, buffering or decoder errors.
- The client needs a small integration to request a session and send typed events; this change does not add a web/mobile player or SDK.
- The current first report exposes average startup time, buffering ratio (including an interval that is still active when queried), rebuffer count, decoder-error count and aggregate stage resource/cost values. Estimated cost is reported as complete only when every attempt is priced under one currency and price model; partial coverage is exposed separately and never mislabeled as a total. Percentiles, cohorts, adaptive bitrate decisioning, player-specific integrations and automated quality policies are deferred until representative telemetry volume exists.
