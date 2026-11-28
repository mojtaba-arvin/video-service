# Durable dispatch staging acceptance record

- **Issue:** [#212 — verify durable dispatch rollout in staging](https://github.com/mojtaba-arvin/video-service/issues/212)
- **Parent:** [#71 — durable Job-to-Celery dispatch](https://github.com/mojtaba-arvin/video-service/issues/71)
- **Implementation:** [PR #151](https://github.com/mojtaba-arvin/video-service/pull/151)
- **Cancellation hardening:** [PR #210](https://github.com/mojtaba-arvin/video-service/pull/210) (open at preparation time)
- **Status:** **BLOCKED — staging acceptance NOT RUN**
- **Preparation baseline:** `develop` at `68eb623191c396863d0df544f8e32108737435bf`
- **Acceptance candidate SHA:** not supplied / not established
- **Staging deployment and image digest:** not supplied / not established
- **Operator identity and execution timestamps:** not supplied / not established
- **Feature gate:** retain `WORKFLOW_DURABLE_DISPATCH_ENABLED=false`

This record is an execution/evidence plan, not a claim that staging tests ran. Repository CI, test doubles, and local integration tests are not substitutes for the deployed broker/worker topology. Do not mark any gate PASS until the exact deployed image, timestamped output, and operator evidence are attached.

## 1. Verified implementation contract

The current implementation and operational docs establish:

- PostgreSQL is the authoritative workflow and dispatch state store. Broker messages carry stable Job/Workflow/plan references, not the plan body or secret material.
- The outbox relay claims bounded batches with PostgreSQL row locks / `SKIP LOCKED`, expiring leases, one-time lease tokens, and fenced acknowledgement/retry operations.
- Relay publication is **at-least-once**. If the broker accepts a message and the PostgreSQL `PUBLISHED` acknowledgement is lost, the same logical dispatch may be published again. Celery task IDs are correlation metadata, not deduplication.
- Consumers must re-read authoritative Job/plan state and claim each node through the PostgreSQL Attempt lease before side effects. Side-effect adapters must be idempotent.
- Celery dispatch and node tasks use late acknowledgement and `reject_on_worker_lost=True`; the actual broker's requeue/visibility behavior still requires deployment verification.
- Outbox retry is bounded (relay defaults: 20 attempts, exponential delay starting at 2 seconds and capped at 300 seconds); exhausted rows become `DEAD`.
- Manual replay accepts only a `DEAD` row and records operator identity and reason in a durable event.
- Cancellation and publication cannot share one atomic transaction across PostgreSQL and the broker. The consumer must re-check Job state. PR #210 adds relay-side cancellation suppression, but it was open at the preparation baseline and must be included in the exact acceptance candidate before its behavior is credited.
- The deployed workflow feature gate defaults to false. The release runbook explicitly says green CI does not authorize activation.

Relevant implementation references:
- `docs/operations/workflow-outbox-relay.md`
- `docs/operations/durable-vod-release-runbook.md`
- `docs/operations/transactional-outbox-foundation.md`
- `docs/decisions/0010-celery-attempt-lease-worker-integration.md`
- `video-streaming/video_streaming/workflows/relay.py`
- `video-streaming/video_streaming/workflows/tasks.py`
- `video-streaming/video_streaming/workflows/outbox_admin.py`

## 2. Preconditions — no test execution before these are recorded

| Required evidence | Value |
|---|---|
| Staging environment / account / region | NOT PROVIDED |
| Change / maintenance ticket and approval | NOT PROVIDED |
| Candidate source commit (full SHA) | NOT ESTABLISHED |
| API image digest (immutable) | NOT PROVIDED |
| Relay/scheduler image digest (immutable) | NOT PROVIDED |
| Worker image digest (immutable) | NOT PROVIDED |
| Migration image digest (immutable) | NOT PROVIDED |
| PostgreSQL engine/version and Alembic revision | NOT PROVIDED |
| Broker product/version/cluster and queue or exchange topology | NOT PROVIDED |
| Broker durability, message persistence, acknowledgement and redelivery policy | NOT PROVIDED |
| Celery version, transport, queue names, worker concurrency/prefetch/acks settings | NOT PROVIDED |
| Relay replica count and lease TTL / batch / retry configuration | NOT PROVIDED |
| Attempt lease TTL and reconciliation schedule | NOT PROVIDED |
| Handler factory identity and 13-operation registry validation | NOT PROVIDED |
| S3 bucket/prefix policy and KMS/STS workload identity evidence | NOT PROVIDED |
| Non-secret configuration snapshot (redacted) | NOT PROVIDED |
| Operator identity, reviewer, start/end timestamps | NOT PROVIDED |

Do not paste connection strings, credentials, tokens, signed URLs, plaintext callback URLs, secret envelopes, or customer media identifiers into this record. Record safe identifiers and secret-manager reference names only.

The exact same immutable candidate must be deployed to the API, relay/scheduler, migration job, and workers, with compatible schema and plan handlers. Do not use moving tags.

## 3. Execution matrix

Each test must use isolated synthetic Jobs and unique correlation IDs. Record the Job, Workflow, Stage, Attempt and outbox IDs in the secured evidence attachment (not as metric labels). Before every destructive fault injection, verify the target is staging-only and that unrelated Jobs are protected.

| Gate | Procedure against deployed topology | Required pass evidence | Status |
|---|---|---|---|
| Broker outage and recovery | Isolate the staging relay's broker connectivity while preserving PostgreSQL. Create accepted canary dispatch intent through the approved staging test path. Observe bounded retry scheduling and oldest-pending age. Restore broker connectivity and observe recovery. Exhaust a separate canary's retry budget to DEAD. | Timestamped broker fault/restore, relay logs, outbox state transitions, retry count/next-available times, pending-age graph, DEAD row, and proof unrelated Jobs continue. Accepted Job remains durable and is eventually dispatched or explicitly DEAD. | NOT RUN |
| Relay crash after broker acceptance, before PostgreSQL ack | Instrument a one-shot fault after broker publisher confirmation and before `mark_outbox_published`. Kill only that relay process. Allow real broker redelivery/retry and observe the duplicate logical dispatch. | Broker confirmation and process-exit timestamps; outbox lease expiry/reclaim; two or more broker deliveries where expected; same stable Job/Workflow/plan identity; one logical node Attempt owner; no duplicate Artifact/publication. Prove behavior from broker and PostgreSQL evidence, not mocked publisher calls. | NOT RUN |
| Worker crash after Attempt claim | Start a canary node and terminate its worker process after PostgreSQL Attempt claim but before completion. Do not acknowledge the delivery. Observe the actual broker's worker-loss behavior and Attempt lease expiry/reconciliation. | Worker termination timestamp, broker unacked/requeued/redelivered evidence, Attempt lease and owner transitions, recovery dispatch, exactly one active logical Attempt, and one validated/published Artifact. If broker does not redeliver automatically, document that fact and demonstrate the explicitly designed recovery mechanism. | NOT RUN |
| Multiple relays and lease fencing | Run at least two relay replicas against the same staging PostgreSQL and broker. Make both poll concurrently for a batch containing canary rows. Pause one relay past lease expiry, let the other reclaim, then resume the stale relay. | Replica identities, overlapping poll timestamps, database lease-owner/expiry snapshots, broker publish records, stale-owner fenced update result, and proof no stale acknowledgement overwrites the current lease owner. | NOT RUN |
| Cancellation before dispatch | Cancel a canary Job before its outbox row is eligible for publication. Exercise the relay on the deployed candidate including PR #210. | Job cancellation event, outbox CANCELLED state/event, no broker publication for that intent, and no Attempt or Artifact created. | NOT RUN |
| Cancellation racing broker publication | Pause a canary relay after its authoritative cancellation check but before broker publish; cancel the Job, then release the relay. Allow the message to reach the deployed worker. | Timestamped interleaving, broker message evidence (if publication won the race), consumer authoritative Job re-check, no new Attempt/side effect for cancelled Job, durable cancellation outcome, and no false success. A broker message may exist in this race; consumer suppression is the safety boundary. | NOT RUN |
| Poison isolation and audited replay | Inject a permanently invalid canary dispatch (for example unsupported event/plan identity) and a healthy unrelated canary. Let bounded retries exhaust. Inspect with `dead`, `inspect`, and `stats`. Replay only after recording an incident/change reason and operator identity. | Poison row reaches DEAD with safe error code and bounded attempts; healthy Job progresses; unauthorized/invalid replay is rejected; authorized replay creates `OUTBOX_MANUAL_REPLAY_REQUESTED` audit event and only the selected row is requeued; final outcome is inspected. | NOT RUN |
| Rollback / forward recovery | Rehearse application rollback with pre-existing PENDING, LEASED, PUBLISHED and DEAD outbox history. Retain schema, rows, immutable plans, Attempts and Artifact identities. Verify old compatible workers/API do not misinterpret in-flight plans. | Before/after image digests, migration revisions, row counts/state snapshots, rollback/forward-recovery commands and outputs, no deleted outbox history, no lost accepted Job, and explicit operator sign-off. | NOT RUN |

### Required assertions for every scenario

1. PostgreSQL remains the source of truth for Job/Workflow/Stage/Attempt/Artifact and outbox state.
2. Delivery is at-least-once; duplicate messages are expected and safe. **Never claim exactly-once delivery.**
3. No duplicate logical Attempt owner, non-idempotent side effect, or published Artifact is created by a duplicate delivery.
4. A stale relay/worker lease holder cannot mutate current state or publish an authoritative completion.
5. Every retry is bounded and observable; poison work cannot starve unrelated Jobs.
6. Secret values and customer data are absent from broker payloads, logs, metrics, and evidence attachments.
7. No test changes production configuration or production data.

## 4. Operator commands

Run only from the approved staging worker/relay container, using the candidate image and staging-only credentials. These commands are read/inspection operations except the explicitly audited replay and relay-once commands.

From `video-streaming/`:

```sh
PYTHONPATH=. python -m video_streaming.workflows.outbox_admin stats
PYTHONPATH=. python -m video_streaming.workflows.outbox_admin dead --limit 100
PYTHONPATH=. python -m video_streaming.workflows.outbox_admin inspect <outbox-id>
```

Replay is permitted only for an inspected `DEAD` row after incident/change approval:

```sh
PYTHONPATH=. python -m video_streaming.workflows.outbox_admin replay <outbox-id> \
  --operator-id '<authenticated operator identity>' \
  --reason '<incident/change ticket and reason>'
```

Do not use `relay-once` as an ad-hoc production recovery action. It publishes work and must remain subject to the feature gate, complete handler registry validation, and approved staging change procedure.

## 5. Evidence bundle format

Attach one immutable evidence bundle per candidate run. Include:

- Candidate source SHA and all component image digests.
- Staging account/environment/region and topology diagram or sanitized deployment manifest.
- Redacted non-secret configuration: broker product/version, exchange/queue names, durability, ack/requeue policy, Celery ack/prefetch/concurrency, relay replicas, lease TTLs, retry limits, schedule intervals, DB version/revision, handler factory identifier, storage bucket/prefix identifiers, KMS key alias and role names.
- Operator and reviewer identities; change ticket; UTC start/end timestamps for each injected fault and recovery.
- Raw, redacted broker management events, relay/worker logs, PostgreSQL state snapshots, metrics/alert screenshots, and command outputs.
- Per-scenario PASS/FAIL/BLOCKED decision and explicit evidence pointers.
- Rollback/forward-recovery rehearsal and sign-off.

Hash the evidence bundle and record its SHA-256 in the Issue. Preserve original evidence; corrections should be additive and timestamped.

## 6. Stop conditions

Stop the rehearsal and keep dispatch disabled if any of the following occurs:

- staging/production boundary is ambiguous;
- candidate images are not pinned to immutable digests or are built from incompatible SHAs;
- a broker acknowledgement/redelivery policy cannot be established;
- duplicate logical Attempts, external side effects, or published Artifacts appear;
- stale lease holders can update authoritative state;
- accepted Jobs disappear or pending age/retry growth is unexplained;
- poison dispatches block healthy work or replay lacks an audit record;
- cancellation allows a new Attempt or side effect after the consumer's authoritative state check;
- secrets/customer data leak into logs, broker messages, metrics or evidence;
- rollback requires deleting outbox/workflow history or changing immutable identities.

## 7. Current execution result

**BLOCKED — NOT RUN.**

At preparation time, GitHub repository access exposed source, tests, and runbooks only. No staging deployment endpoint, cluster/container control plane, broker management access, PostgreSQL staging connection, image registry evidence, secret-manager access, or named staging operator evidence was available to this execution. Therefore no broker outage, process kill, relay concurrency, worker redelivery, poison replay, or rollback was performed.

PR #210 was still open at the inspected repository state. Its cancellation-before-publication changes are not part of the inspected `develop` baseline (`68eb623191c396863d0df544f8e32108737435bf`). Do not accept cancellation-before-publication based on this baseline; include the merged PR #210 commit in the pinned staging candidate and rerun the cancellation scenarios.

No deployment/image SHA, staging timestamps, non-secret staging configuration, operator identity, or pass result has been invented. Keep `WORKFLOW_DURABLE_DISPATCH_ENABLED=false`. Do not enable production dispatch or close Issue #212 until all gates have attached evidence and received operator/reviewer sign-off.
