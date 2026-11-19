# Abandoned package Attempt cleanup

The HLS/DASH publisher writes each package beneath an immutable key namespace:

`<logical-prefix>/.attempts/<publication-attempt-id>/...`

The package inventory is written before object transfer and lists every object owned by that Attempt. Cleanup reads that inventory; it never lists or deletes a shared bucket prefix.

## Eligibility

The `cleanup_package_attempt` Celery task delegates eligibility to PostgreSQL:

- A `FAILED` or `CANCELLED` Attempt must be older than the configured retention grace period.
- A `RETRYABLE` Attempt is eligible only after a newer Attempt has superseded it.
- Any `AVAILABLE` Artifact whose URI is inside the exact bucket/prefix blocks cleanup.
- Attempts with a live lease or an incomplete lease tuple are not reclaimable.
- The default grace period is 24 hours. Configure `WORKFLOW_PACKAGE_ORPHAN_RETENTION_SECONDS` between 3,600 and 2,592,000 seconds.

The task deletes the root manifest first, then the remaining objects listed in the Attempt inventory, and finally the inventory itself. If deletion fails part-way through, the inventory remains so the operation can be retried safely.

## Manual invocation

Run from the application environment with the workflow database and S3-compatible storage configured:

```bash
celery -A video_streaming.celery:celery_app call cleanup_package_attempt \
  --args='["<publication-attempt-id>","vod/master.m3u8","video-output"]'
```

Arguments are the publication Attempt ID, the original logical manifest key (not the attempt-scoped key), and the output bucket.

A result with `"deleted": false` means PostgreSQL has not yet approved reclamation. Do not bypass the database predicate or manually delete the Attempt prefix.

## Scheduling

The `cleanup_abandoned_packages` task is scheduled hourly through `CELERY_BEAT_SCHEDULE`. The Circus configuration starts a dedicated `celery beat` watcher alongside the worker. The repository records each package Attempt's bucket, logical manifest key, immutable prefix, and inventory key in its durable metrics metadata so the janitor can enumerate candidates without listing shared S3 prefixes.

The janitor asks PostgreSQL for old candidate Attempts, then rechecks the database predicate immediately before deletion. The single-attempt `cleanup_package_attempt` task remains available for controlled manual cleanup.
