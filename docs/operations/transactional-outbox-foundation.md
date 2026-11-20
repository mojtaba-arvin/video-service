# Transactional outbox foundation — operational boundary

- **Status:** Persistence foundation only; not enabled for production dispatch
- **Migration:** `0009_transactional_outbox`
- **Depends on:** durable workflow plan and Attempt lease stack

## What this change provides

The PostgreSQL outbox stores a reference to an immutable execution-plan version, not a serialized Celery Canvas, pickle, Python object, task name, or arbitrary executable command.

The table records the Job and Workflow IDs, plan version and fingerprint, schema version, event type, stable deduplication key, retry state, and an expiring relay lease. Repository operations provide:

- atomic insertion in the same transaction as Job, Workflow, Stage, and plan persistence;
- bounded claims using row locking and `SKIP LOCKED` on PostgreSQL;
- one-time lease tokens stored only as SHA-256 digests;
- fenced acknowledgement and retry operations;
- bounded retry attempts and a terminal `DEAD` state;
- unique deduplication keys.

A dispatch intent can only be inserted through `create_job(..., execution_plan=..., enqueue_dispatch=True)`. The repository rejects dispatch without a persisted plan. The current gRPC CreateJob path does not set this option.

## Explicitly not enabled

This foundation does **not** yet dispatch work. There is no production relay/plan-to-Celery adapter in this change, and no Beat schedule is enabled for outbox publishing.

The current accepted VOD request still builds its legacy graph in the gRPC adapter. Its durable Stage rows do not yet represent every preflight, input, fan-in, watermark, publication, and notification node. Enabling replay against that graph would violate ADR 0004 and could repeat non-idempotent effects.

Do not:
- enable a relay or publish outbox rows manually;
- write Celery Canvas payloads into `payload_json`;
- mark an outbox row published before the broker adapter confirms acceptance;
- treat a Celery task ID as deduplication;
- claim exactly-once delivery;
- consider the service Deployment-ready based on this persistence foundation.

## Migration and release order

1. Merge the prerequisite security/secret and lease PRs in their documented dependency order (#119, #121, #123, #127).
2. Apply `alembic -c video-streaming/alembic.ini upgrade head` using the release image and the production workflow database.
3. Deploy code that understands migration 0009. The outbox remains inactive until the complete plan compiler, dispatcher, consumer claims, and crash/replay gates are delivered.
4. Verify the table and indexes exist, and confirm no pending rows are being created by the current API path.
5. Do not start a relay process or add an outbox Beat entry until the activation criteria below are met.

## Activation criteria

Before enabling any relay:

- every node in the accepted request graph is represented by a versioned, immutable plan with explicit dependency/fan-in semantics;
- callback and storage credentials are represented only by authorized opaque references;
- the plan-to-executor adapter is allow-listed and version-aware;
- every node obtains a durable idempotent Stage/Attempt claim before side effects;
- cancellation is checked before dispatch and before irreversible effects;
- duplicate delivery, worker crash, relay crash after broker acceptance, broker outage, expired lease, poison message, and recovery are covered by real PostgreSQL + broker tests;
- external storage writes use deterministic/attempt-scoped identities and verified publication;
- bounded-cardinality metrics, structured redacted logs, alert thresholds based on measured baselines, and operator replay/dead-letter procedures are implemented;
- CI is green on the final commit SHA and the rollout/rollback plan is reviewed.

## Recovery semantics once activated

The intended contract is at-least-once publication. A relay can crash after broker acceptance but before the PostgreSQL acknowledgement, so a duplicate message is possible. Durable consumer claims and idempotent effects—not broker task IDs—must make replay safe.

Expired relay leases are reclaimed by a later claim. Retryable errors use bounded backoff; exhausted records enter `DEAD` and require operator inspection. An operator must verify the referenced Job, Workflow, plan fingerprint, and side-effect state before any manual replay.

## Rollback

Migration 0009 is additive. If a release must be rolled back before the relay is activated, roll back application code while retaining the table; retaining an unused table is safer than dropping durable dispatch history.

Once any outbox rows exist, do not downgrade/drop the table as a routine rollback. Roll back the relay/adapter independently and use a forward migration or controlled data-retention procedure. Never delete pending or dead rows merely to make a rollback pass.
