"""Versioned HMAC primitives for authenticated webhook deliveries.

This module deliberately does not resolve secrets, persist delivery state, or
claim replay prevention. Callers must supply a separately stored signing key
and a stable delivery ID; receivers must atomically persist accepted delivery
IDs to reject replays.
"""
import hashlib
import hmac
import re
import time
import uuid


SIGNATURE_VERSION = "v1"
SIGNATURE_HEADER = "X-Video-Webhook-Signature"
VERSION_HEADER = "X-Video-Webhook-Version"
TIMESTAMP_HEADER = "X-Video-Webhook-Timestamp"
DELIVERY_ID_HEADER = "X-Video-Webhook-Delivery-Id"
_DELIVERY_NAMESPACE = uuid.UUID("b1c16e56-8c95-4c1b-9f0b-caa4d04e39b2")
_DELIVERY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SIGNATURE_RE = re.compile(r"^v1=[0-9a-f]{64}$")


class WebhookSignatureError(ValueError):
    """A webhook signature or its canonical input is invalid."""


def stable_delivery_id(job_id, stage_key):
    """Return a deterministic opaque ID reused for every retry of one stage."""
    if not isinstance(job_id, str) or not job_id.strip():
        raise WebhookSignatureError("job_id is required")
    if not isinstance(stage_key, str) or not stage_key.strip():
        raise WebhookSignatureError("stage_key is required")
    identity = "video-webhook:v1:{}:{}".format(job_id, stage_key)
    return "whd_" + uuid.uuid5(_DELIVERY_NAMESPACE, identity).hex


def _delivery_id(value):
    if not isinstance(value, str) or not _DELIVERY_ID_RE.fullmatch(value):
        raise WebhookSignatureError("delivery ID is invalid")
    return value


def _timestamp(value):
    if isinstance(value, bool):
        raise WebhookSignatureError("timestamp is invalid")
    if isinstance(value, int):
        value = str(value)
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"(0|[1-9][0-9]{0,11})", value)
    ):
        raise WebhookSignatureError("timestamp is invalid")
    return value


def _body_bytes(body):
    if isinstance(body, bytes):
        return body
    if isinstance(body, bytearray):
        return bytes(body)
    raise WebhookSignatureError("webhook body must be the exact transmitted bytes")


def _key_bytes(signing_secret):
    if isinstance(signing_secret, str):
        try:
            signing_secret = signing_secret.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise WebhookSignatureError("signing secret is invalid") from exc
    if not isinstance(signing_secret, bytes) or len(signing_secret) < 32:
        raise WebhookSignatureError("signing secret must contain at least 32 bytes")
    return signing_secret


def canonical_webhook_payload(timestamp, delivery_id, body):
    """Build the unambiguous v1 signing input from exact transmitted bytes."""
    timestamp = _timestamp(timestamp)
    delivery_id = _delivery_id(delivery_id)
    body = _body_bytes(body)
    return (
        SIGNATURE_VERSION.encode("ascii")
        + b"\n"
        + timestamp.encode("ascii")
        + b"\n"
        + delivery_id.encode("ascii")
        + b"\n"
        + body
    )


def sign_webhook(signing_secret, timestamp, delivery_id, body):
    """Return a v1 HMAC-SHA256 signature; never log the returned value."""
    digest = hmac.new(
        _key_bytes(signing_secret),
        canonical_webhook_payload(timestamp, delivery_id, body),
        hashlib.sha256,
    ).hexdigest()
    return "v1=" + digest


def build_webhook_signature_headers(signing_secret, delivery_id, body, timestamp=None):
    """Create headers for one attempt; retries retain ID but use a fresh timestamp."""
    timestamp = str(int(time.time())) if timestamp is None else _timestamp(timestamp)
    delivery_id = _delivery_id(delivery_id)
    return {
        VERSION_HEADER: SIGNATURE_VERSION,
        TIMESTAMP_HEADER: timestamp,
        DELIVERY_ID_HEADER: delivery_id,
        SIGNATURE_HEADER: sign_webhook(
            signing_secret, timestamp, delivery_id, _body_bytes(body)
        ),
    }


def verify_webhook_signature(
        signing_secret, timestamp, delivery_id, body, signature,
        now=None, max_clock_skew_seconds=300):
    """Verify v1 HMAC and bounded clock skew using constant-time comparison.

    This verifies authenticity and freshness only. A receiver must additionally
    insert delivery_id into a durable unique-key store in the same transaction
    as the accepted event; otherwise a captured request can be replayed inside
    the allowed clock window.
    """
    timestamp_text = _timestamp(timestamp)
    delivery_id = _delivery_id(delivery_id)
    if not isinstance(max_clock_skew_seconds, int) or isinstance(
            max_clock_skew_seconds, bool) or max_clock_skew_seconds < 0:
        raise WebhookSignatureError("clock skew limit is invalid")
    current = int(time.time()) if now is None else now
    if isinstance(current, bool) or not isinstance(current, int):
        raise WebhookSignatureError("verification time is invalid")
    issued_at = int(timestamp_text)
    if abs(current - issued_at) > max_clock_skew_seconds:
        return False
    if not isinstance(signature, str) or not _SIGNATURE_RE.fullmatch(signature):
        return False
    expected = sign_webhook(
        signing_secret, timestamp_text, delivery_id, _body_bytes(body)
    )
    return hmac.compare_digest(expected, signature)
