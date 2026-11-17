# Security Policy

## Supported versions

Security fixes target the current develop branch. Older commits and historical
branches are not maintained release lines.

## Reporting a vulnerability

Do not publish exploitable details, credentials, signed URLs, or proof-of-concept
payloads in a public issue.

Use the repository Security tab's private vulnerability reporting flow when
available. Include the affected commit, impact, reproduction steps, and a
proposed mitigation. If private reporting is unavailable, contact the
maintainer through their GitHub profile and request a private disclosure
channel. Please allow coordinated triage and remediation before public
disclosure.

## Security-sensitive areas

Reports are especially welcome for:

- command/filter injection through media metadata or caller-controlled inputs;
- FFmpeg/FFprobe process escape, resource exhaustion, or cross-job file access;
- SSRF through webhook destinations, redirects, DNS resolution, or storage URLs;
- missing authentication, authorization, tenant isolation, or quotas;
- secrets exposed through logs, errors, CI artifacts, or repository history;
- unsafe artifact publication, path traversal, or retry/idempotency flaws.

Do not upload customer media or secrets to public issues or pull requests.
