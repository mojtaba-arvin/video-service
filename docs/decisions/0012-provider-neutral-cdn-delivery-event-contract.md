# ADR 0012: Provider-neutral CDN delivery event contract

- **Status:** Accepted; normalized persistence/query path implemented, provider-specific source integration deferred
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
- `asset_version`: optional bounded asset-version label;
- `delivery_config_id`: optional bounded delivery-configuration version/reference;
- `edge_region`: optional coarse region label, not a viewer or precise edge location;
- `cache_status`: `HIT`, `MISS`, `BYPASS`, `REVALIDATED`, `STALE` or `UNKNOWN`;
- `http_status`: optional HTTP response status;
- `response_time_ms`: optional bounded non-negative response duration;
- `bytes_sent`: optional non-negative response byte count.

The contract validates identifiers, timestamp awareness, enumerated cache outcomes and numeric bounds. Its canonical payload contains only these allow-listed fields. A canonical SHA-256 fingerprint supports later idempotent ingestion: `(source, source_event_id)` is the source identity; an exact replay is a no-op, while reuse with a different semantic payload is a conflict.

When a `session_id` is supplied, the persistence adapter verifies that the session references the same `artifact_id`, Stage and Job; it never correlates solely by a client-supplied association. `artifact_id` alone still permits output-level delivery reporting when no session correlation exists.

## Privacy and storage boundary

Never ingest or persist viewer IP addresses, cookies, authorization headers, raw request/response headers, full request URLs, query strings, user-agent strings, account identifiers, advertising identifiers or arbitrary raw provider payloads. Do not infer viewer identity from network metadata. Preserve source provenance and the source timestamp without preserving the raw log line.

The durable increment adds a PostgreSQL table, a provider-neutral normalized-event adapter, idempotent persistence, an authorized gRPC query API, bounded aggregation windows and explicit coverage. It does not add a provider-specific parser, raw access-log ingestion, dashboard or production QoE claim. Provider-specific source integration remains deferred until a concrete log source and deployment privacy approval are selected.

The repository retention policy defaults to 30 days from ingestion and accepts deployment values from 1 through 90 days. Every row stores an explicit expiry; the bounded purge operation deletes at most 5,000 rows per call and can be safely repeated. The repository schedules a bounded purge task hourly through Celery Beat when the workflow database is configured. Deployments must keep Beat active and confirm their customer/legal retention policy before connecting live provider exports. Expiry metadata alone is not represented as proof that a scheduler has run.



## Consequences

- Provider adapters can normalize source-specific logs without leaking provider fields into the domain model.
- Delivery data can be correlated to the exact immutable manifest and, when safely available, a playback session.
- Missing metrics remain `null`/unknown rather than being fabricated as zero.
- The event identity and fingerprint enforce at-least-once/idempotent ingestion: exact replay is a no-op, semantic conflict is rejected.
- Observed source events, derived aggregates and missing coverage are returned separately.
- Issue #82 still requires a concrete provider export and deployment-specific privacy/retention approval before claiming live CDN coverage.


## Implementation boundary (Issue #82, 2026-11-28)

- `ProviderNeutralCDNAdapter` accepts only normalized `DeliveryEvent` instances; provider-specific parsers must discard disallowed request/viewer fields before this boundary.
- `DeliveryTelemetryRepository` persists allow-listed fields and derives Job, Workflow, Stage, Attempt and manifest Artifact links from the durable Artifact. It never trusts a caller-supplied Job or Stage association.
- Session correlation is optional, but when supplied it must match the exact manifest Artifact, Stage and Job.
- Query API output separates `observed_events`, `derived_aggregates` and `coverage`; unavailable HTTP, timing, byte, asset-version and player/CDN signals have explicit availability/coverage indicators.
- The query API is owner-authorized and bounded to 1,000 records and 60–86,400 second aggregation windows. Metric labels remain bounded; identifiers are returned only in query records.
- Default retention is 30 days from ingestion, configurable only within 1–90 days. The purge method is bounded and repeatable; deployment scheduling and legal approval remain operational gates.
- No CDN vendor has been selected. This is a provider-neutral adapter contract, not evidence that any provider log export is connected or that QoE has been validated in production.
