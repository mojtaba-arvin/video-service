"""Contract tests for versioned webhook HMAC signing primitives."""
import unittest

from video_streaming.security.webhook_signing import (
    DELIVERY_ID_HEADER,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    VERSION_HEADER,
    WebhookSignatureError,
    build_webhook_signature_headers,
    canonical_webhook_payload,
    sign_webhook,
    stable_delivery_id,
    verify_webhook_signature,
)


class WebhookSigningTests(unittest.TestCase):
    def setUp(self):
        self.secret = "s" * 48
        self.body = b'{"job_id":"job-1","status":"finished"}'
        self.delivery_id = stable_delivery_id("job-1", "publish")

    def test_delivery_id_is_stable_and_scoped_to_stage(self):
        self.assertEqual(
            self.delivery_id, stable_delivery_id("job-1", "publish")
        )
        self.assertNotEqual(
            self.delivery_id, stable_delivery_id("job-1", "callback")
        )
        # Delimiter-containing values must not create ambiguous tuple encodings.
        self.assertNotEqual(
            stable_delivery_id("job:one", "stage"),
            stable_delivery_id("job", "one:stage"),
        )
        self.assertNotIn("job-1", self.delivery_id)

    def test_canonical_payload_binds_version_timestamp_id_and_exact_body(self):
        payload = canonical_webhook_payload("1700000000", self.delivery_id, self.body)
        self.assertTrue(payload.startswith(
            ("v1\n1700000000\n" + self.delivery_id + "\n").encode("ascii")
        ))
        self.assertTrue(payload.endswith(self.body))
        self.assertNotEqual(
            sign_webhook(self.secret, "1700000000", self.delivery_id, self.body),
            sign_webhook(self.secret, "1700000000", self.delivery_id, self.body + b" "),
        )

    def test_headers_include_version_timestamp_stable_id_and_signature(self):
        headers = build_webhook_signature_headers(
            self.secret, self.delivery_id, self.body, timestamp=1700000000
        )
        self.assertEqual(headers[VERSION_HEADER], "v1")
        self.assertEqual(headers[TIMESTAMP_HEADER], "1700000000")
        self.assertEqual(headers[DELIVERY_ID_HEADER], self.delivery_id)
        self.assertEqual(headers[SIGNATURE_HEADER][:3], "v1=")
        self.assertTrue(verify_webhook_signature(
            self.secret,
            headers[TIMESTAMP_HEADER],
            headers[DELIVERY_ID_HEADER],
            self.body,
            headers[SIGNATURE_HEADER],
            now=1700000001,
            max_clock_skew_seconds=300,
        ))

    def test_same_delivery_id_can_be_resigned_with_fresh_attempt_timestamp(self):
        first = build_webhook_signature_headers(
            self.secret, self.delivery_id, self.body, timestamp=1700000000
        )
        retry = build_webhook_signature_headers(
            self.secret, self.delivery_id, self.body, timestamp=1700000010
        )
        self.assertEqual(first[DELIVERY_ID_HEADER], retry[DELIVERY_ID_HEADER])
        self.assertNotEqual(first[SIGNATURE_HEADER], retry[SIGNATURE_HEADER])

    def test_tampering_wrong_key_and_stale_timestamp_fail(self):
        signature = sign_webhook(self.secret, "1700000000", self.delivery_id, self.body)
        self.assertFalse(verify_webhook_signature(
            self.secret, "1700000000", self.delivery_id, self.body + b"x",
            signature, now=1700000000,
        ))
        self.assertFalse(verify_webhook_signature(
            "x" * 48, "1700000000", self.delivery_id, self.body,
            signature, now=1700000000,
        ))
        self.assertFalse(verify_webhook_signature(
            self.secret, "1700000000", self.delivery_id, self.body,
            signature, now=1700001000, max_clock_skew_seconds=300,
        ))

    def test_malformed_inputs_fail_closed(self):
        for secret in ("short", b"x" * 31):
            with self.subTest(secret_type=type(secret).__name__):
                with self.assertRaises(WebhookSignatureError):
                    sign_webhook(secret, "1700000000", self.delivery_id, self.body)
        with self.assertRaises(WebhookSignatureError):
            canonical_webhook_payload("01", self.delivery_id, self.body)
        with self.assertRaises(WebhookSignatureError):
            canonical_webhook_payload("1700000000", "bad id", self.body)
        with self.assertRaises(WebhookSignatureError):
            canonical_webhook_payload("1700000000", self.delivery_id, "{}")
        with self.assertRaises(WebhookSignatureError):
            verify_webhook_signature(
                self.secret, "1700000000", self.delivery_id, self.body,
                "v2=" + "0" * 64, now=1700000000,
            )


if __name__ == "__main__":
    unittest.main()
