# Workflow outbox relay operations

## Current delivery contract

The outbox is the durable source of dispatch intent. The relay claims rows with
PostgreSQL `FOR UPDATE SKIP LOCKED` leases, verifies the persisted plan
fingerprint and workflow identity, then publishes only the Job ID, Workflow ID,
workflow version and plan fingerprint. It does not send the plan body, webhook
URLs, secret envelopes, or task paths through the broker.

Delivery is **at least once**. A relay crash after broker acceptance but before
`PUBLISHED` is committed causes a repeat publish with the same deterministic
Celery task ID. That ID is correlation metadata, not a deduplication guarantee.
The eventual plan consumer must re-check durable Job state and claim each node
under a database lease before any side effect. Every side-effect adapter must
honor the deterministic node idempotency key.

## Operator commands

Run from `video-streaming/` with the production Python dependencies installed
and `WORKFLOW_DATABASE_URL` configured:

```sh
PYTHONPATH=. python -m video_streaming.workflows.outbox_admin stats
PYTHONPATH=. python -m video_streaming.workflows.outbox_admin dead --limit 100
PYTHONPATH=. python -m video_streaming.workflows.outbox_admin inspect <outbox-id>
PYTHONPATH=. python -m video_streaming.workflows.outbox_admin replay <outbox-id> \
  --operator-id '<operator identity>' \
  --reason '<ticket or incident reference and reason>'
PYTHONPATH=. python -m video_streaming.workflows.outbox_admin relay-once
```

`replay` only accepts a `DEAD` row, resets its delivery budget and writes an
`OUTBOX_MANUAL_REPLAY_REQUESTED` durable event with operator identity and
reason. The CLI is an internal operator interface: access must be restricted
through host/container access and database credentials. Do not expose this
low-level repository operation as an unauthenticated RPC or public endpoint.

`stats` reports bounded-cardinality state counts and oldest pending age.
`dead` lists bounded non-secret metadata only. Investigate the associated Job
and failure before replay; do not blindly replay poison messages.

## Failure and recovery

- Broker unavailable: the relay schedules bounded exponential retries.
- Broker accepted, acknowledgement lost: the row is retried and may be
  published again. The consumer must be idempotent.
- Relay process lost while holding a claim: the lease expires; another relay
  may reclaim it. Expired claims consume the delivery budget.
- Retry budget exhausted: the row becomes `DEAD` and is visible in `dead` and
  `stats`; an operator may inspect and explicitly replay it.
- Plan missing, changed or mismatched: no message is published; the row is
  retried and eventually dead-lettered. Do not repair the fingerprint in place.
- Job cancellation racing with publish: broker publication cannot be made
  atomic with the Job database transaction. A plan consumer must re-read the
  Job's authoritative state and refuse to claim any node for a cancelled Job.

## Rollout gate

The relay command and fake-publisher contract tests are implemented. Do not
start a production relay or set `enqueue_dispatch=True` until a registered
`video_streaming.workflows.dispatch_plan` consumer is deployed, the complete
operation-handler registry is wired to real adapters, Job cancellation is
checked by that consumer, and real PostgreSQL + broker crash/duplicate tests
pass. The current CreateJob path still uses `legacy_celery`; this is an
intentional safety gate, not a completed durable-executor rollout.
