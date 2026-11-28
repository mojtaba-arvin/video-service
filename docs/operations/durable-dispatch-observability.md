# Durable dispatch observability and staging evidence

## Status

This is the operational contract for Issue #215, not evidence that a staging
deployment has been exercised. The durable dispatch flag remains
`WORKFLOW_DURABLE_DISPATCH_ENABLED=false`. Do not deploy the relay schedule,
change CreateJob routing, or enable durable dispatch until every staging row
below has a retained artifact and operator/reviewer sign-off.

PostgreSQL remains authoritative for Outbox state and append-only workflow
events. Celery/Redis are transport/projection layers. Delivery is at-least-once;
operators must expect redelivery and verify idempotent effects.

## Current code evidence versus environment evidence

The current repository implementation provides:
- PostgreSQL state counts and nullable oldest-pending age through
  `WorkflowRepository.get_outbox_metrics()`;
- durable `OUTBOX_PUBLISHED`, `OUTBOX_RETRY_SCHEDULED` and
  `OUTBOX_DEAD_LETTERED` workflow events;
- bounded, non-secret `stats`, `dead`, and `inspect` operator reads;
- a manual replay operation restricted to DEAD rows, recording operator
  identity and reason in `OUTBOX_MANUAL_REPLAY_REQUESTED`;
- a release runbook that requires measured baselines and keeps the feature
  gate off.

The following are not established by those code-level contracts alone:
- a deployed scrape endpoint/exporter and live dashboard;
- a deployed alert receiver, owner, escalation route or alert delivery test;
- staging baseline samples, image digest, broker topology and rollback rehearsal;
- cancellation and lease-loss durable event coverage until the cancellation
  increment is merged and the relay records the corresponding outcomes.

Do not convert missing evidence into a zero measurement or a passing check.
The Prometheus rules in `alerts/durable-dispatch.rules.yml` are templates
until their metric exporter and threshold gauges are deployed and verified.

## Metric contract

Expose only bounded labels:
- `state`: `pending|processing|published|dead|cancelled`;
- `outcome`: `publish|retry|dead_letter|cancel|lease_loss`;
- `component`: fixed component names such as `relay`.

Required samples:
- `video_workflow_outbox_events{state}` — current PostgreSQL row count;
- `video_workflow_outbox_oldest_pending_age_seconds` — age of oldest PENDING
  row, omitted when no pending row exists (absence is not zero);
- `video_workflow_outbox_outcomes_total{outcome}` — cumulative durable outcome
  count from PostgreSQL workflow events;
- `video_workflow_outbox_telemetry_last_success_timestamp_seconds` — last
  successful exporter/database observation;
- `video_workflow_outbox_alert_threshold_seconds{signal}` — threshold values
  populated only from the accepted staging baseline;
- `video_workflow_outbox_alert_threshold_total{signal}` — count threshold
  populated only from the accepted staging baseline.

Never attach Job, Workflow, Outbox, tenant, operator, URL, filename, exception
text, plan fingerprint, or broker task IDs as metric labels. Keep correlation
identifiers in access-controlled logs and PostgreSQL events.

The exporter must query PostgreSQL directly, expose state and outcome gauges
without mutating workflow state, use a bounded database timeout, and fail
closed on database errors. A failed scrape must not publish zeroes.

## Staging baseline procedure

Run only with the feature gate off first. Capture raw timestamped samples from
the same deployed API/relay/worker images and the intended PostgreSQL and
broker topology. Record workload class, request/dispatch volume, test window,
deployment SHA, image digest, database revision, broker type/configuration,
scrape interval, missing-sample coverage, and operator.

Collect enough representative idle, normal-load, burst, broker-degraded,
relay-restart and worker-loss periods to describe normal variance and recovery.
Do not set numeric targets from synthetic CI or an arbitrary rule default.
The owner and reviewer must derive threshold gauges from the accepted raw
baseline, document the measurement window and exclusions, and retain the
source dashboard/export.

| Signal | Baseline evidence required | Threshold owner decision |
|---|---|---|
| Oldest pending age / queue depth | Time series under idle, normal, burst and recovery | Pending |
| Lease expiry / lease loss | Count and rate during normal operation and controlled relay termination | Pending |
| Retry exhaustion / DEAD rows | Expected retry lifecycle and poison-message drill | Pending |
| Relay publish failures | Broker outage/recovery and acknowledgement-loss drill | Pending |
| Missing telemetry | Scrape/exporter outage and recovery | Pending |

## Alert exercises

After baseline approval and after the exporter is deployed, exercise each
alert through the real staging receiver and record the alert ID, firing and
resolved timestamps, route, on-call acknowledgement, runbook link and
redacted evidence:
1. oldest pending age exceeds its baseline-derived threshold;
2. lease-loss/expiry count exceeds its baseline-derived threshold;
3. a row reaches DEAD / retry exhaustion;
4. relay publish failures exceed their baseline-derived threshold;
5. exporter samples disappear or the PostgreSQL observation becomes stale.

A successful unit test of an expression is not proof of alert delivery. Do not
silence or route these alerts to an unowned destination.

## Safe operator workflow

Use the restricted host/container identity and database role described by the
release runbook. Do not expose the CLI as a public RPC.

```sh
cd video-streaming
PYTHONPATH=. python -m video_streaming.workflows.outbox_admin stats
PYTHONPATH=. python -m video_streaming.workflows.outbox_admin dead --limit 100
PYTHONPATH=. python -m video_streaming.workflows.outbox_admin inspect <outbox-id>
PYTHONPATH=. python -m video_streaming.workflows.outbox_admin replay <outbox-id> \
  --operator-id '<operator identity>' \
  --reason '<incident/change ticket and reviewed reason>'
```

Replay is only for an inspected DEAD row after root cause is corrected. Verify
the immutable plan fingerprint and in-flight plan compatibility first.
Replay must be approved, attributable and auditable; it resets the row's
delivery attempt budget, so record the previous state and reason in the
incident record. Never replay PENDING, PROCESSING, PUBLISHED or CANCELLED rows.

## Rollback and forward recovery

1. Stop new durable acceptance first: keep or set
   `WORKFLOW_DURABLE_DISPATCH_ENABLED=false` and keep CreateJob on
   `legacy_celery`. Confirm no new durable dispatch rows are being created.
2. Do not delete, truncate, downgrade away, or rewrite PostgreSQL Outbox,
   Workflow, Attempt, event or artifact history.
3. Inventory PENDING, PROCESSING, DEAD and CANCELLED rows; identify active
   leases, accepted broker messages, worker claims and completed side effects.
4. Preserve in-flight plan schema/version/fingerprint compatibility. Roll back
   application images only to a version that can read every persisted plan and
   state transition; otherwise deploy a forward-compatible fix.
5. Let valid in-flight work finish or reconcile it through reviewed,
   idempotent recovery. Do not bulk replay or republish merely to clear backlog.
6. Verify PostgreSQL state, event history, broker redelivery and downstream
   idempotency before resuming the legacy path or any future staged cohort.
7. Record before/after image digests, migration revisions, row counts, event
   counts, artifact checksums and operator/reviewer sign-off.

A database downgrade that removes outbox history is not an acceptable rollback.
Prefer application rollback with additive schema retained, or a reviewed
forward migration.

## Evidence record (must be completed in staging)

| Evidence | Required value / link | Status |
|---|---|---|
| Environment / region / timestamp | Exact staging identity | Missing |
| API / relay / worker image digests | Immutable digests for one release candidate | Missing |
| Git commit and migration revision | Exact SHA and Alembic head | Missing |
| Dashboard and raw baseline export | Read-only links plus raw samples | Missing |
| Alert rules and alert receiver | Deployed config SHA and route owner | Missing |
| Alert exercise results | Firing/resolution and acknowledgement evidence | Missing |
| Redaction regression | Sanitized credential, signed URL and customer-data test output | Missing |
| Broker outage / relay crash / worker-loss | Run IDs and redacted logs | Missing |
| Manual inspection and replay | Operator, incident, durable event ID | Missing |
| Rollback / forward recovery | Before/after inventory and compatibility evidence | Missing |
| Operator and independent reviewer | Explicit sign-off | Missing |

## Related procedures

- [Durable VOD release and rollback runbook](durable-vod-release-runbook.md)
- [Workflow outbox relay operations](workflow-outbox-relay.md)
- [Reliability and observability standard](../engineering/reliability-observability-standard.md)
- [Issue #215](https://github.com/mojtaba-arvin/video-service/issues/215)
