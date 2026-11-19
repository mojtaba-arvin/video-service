"""Unit tests for authenticated Job-scoped secret envelopes."""

import base64
import unittest
from datetime import datetime, timedelta, timezone

from video_streaming.security.secret_store import (
    SecretContext,
    SecretContextMismatch,
    SecretDecryptionError,
    SecretEnvelope,
    AwsKMSKeyProvider,
    prepare_secret,
    resolve_secret,
)


class FakeKeyProvider:
    key_id = "kms-test-key"

    def __init__(self):
        self.data_key = bytearray(range(32))
        self.contexts = []
        self.decryption_key_ids = []

    def generate_data_key(self, encryption_context):
        self.contexts.append(dict(encryption_context))
        # Test-only wrapping: production must use AwsKMSKeyProvider.
        wrapped = bytes(value ^ 0xA5 for value in self.data_key)
        return bytearray(self.data_key), wrapped

    def decrypt_data_key(self, wrapped_key, encryption_context, key_id=None):
        self.contexts.append(dict(encryption_context))
        self.decryption_key_ids.append(key_id)
        return bytearray(value ^ 0xA5 for value in wrapped_key)



class FakeKMSClient:
    def __init__(self):
        self.generate_args = None
        self.decrypt_args = None

    def generate_data_key(self, **kwargs):
        self.generate_args = kwargs
        return {"Plaintext": bytes(range(32)), "CiphertextBlob": b"wrapped-key"}

    def decrypt(self, **kwargs):
        self.decrypt_args = kwargs
        return {"Plaintext": bytes(range(32))}


class SecretEnvelopeTests(unittest.TestCase):
    def setUp(self):
        self.context = SecretContext(
            job_id="job-123",
            tenant_id="tenant-a",
            issuer="https://issuer.example.test/",
            subject="subject-7",
            purpose="webhook.callback",
        )
        self.provider = FakeKeyProvider()

    def test_aws_kms_adapter_uses_explicit_key_and_encryption_context(self):
        client = FakeKMSClient()
        provider = AwsKMSKeyProvider(client, "arn:aws:kms:region:account:key/current")
        context = {"job_id": "job-123", "purpose": "webhook.callback"}

        plaintext, wrapped = provider.generate_data_key(context)
        self.assertEqual(len(plaintext), 32)
        self.assertEqual(wrapped, b"wrapped-key")
        self.assertEqual(
            client.generate_args["KeyId"],
            "arn:aws:kms:region:account:key/current",
        )
        self.assertEqual(client.generate_args["KeySpec"], "AES_256")
        self.assertEqual(client.generate_args["EncryptionContext"], context)

        decrypted = provider.decrypt_data_key(
            wrapped, context, key_id="arn:aws:kms:region:account:key/previous"
        )
        self.assertEqual(bytes(decrypted), bytes(range(32)))
        self.assertEqual(
            client.decrypt_args["KeyId"],
            "arn:aws:kms:region:account:key/previous",
        )
        self.assertEqual(client.decrypt_args["EncryptionContext"], context)

    def test_round_trip_binds_secret_to_full_context(self):
        envelope = prepare_secret(
            "https://callback.example.test/hook?token=not-logged",
            self.context,
            self.provider,
        )

        self.assertIsInstance(envelope, SecretEnvelope)
        self.assertNotIn(
            "https://callback.example.test/hook?token=not-logged",
            repr(envelope),
        )
        self.assertNotIn(
            "https://callback.example.test/hook?token=not-logged",
            repr(envelope.to_dict()),
        )
        self.assertEqual(
            resolve_secret(envelope, self.context, self.provider),
            "https://callback.example.test/hook?token=not-logged",
        )
        self.assertEqual(self.provider.decryption_key_ids, ["kms-test-key"])

    def test_cross_job_or_tenant_resolution_is_denied_before_kms(self):
        envelope = prepare_secret("sensitive", self.context, self.provider)
        calls_before = len(self.provider.contexts)
        other = SecretContext(
            job_id="job-456",
            tenant_id="tenant-b",
            issuer=self.context.issuer,
            subject=self.context.subject,
            purpose=self.context.purpose,
        )

        with self.assertRaises(SecretContextMismatch):
            resolve_secret(envelope, other, self.provider)
        self.assertEqual(len(self.provider.contexts), calls_before)

    def test_expired_secret_is_not_decrypted(self):
        expired = datetime.now(timezone.utc) - timedelta(seconds=1)
        envelope = prepare_secret(
            "sensitive", self.context, self.provider, expires_at=expired
        )
        calls_before = len(self.provider.contexts)

        with self.assertRaises(SecretContextMismatch):
            resolve_secret(envelope, self.context, self.provider)
        self.assertEqual(len(self.provider.contexts), calls_before)

    def test_ciphertext_tampering_fails_authenticated_decryption(self):
        envelope = prepare_secret("sensitive", self.context, self.provider)
        tampered = SecretEnvelope(
            secret_id=envelope.secret_id,
            context=envelope.context,
            nonce=envelope.nonce,
            ciphertext=base64.urlsafe_b64encode(b"tampered").decode("ascii"),
            wrapped_dek=envelope.wrapped_dek,
            kms_key_id=envelope.kms_key_id,
            created_at=envelope.created_at,
        )

        with self.assertRaises(SecretDecryptionError):
            resolve_secret(tampered, self.context, self.provider)

    def test_empty_and_oversized_secret_values_are_rejected(self):
        with self.assertRaises(ValueError):
            prepare_secret("", self.context, self.provider)
        with self.assertRaises(ValueError):
            prepare_secret("x" * (16 * 1024 + 1), self.context, self.provider)

    def test_expiry_must_be_timezone_aware(self):
        with self.assertRaises(ValueError):
            prepare_secret(
                "sensitive",
                self.context,
                self.provider,
                expires_at=datetime(2026, 1, 1),
            )


if __name__ == "__main__":
    unittest.main()
