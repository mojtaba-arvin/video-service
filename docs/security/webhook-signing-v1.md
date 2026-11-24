# Webhook signing contract (v1)

The HMAC primitive lives in `video_streaming.security.webhook_signing`.
It is intentionally separate from callback URL handling.

## Wire format

- `X-Video-Webhook-Version: v1`
- `X-Video-Webhook-Timestamp: <Unix seconds>`
- `X-Video-Webhook-Delivery-Id: <stable opaque ID>`
- `X-Video-Webhook-Signature: v1=<lowercase hex HMAC-SHA256>`

The canonical byte sequence is exactly:

```text
v1\n<timestamp>\n<delivery-id>\n<exact HTTP request body bytes>
```

The signing key is a distinct secret with at least 32 bytes of entropy. It must
not be derived from, reused as, or stored under the `webhook.callback`
destination-URL secret purpose. The API must persist it under a separate
`webhook.signing` purpose and expose only its opaque reference in the plan.

## Retry and replay semantics

A delivery ID is deterministic for the Job + callback Stage and therefore
remains unchanged across retries. Each attempt uses a fresh timestamp and
signature over the exact bytes sent. Receivers verify HMAC with a constant-time
comparison and enforce a bounded clock-skew window.

Timestamp validation is not replay protection. A receiver must atomically
insert the delivery ID into a durable unique-key store with its accepted event;
a duplicate ID is rejected even when its timestamp and HMAC are valid. Sender
signing alone cannot guarantee receiver-side exactly-once effects.

## Integration and rollout gates

The current helper and its unit tests establish only the canonical HMAC
primitive. They do not activate signed delivery or prove durable replay
suppression. Before enabling signing in either legacy Celery or durable outbox
delivery, the service still needs:

1. A separate encrypted `webhook.signing` Job secret, atomically persisted
   with the Job and execution plan, plus worker-only purpose-scoped resolution.
2. Signing in every delivery adapter using the exact serialized body bytes and
   the executor's stable delivery identity.
3. Durable delivery-attempt/result receipts and bounded retention.
4. A receiver-side dedupe contract and an explicit compatibility policy for
   existing unsigned consumers. No silent unsigned fallback when signing is
   required.
5. Integration tests for secret isolation, retry identity, body tampering,
   timestamp skew, concurrent duplicate acceptance, and failure recovery.

Until those gates pass, the helper is not wired into production delivery and
must not be described as end-to-end webhook authentication or replay prevention.
