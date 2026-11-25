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

The signing key is a distinct 32-byte-or-longer secret generated with a
cryptographically secure random source. The helper enforces length, not entropy. It must
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

The API accepts an optional `webhook_signing_secret` field on `JobRequest`.
When present, it is encrypted as a separate `webhook.signing` Job secret in the
same transaction as the callback destination and immutable execution plan.
Both the legacy Celery callback adapter and durable publication adapter resolve
that purpose-scoped secret only inside the trusted worker and sign the exact
UTF-8 request-body bytes with a stable per-output Delivery ID.

Omitting `webhook_signing_secret` is the explicit compatibility mode for
existing unsigned callback consumers. Supplying it is fail-closed: secret
resolution or signing errors fail the delivery; there is no unsigned fallback.

The sender integration does not provide receiver-side replay suppression. A
receiver must atomically persist each accepted Delivery ID with its event and
reject duplicates. The durable adapter currently writes a receipt only after
observing a 2xx response; that records the sender's observation, not proof that
the receiver committed its side effect. Durable failed-attempt history and
bounded receipt retention remain separate acceptance work.
