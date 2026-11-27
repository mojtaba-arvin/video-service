# Incremental source ingestion contract

## Status

This is an opt-in internal vertical slice. It does not change the gRPC request
contract, legacy Redis/Celery path, or default VOD execution. The caller must
create a durable Job and an ingestion session explicitly. No production feature
flag is enabled by this change.

## Identity and persistence

PostgreSQL is authoritative for the ingestion session and chunk ledger. A chunk
is identified by `(ingestion_id, chunk_index)` and carries a stable caller
`chunk_id`, zero-based byte offset, byte length, SHA-256, optional media time
range, and immutable private S3 URI. The unique constraints prevent two
committed chunks from occupying one index or reusing one chunk identity in a
session.

The byte offset is exactly `chunk_index * chunk_size_bytes`. Every non-final
chunk must have the configured size; the final chunk must cover the exact
remaining source bytes. The reorder window is bounded by
`max_pending_chunks`, and the service also has a process-local bounded upload
semaphore. This is not a distributed tenant quota or a replacement for the
existing Job admission policy.

## Replay, resume and finalization

- An exact replay of the same chunk identity, index, offset, size, checksum and
  time range is a no-op.
- Reusing an index or chunk identity with different content/metadata is a
  conflict; the original committed chunk is never overwritten.
- A restart resumes from the durable chunk ledger. Missing indices remain
  missing; no gaps are inferred or silently filled.
- Finalization requires a declared total size, a complete ordered index set,
  exact byte coverage, per-chunk checksum verification, optional source
  checksum verification, and final object readback.
- Final object keys are immutable and attempt-scoped. Retrying finalization
  reuses the same immutable identity; it cannot overwrite different bytes.
- A checksum or storage failure leaves the session non-COMPLETE and the final
  Artifact URI unset. A FINALIZING session may be retried after the dependency
  recovers.

## Provisional versus final availability

Chunk rows and the progressive view are explicitly PROVISIONAL. The view only
exposes a contiguous prefix and never supplies a final Artifact URI. A caller
must declare an `InputContract` (minimum contiguous bytes, optional minimum
contiguous duration, and whether the complete source is mandatory) before
starting dependent work. The current slice provides this contract check but
does not dispatch or activate downstream Stage execution.

Only a COMPLETE session returns a final immutable source Artifact URI and
`final_artifact_id`. The final `workflow_artifacts` row is registered as
AVAILABLE in the same PostgreSQL transaction that marks the ingestion COMPLETE.
The existing VOD package/validation/publication path remains the final correctness
baseline. Provisional chunks are not registered as AVAILABLE workflow Artifacts
and must not be passed to existing VOD handlers as though they were complete
source media.

## Cancellation and cleanup

Cancellation is accepted during chunk receipt and source assembly. The service
rechecks the durable state after assembly and checksum validation, then enters
FINALIZING immediately before immutable final-object publication. Cancellation
during that short publication fence is rejected rather than racing final publication.
Cancelling an OPEN session marks it CANCELLED before deleting only the exact
chunk object URIs recorded for that session. Cleanup failures remain visible as
`cleanup_pending` and can be retried with `cleanup_cancelled()`. Completed
sources are immutable and cannot be cancelled by this ingestion operation.

## Measurement boundary

The repository's `synthetic-vod-v1` fixtures are generated correctness inputs,
not a representative licensed media corpus. They can exercise timing and
regression mechanics but cannot support production latency or throughput
claims. A performance report must pin the corpus manifest/hash, S3-compatible
provider/region, PostgreSQL version, object-store configuration, chunk size,
reorder/concurrency limits, repetitions and raw samples. Report time to first
contract-satisfying provisional prefix separately from time to final validated
Artifact. Compare against the whole-file `S3ArtifactStore.put_file` path using
the same inputs and storage. No performance gain is claimed by this increment.
