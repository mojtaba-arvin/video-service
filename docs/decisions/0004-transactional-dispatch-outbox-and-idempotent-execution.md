# ADR 0004: Transactional dispatch outbox and idempotent execution plans

- **Status:** Proposed
- **Date:** 2026-11-18
- **Parent issue:** #71
- **Scope:** Reliable dispatch of durable VOD workflows from PostgreSQL to Celery or a future execution adapter

## Implementation status (2026-11-19)

PR #123 adds the first durable Attempt lease primitives on top of the encrypted
Job Secret Store work in PRs #119/#121. The implementation adds one-time
worker lease tokens (persisted only as SHA-256 digests), lease owner/expiry and
heartbeat timestamps, guarded renewal, expiry recovery and token checks on
leased-attempt validation/publication/completion/failure writes.

This is **not yet live worker integration**. Existing Celery task call sites
still use the legacy `start_attempt()` API; no production task has switched to
`claim_attempt()` or periodic heartbeat renewal. The lease API is not proof
that FFmpeg/S3 side effects are fenced after a worker loses its lease.
Attempt-scoped deterministic artifacts and side-effect idempotency remain
required before dispatch replay can be enabled.

PR #123 is open and unmerged. Its CI run #44469241842 and Dependency Review
run #44469241786 both passed on head `7356c90086c1b4948a662f2ca7686c4f01419f55`
(2026-11-19). This verifies the repository-level lease primitives and existing
regression/E2E suites; it does not verify live worker lease integration.

The required Celery integration sequence, including the separate processing
and publication lease model, is specified as a proposed design in
[ADR 0010](0010-celery-attempt-lease-worker-integration.md). The transactional
outbox and relay are not implemented. Do not activate broker-backed plan
dispatch until authenticated CreateJob/secret resolution, complete stage
coverage, worker lease/heartbeat integration, external side-effect fencing,
crash recovery and duplicate delivery tests all pass.

## Context

The current create path persists Job, Workflow and Stage records with
`WorkflowRepository.create_job()`, returns from the database transaction,
writes transient job details to Redis, builds an in-memory Celery Canvas graph
and calls `apply_async()`.

A process crash between the database commit and successful broker publication
leaves an accepted durable Job with no scheduled work. Publishing before the
database commit would also be unsafe: a worker could execute a Job that later
rolls back.

PostgreSQL and RabbitMQ/Redis do not share one transaction. A relay can publish
successfully and crash before marking its outbox row delivered. The broker may
therefore receive the same logical dispatch more than once. A Celery task ID is
a correlation value, not a broker-level exactly-once deduplication guarantee.

The current persisted Stage contracts are not yet a complete, versioned
representation of the legacy graph. They describe output stages, while
preflight checks, bucket checks, input downloads, input analysis, fan-in,
callbacks and watermark dependencies are still assembled in memory.

## Decisions

### 1. Persist intent with the workflow

Create Job, Workflow, Stage rows, a complete immutable execution-plan version
and its outbox dispatch record in one PostgreSQL transaction.

The outbox row is an instruction to dispatch a specific workflow-plan version,
not a serialized Python object or arbitrary Celery command. Its payload is
schema-versioned JSON and contains opaque identifiers. It must not contain
bearer tokens, storage credentials, signed URLs or raw webhook secrets.

### 2. Define a vendor-neutral execution plan before activating dispatch

Introduce a versioned plan contract that records:

- workflow name and plan schema version;
- stable Job and Workflow IDs;
- operation/stage keys and typed operation identifiers;
- explicit dependency edges and fan-in semantics;
- validated input/output contracts and artifact references;
- retry classification, attempt limits, deadlines and resource profile;
- callback/notification references resolved through an authorized secret
  store rather than embedded secret values.

The plan must cover the complete request path, including input checks,
download/inspection, output generation, validation, publication and final
notification. It must be sufficient to resume a workflow after the API process
and Redis cache are gone.

Do not persist Python pickles or depend on undocumented Celery Canvas internals
as the domain workflow definition. Celery remains an adapter that translates
the plan into executable messages.

### 3. Relay with leases and at-least-once publication

The relay claims a bounded number of due outbox rows in a short database
transaction, using row locks and `SKIP LOCKED` on PostgreSQL. It records a
lease owner, lease expiry and incremented attempt count, then commits before
network I/O.

It publishes outside the database transaction. After broker acknowledgement,
it marks the row delivered only if it still owns the active lease. Failed
publishes receive bounded exponential backoff with jitter. Expired leases are
reclaimable. Repeatedly failing messages enter an observable dead-letter state
and can be replayed by an authorized operator.

A crash after broker publication but before the delivered mark may publish
again. Therefore the contract is **at-least-once dispatch with idempotent
effects**, not exactly-once delivery.

### 4. Make execution effects idempotent before enabling retries

Every dispatched stage must acquire its durable execution claim in PostgreSQL
before performing non-idempotent side effects. The claim creates or attaches
to one active Attempt under row locking. Duplicate delivery must attach to the
current attempt or return a safe no-op; it must not create concurrent writes.

Use attempt-scoped scratch paths and deterministic artifact identities. Object
publication must be safe to retry, and a final Artifact must not become
available until validation and remote readback succeed.

This condition is not yet true for every preflight/input/funnel operation in
the legacy Celery graph. Therefore adding an outbox table alone must not
enable automatic replay of the whole legacy graph. First expand durable stage
coverage or introduce a guarded workflow dispatcher that routes every stage
through the durable claim API.

### 5. Cancellation and poison-message semantics

Before publishing, the relay re-checks durable Job/Workflow/Stage state. A
workflow cancelled before dispatch is suppressed and its outbox row is marked
cancelled. Once execution has started, cancellation is represented in durable
state and cooperatively propagated to the active Attempt/process supervisor.

A poison dispatch must not block unrelated rows. Operators need a queryable
reason, attempt history, next-available time and an explicit replay action.
Automatic infinite retry is prohibited.

### 6. Execution backend portability

Outbox payloads contain application-level operation references and plan
versions, not Celery task names as the permanent domain contract. A Celery
adapter may translate a plan into Celery tasks; another executor can later
consume the same plan without changing Job, Stage or Artifact identity.

## Proposed data contract

A new `workflow_outbox` table should include at least:

- `id`: stable UUID event identifier;
- `job_id`, `workflow_id`: foreign keys;
- `plan_version`: immutable workflow-plan version;
- `event_type`: bounded dispatch event name;
- `payload_json`: schema-versioned non-secret payload;
- `state`: PENDING, LEASED, DELIVERED, RETRY_WAIT, DEAD or CANCELLED;
- `attempt_count`, `available_at`, `created_at`;
- `lease_owner`, `lease_expires_at`;
- `published_at`, `last_error_code`.

Use an index that supports polling by state/availability/creation time and a
unique constraint that prevents the same logical dispatch event from being
inserted twice for one workflow-plan version. Confirm the exact unique key
against the replay policy before migration 0004 is authored.

## Required failure tests

1. Job/plan/outbox commit succeeds, API process dies before the relay starts:
   the workflow is eventually dispatched.
2. Relay dies after claiming but before publish: lease expiry makes the row
   available again.
3. Broker publish succeeds, relay dies before marking delivered: publication
   may repeat, but no duplicate logical Attempt or Artifact is created.
4. Broker is unavailable: bounded backoff occurs and unrelated rows continue.
5. Two relays race: row locking and leases prevent concurrent ownership.
6. Lease expires during slow publication: stale owner cannot mark delivery
   after a new owner has claimed the row.
7. Cancellation before publish suppresses dispatch.
8. A poison row becomes visible and can be replayed without blocking others.
9. Worker dies after an Attempt claim: stale-attempt recovery safely reopens
   the stage and a new numbered Attempt can resume it.
10. The same dispatch is consumed twice concurrently: one authoritative
    attempt owns side effects.

## Consequences

- The database commit becomes the durable acceptance boundary for work.
- Broker outages no longer lose accepted workflow intent.
- Relay publication can be repeated safely once every stage has idempotent
  claim and side-effect semantics.
- The workflow plan becomes the portable contract shared by Celery, MCP and
  future edge/worker adapters.
- The relay/outbox lifecycle must be monitored and operated; it is justified
  by the current Job-to-Celery crash window.
- This ADR does not claim the outbox or dispatcher is implemented. It records
  design constraints for Issue #71 before runtime integration begins.

## References

- AWS Prescriptive Guidance, Transactional outbox pattern:
  https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html
- microservices.io, Transactional outbox:
  https://microservices.io/patterns/data/transactional-outbox.html
- Celery task acknowledgement and idempotency:
  https://docs.celeryq.dev/en/stable/userguide/tasks.html
- Celery Canvas signatures and serialization:
  https://docs.celeryq.dev/en/stable/userguide/canvas.html
