# ADR 0012: Provider-neutral CDN delivery event contract

- **Status:** Accepted for the normalized domain contract; persistence and provider integration deferred
- **Date:** 2026-11-26
- **Related:** [ADR 0003 — session-scoped playback QoE telemetry](0003-session-scoped-playback-qoe-telemetry.md), [ADR 0005 — modular core and replaceable adapters](0005-modular-core-and-replaceable-adapters.md), [Issue #82](https://github.com/mojtaba-arvin/video-service/issues/82)

## Context

Playback sessions and append-only player events are already bound to the exact published manifest Artifact. The missing signal is server/CDN delivery: edge/cache outcome, response timing and transferred bytes. A provider-specific raw access-log schema would couple the domain to one CDN and could unnecessarily collect viewer identifiers or request data.

No CDN provider, source-log format, customer retention policy or privacy approval has been selected. The repository must not invent those external facts or begin retaining raw access logs.

## Decision

Introduce a small provider-neutral normalized event contract before selecting an ingestion provider. One event represents one completed delivery response and contains only:

- `schema_version`: currently `1`;
- `source`: bounded lowercase adapter identifier;
- `source_event_id`: stable provider event identifier;
- `source_timestamp`: timezone-aware source event time, normalized to UTC;
- `artifact_id`: immutable ID of the exact published manifest Artifact;
- `session_id`: optional opaque playback session ID, only when the source can correlate it safely;
- `edge_region`: optional coarse region label, not a viewer or precise edge location;
- `cache_status`: `HIT`, `MISS`, `BYPASS`, `REVALIDATED`, `STALE` or `UNKNOWN`;
- `http_status`: optional HTTP response status;
- `response_time_ms`: optional bounded non-negative response duration;
- `bytes_sent`: optional non-negative response byte count.

The contract validates identifiers, timestamp awareness, enumerated cache outcomes and numeric bounds. Its canonical payload contains only these allow-listed fields. A canonical SHA-256 fingerprint supports later idempotent ingestion: `(source, source_event_id)` is the source identity; an exact replay is a no-op, while reuse with a different semantic payload is a conflict.

When a `session_id` is supplied, the future persistence adapter must verify that the session references the same `artifact_id`; never correlate a delivery event to a session solely by a client-supplied association. `artifact_id` alone still permits output-level delivery reporting when no session correlation exists.

## Privacy and storage boundary

Never ingest or persist viewer IP addresses, cookies, authorization headers, raw request/response headers, full request URLs, query strings, user-agent strings, account identifiers, advertising identifiers or arbitrary raw provider payloads. Do not infer viewer identity from network metadata. Preserve source provenance and the source timestamp without preserving the raw log line.

This increment adds only the pure domain contract and deterministic tests. It does not add a database table, ingestion endpoint, provider adapter, dashboard or production QoE claim. Persistence, query windows and retention enforcement remain blocked until a customer-approved retention period and a concrete provider log source are selected. A later migration must define bounded retention and deletion behavior before accepting production delivery events.

## Consequences

- Provider adapters can normalize source-specific logs without leaking provider fields into the domain model.
- Delivery data can be correlated to the exact immutable manifest and, when safely available, a playback session.
- Missing metrics remain `null`/unknown rather than being fabricated as zero.
- The event identity and fingerprint make replay/conflict semantics explicit before persistence is introduced.
- Issue #82 remains open for persistence, privacy/retention acceptance, query/aggregation behavior and a real provider integration.
