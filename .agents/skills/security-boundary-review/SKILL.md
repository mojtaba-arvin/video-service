---
name: security-boundary-review
description: Use when changing auth, tenants, webhooks, URLs, local paths, subprocesses, uploads, secrets, logs, quotas, artifact publication, or CI workflows.
---

# Security boundary review

Assume callers control media bytes, metadata, job fields, filenames, webhook
URLs, storage references, and timing. Assume parsers and external services fail.

- Validate types, lengths, ranges, enums, and allow-lists at application edges.
- For webhooks, reject loopback/private/link-local/multicast/reserved and
  metadata-service addresses. Revalidate resolved IPs after DNS changes and
  every redirect to prevent rebinding.
- Apply connect/read timeouts, response-size caps, redirect limits, signatures,
  and replay protection.
- Use per-job directories and safe path joins; reject traversal and symlink
  escapes. Enforce tenant ownership for all artifacts.
- Launch subprocesses with argument arrays, restricted environment, explicit
  working directory, process-group supervision, timeout, and resource bounds.
- Redact credentials, signed URLs, metadata, and customer data from logs,
  errors, telemetry, and CI output.
- Authenticate and authorize every job/artifact operation; apply quotas before
  queue admission.
- Make publication atomic and idempotent; partial artifacts stay private.
- Keep workflow tokens least-privilege, pin actions to full SHAs, and never
  run untrusted PR code under privileged triggers.

A security fix is complete only with tests for exploit and valid behavior.
Record residual risk and operational mitigations; do not claim absolute safety.
