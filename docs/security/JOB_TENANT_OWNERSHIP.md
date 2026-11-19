# Job tenant ownership persistence

The durable workflow database records an explicit owner for newly created Jobs
when the trusted application boundary supplies a verified
`RequestPrincipal`.

The ownership tuple is:

- `owner_tenant_id`: tenant claim resolved by the configured authenticator;
- `owner_issuer`: exact trusted issuer;
- `owner_subject`: authenticated subject within that issuer.

The three fields are nullable only to preserve pre-migration Jobs. A database
constraint requires either all three values or none. Existing rows are not
backfilled from caller metadata, request payloads, reference IDs, or guesses.
An unowned legacy Job remains unowned and is denied by tenant-scoped
authorization.

`WorkflowRepository.create_job(..., owner_principal=...)` accepts only the
immutable `RequestPrincipal` value object and rejects expired credentials.
Idempotent replays supplied with a principal must match the persisted tenant,
issuer, and subject; cross-owner replay is denied with a non-enumerating
message. The repository also exposes `authorize_job_access` as the shared
resource boundary for Job-scoped operations.

## Rollout boundary

This increment does **not** authenticate JWTs, attach principals to gRPC
contexts, or force the existing legacy CreateJob RPC to provide a principal.
That wiring must land before authenticated production traffic is enabled.
Until then, callers omitting `owner_principal` create explicitly unowned
compatibility records. Do not treat this compatibility mode as tenant-safe.

No automatic legacy ownership migration is permitted. Any administrative
reconciliation must be explicit, audited, and separately authorized.
