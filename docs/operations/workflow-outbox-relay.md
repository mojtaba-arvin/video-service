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
The plan consumer must re-check durable Job state and claim each node under a
database lease before any side effect. Every side-effect adapter must honor
the deterministic node idempotency key.

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
- Job cancelled before relay publication: the relay fences its lease, locks and
  rechecks the authoritative Job row, marks the outbox row `CANCELLED`, and
  emits `OUTBOX_CANCELLED_BEFORE_DISPATCH` without publishing. The consumer
  independently rechecks Job state to cover a cancellation racing after the
  relay's check.
- Plan missing, changed or mismatched: no message is published; the row is
  retried and eventually dead-lettered. Do not repair the fingerprint in place.
- Job cancellation racing with publish: broker publication cannot be made
  atomic with the Job database transaction. A plan consumer must re-read the
  Job's authoritative state and refuse to claim any node for a cancelled Job.

## Verified CI evidence and production boundary

On 2026-11-22, commit
`e1469bf13280650cd552b78a5bae3d7e07d849d3` passed the final-head Push CI,
Pull Request CI, and Dependency Review:

- [Push CI](https://github.com/mojtaba-arvin/video-service/actions/runs/45015170071)
- [Pull Request CI](https://github.com/mojtaba-arvin/video-service/actions/runs/45015175951)
- [Dependency Review](https://github.com/mojtaba-arvin/video-service/actions/runs/45015175953)

The Redis-broker/PostgreSQL outbox acceptance case verifies retry after broker
acceptance and a simulated lost database acknowledgement, including reuse of
the same task ID and immutable plan reference. The OS-process recovery test
kills a worker with SIGKILL and verifies lease-expiry recovery after explicit
redelivery. It does **not** prove automatic broker redelivery after process
death.

These tests use ephemeral PostgreSQL/Redis and S3/KMS test doubles. They do not
establish live AWS KMS/STS/IAM permissions, production bucket behavior, or
broker redelivery semantics in the deployed topology.

## Rollout gate

The relay, plan dispatcher, node consumer, complete 13-operation handler
registry, and test coverage are implemented in the branch. They are **not
active in production**. The durable feature gate defaults to false, Beat only
schedules the relay when the gate is enabled, and gRPC CreateJob remains on
`legacy_celery` without inserting a durable dispatch intent.

Do not start a production relay, schedule its Beat task, set
`enqueue_dispatch=True`, or enable the feature gate until all gates in the
[Durable VOD release and rollback runbook](durable-vod-release-runbook.md) have
been evidenced and explicitly approved. In particular, live AWS identity and
policy validation, accepted legacy Job/callback reconciliation, deployed
broker recovery, monitoring/alerting, and rollback rehearsal remain
environment-dependent release gates.

Green CI is necessary evidence for the tested commit; it is not production
activation approval.
