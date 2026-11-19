"""Authenticated, Job-scoped secret envelopes backed by an external KMS.

This module deliberately provides cryptographic primitives only. It does not
persist secrets, authorize callers, resolve webhook destinations, or activate
legacy callback delivery. Those operations must be integrated atomically with
the workflow repository and the service authorization boundary.
"""

import base64
import json
import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone


ALGORITHM = "AES-256-GCM"
NONCE_BYTES = 12
MAX_SECRET_BYTES = 16 * 1024
_SECRET_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{40,64}$")


class SecretStoreError(Exception):
    """Base class for secret-envelope failures."""


class SecretContextMismatch(SecretStoreError):
    """The requested Job/tenant/purpose does not match the envelope."""


class SecretDecryptionError(SecretStoreError):
    """The KMS or authenticated-decryption operation failed."""


def _required_text(value, field, max_length=512):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("{} must be a non-empty string".format(field))
    if len(value) > max_length:
        raise ValueError("{} exceeds {} characters".format(field, max_length))
    return value


@dataclass(frozen=True)
class SecretContext:
    job_id: str
    tenant_id: str
    issuer: str
    subject: str
    purpose: str

    def __post_init__(self):
        for field in ("job_id", "tenant_id", "issuer", "subject", "purpose"):
            _required_text(getattr(self, field), field)


@dataclass(frozen=True)
class SecretEnvelope:
    secret_id: str
    context: SecretContext
    nonce: str
    ciphertext: str
    wrapped_dek: str
    kms_key_id: str
    created_at: str
    expires_at: str = None
    algorithm: str = ALGORITHM

    def __post_init__(self):
        if not _SECRET_ID_PATTERN.match(self.secret_id or ""):
            raise ValueError("secret_id is invalid")
        if self.algorithm != ALGORITHM:
            raise ValueError("unsupported secret-envelope algorithm")
        _required_text(self.nonce, "nonce", 64)
        _required_text(self.ciphertext, "ciphertext", MAX_SECRET_BYTES * 2)
        _required_text(self.wrapped_dek, "wrapped_dek", 16384)
        _required_text(self.kms_key_id, "kms_key_id", 512)
        _required_text(self.created_at, "created_at", 64)
        if self.expires_at is not None:
            _required_text(self.expires_at, "expires_at", 64)

    def to_dict(self):
        """Return only the encrypted envelope fields suitable for DB storage."""
        return {
            "secret_id": self.secret_id,
            "job_id": self.context.job_id,
            "tenant_id": self.context.tenant_id,
            "issuer": self.context.issuer,
            "subject": self.context.subject,
            "purpose": self.context.purpose,
            "algorithm": self.algorithm,
            "nonce": self.nonce,
            "ciphertext": self.ciphertext,
            "wrapped_dek": self.wrapped_dek,
            "kms_key_id": self.kms_key_id,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }


def _context_bytes(secret_id, context, algorithm=ALGORITHM):
    value = {
        "algorithm": algorithm,
        "job_id": context.job_id,
        "issuer": context.issuer,
        "purpose": context.purpose,
        "secret_id": secret_id,
        "subject": context.subject,
        "tenant_id": context.tenant_id,
    }
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


class AwsKMSKeyProvider:
    """Envelope-encryption key provider using AWS KMS GenerateDataKey/Decrypt."""

    def __init__(self, kms_client, key_id):
        if kms_client is None:
            raise ValueError("kms_client is required")
        self._kms = kms_client
        self._key_id = _required_text(key_id, "key_id")

    @property
    def key_id(self):
        return self._key_id

    def generate_data_key(self, encryption_context):
        try:
            response = self._kms.generate_data_key(
                KeyId=self._key_id,
                KeySpec="AES_256",
                EncryptionContext=encryption_context,
            )
            return bytearray(response["Plaintext"]), bytes(response["CiphertextBlob"])
        except Exception as exc:
            raise SecretDecryptionError(
                "KMS data-key generation failed"
            ) from exc

    def decrypt_data_key(self, wrapped_key, encryption_context, key_id=None):
        try:
            response = self._kms.decrypt(
                CiphertextBlob=wrapped_key,
                KeyId=key_id or self._key_id,
                EncryptionContext=encryption_context,
            )
            return bytearray(response["Plaintext"])
        except Exception as exc:
            raise SecretDecryptionError(
                "KMS data-key decryption failed"
            ) from exc


def _kms_context(secret_id, context):
    # KMS encryption context values are non-secret and authenticated by KMS.
    return {
        "secret_id": secret_id,
        "job_id": context.job_id,
        "tenant_id": context.tenant_id,
        "issuer": context.issuer,
        "subject": context.subject,
        "purpose": context.purpose,
        "algorithm": ALGORITHM,
    }


def _b64encode(value):
    return base64.urlsafe_b64encode(value).decode("ascii")


def _b64decode(value, field):
    try:
        return base64.urlsafe_b64decode(value.encode("ascii"))
    except (AttributeError, UnicodeEncodeError, ValueError) as exc:
        raise SecretDecryptionError(
            "Stored secret envelope is invalid"
        ) from exc


def prepare_secret(value, context, key_provider, expires_at=None):
    """Encrypt a secret with a fresh DEK and nonce; return an opaque envelope.

    The caller must persist the returned envelope in the same database
    transaction as the owning Job and must place only secret_id in plans.
    """
    if not isinstance(context, SecretContext):
        raise TypeError("context must be a SecretContext")
    if not isinstance(value, str) or not value:
        raise ValueError("secret value must be a non-empty string")
    plaintext = value.encode("utf-8")
    if len(plaintext) > MAX_SECRET_BYTES:
        raise ValueError("secret value exceeds the configured size limit")
    if key_provider is None:
        raise ValueError("key_provider is required")

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    secret_id = secrets.token_urlsafe(32)
    kms_context = _kms_context(secret_id, context)
    data_key = None
    try:
        data_key, wrapped_key = key_provider.generate_data_key(kms_context)
        if len(data_key) != 32:
            raise SecretDecryptionError("KMS returned an invalid AES-256 data key")
        nonce = os.urandom(NONCE_BYTES)
        ciphertext = AESGCM(bytes(data_key)).encrypt(
            nonce, plaintext, _context_bytes(secret_id, context)
        )
        now = datetime.now(timezone.utc).isoformat()
        expiry = None
        if expires_at is not None:
            if not isinstance(expires_at, datetime):
                raise TypeError("expires_at must be a datetime")
            if expires_at.tzinfo is None or expires_at.utcoffset() is None:
                raise ValueError("expires_at must be timezone-aware")
            expiry = expires_at.astimezone(timezone.utc).isoformat()
        return SecretEnvelope(
            secret_id=secret_id,
            context=context,
            nonce=_b64encode(nonce),
            ciphertext=_b64encode(ciphertext),
            wrapped_dek=_b64encode(wrapped_key),
            kms_key_id=key_provider.key_id if hasattr(key_provider, "key_id") else "provider-managed",
            created_at=now,
            expires_at=expiry,
        )
    finally:
        if data_key is not None:
            for index in range(len(data_key)):
                data_key[index] = 0


def resolve_secret(envelope, expected_context, key_provider, now=None):
    """Decrypt only when the full Job/tenant/issuer/subject/purpose scope matches."""
    if not isinstance(envelope, SecretEnvelope):
        raise TypeError("envelope must be a SecretEnvelope")
    if not isinstance(expected_context, SecretContext):
        raise TypeError("expected_context must be a SecretContext")
    if envelope.context != expected_context:
        raise SecretContextMismatch("Secret is not available in this context")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if envelope.expires_at:
        expiry = datetime.fromisoformat(envelope.expires_at)
        if expiry.tzinfo is None or expiry.utcoffset() is None:
            raise SecretDecryptionError("Stored secret expiry is invalid")
        if current >= expiry:
            raise SecretContextMismatch("Secret is expired")
    if key_provider is None:
        raise ValueError("key_provider is required")

    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    data_key = None
    try:
        kms_context = _kms_context(envelope.secret_id, envelope.context)
        data_key = key_provider.decrypt_data_key(
            _b64decode(envelope.wrapped_dek, "wrapped_dek"),
            kms_context,
            key_id=envelope.kms_key_id,
        )
        if len(data_key) != 32:
            raise SecretDecryptionError("KMS returned an invalid AES-256 data key")
        plaintext = AESGCM(bytes(data_key)).decrypt(
            _b64decode(envelope.nonce, "nonce"),
            _b64decode(envelope.ciphertext, "ciphertext"),
            _context_bytes(envelope.secret_id, envelope.context, envelope.algorithm),
        )
        return plaintext.decode("utf-8")
    except SecretStoreError:
        raise
    except (InvalidTag, UnicodeDecodeError, ValueError, TypeError) as exc:
        raise SecretDecryptionError("Secret envelope authentication failed") from exc
    finally:
        if data_key is not None:
            for index in range(len(data_key)):
                data_key[index] = 0
