"""SQLite-backed contract tests for atomic Job secret persistence."""

import os
import unittest
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import create_engine, event
from sqlalchemy.exc import IntegrityError

from video_streaming.security import RequestPrincipal, AuthorizationError
from video_streaming.security.secret_store import (
    SecretContext,
    prepare_secret,
    resolve_secret,
)
from video_streaming.workflows.domain import IdempotencyConflict
from video_streaming.workflows.repository import (
    JobRecord,
    JobSecretRecord,
    WorkflowEventRecord,
    WorkflowRepository,
    utcnow,
)
from video_streaming.workflows.worker_identity import (
    WorkflowWorkerIdentity,
)


class FakeKeyProvider:
    key_id = "kms-test-key"

    def __init__(self):
        self._key = bytearray(range(32))

    def generate_data_key(self, encryption_context):
        wrapped = bytes(value ^ 0x5A for value in self._key)
        return bytearray(self._key), wrapped

    def decrypt_data_key(self, wrapped_key, encryption_context, key_id=None):
        if key_id != self.key_id:
            raise ValueError("unexpected KMS key id")
        return bytearray(value ^ 0x5A for value in wrapped_key)


class JobSecretRepositoryTests(unittest.TestCase):
    def setUp(self):
        database_url = os.environ.get("WORKFLOW_DATABASE_URL")
        if database_url:
            self.engine = create_engine(
                database_url,
                pool_pre_ping=True,
                future=True,
            )
        else:
            self.engine = create_engine(
                "sqlite:///:memory:",
                connect_args={"check_same_thread": False},
                future=True,
            )

            @event.listens_for(self.engine, "connect")
            def enable_foreign_keys(connection, _record):
                cursor = connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()

        self.repository = WorkflowRepository(
            None, engine=self.engine, create_schema=True
        )
        self.provider = FakeKeyProvider()
        self.principal = self.make_principal()

    def tearDown(self):
        self.repository.close()

    @staticmethod
    def make_principal(tenant="tenant-a", subject="subject-1"):
        return RequestPrincipal(
            issuer="https://issuer.example.test/",
            subject=subject,
            tenant_id=tenant,
            scopes=frozenset([
                "video.jobs:create",
                "video.jobs:callback:resolve",
            ]),
            roles=frozenset(),
            authentication_method="bearer",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )

    def envelope(
            self, job_id, principal=None, value="https://callback.invalid/hook",
            purpose="webhook.callback"):
        owner = principal or self.principal
        return prepare_secret(
            value,
            SecretContext(
                job_id=job_id,
                tenant_id=owner.tenant_id,
                issuer=owner.issuer,
                subject=owner.subject,
                purpose=purpose,
            ),
            self.provider,
        )

    def create_owned_job(self, job_id, key=None, envelope=None, stage_specs=None):
        return self.repository.create_job(
            job_id=job_id,
            idempotency_key=key or "idem-{}".format(uuid4()),
            request_hash="a" * 64,
            owner_principal=self.principal,
            secret_envelopes=[envelope] if envelope else [],
            stage_specs=stage_specs,
        )

    def test_job_secret_and_job_commit_atomically_and_resolve_by_owner(self):
        job_id = str(uuid4())
        plaintext = "https://callback.invalid/hook?token=must-not-persist"
        envelope = self.envelope(job_id, value=plaintext)

        self.assertEqual(
            self.create_owned_job(job_id, envelope=envelope),
            (job_id, True),
        )

        with self.repository._session() as session:
            job = session.get(JobRecord, job_id)
            secret = session.get(JobSecretRecord, envelope.secret_id)
            self.assertEqual(job.owner_tenant_id, self.principal.tenant_id)
            self.assertIsNotNone(secret)
            self.assertNotIn(plaintext, secret.ciphertext)
            self.assertNotIn(plaintext, repr(secret))
            self.assertIsNotNone(job)

        stored = self.repository.get_job_secret_envelope(
            job_id,
            self.principal,
            "webhook.callback",
        )
        self.assertEqual(stored.secret_id, envelope.secret_id)
        self.assertEqual(
            resolve_secret(stored, envelope.context, self.provider),
            plaintext,
        )

    def test_callback_and_signing_secrets_persist_under_distinct_purposes(self):
        job_id = str(uuid4())
        callback = self.envelope(job_id, purpose="webhook.callback")
        signing = self.envelope(job_id, value="s" * 48, purpose="webhook.signing")
        self.repository.create_job(
            job_id=job_id,
            idempotency_key="idem-two-secrets-{}".format(uuid4()),
            request_hash="c" * 64,
            owner_principal=self.principal,
            secret_envelopes=[callback, signing],
        )
        for purpose, envelope in (("webhook.callback", callback), ("webhook.signing", signing)):
            stored = self.repository.get_job_secret_envelope(job_id, self.principal, purpose)
            self.assertEqual(stored.secret_id, envelope.secret_id)
            self.assertEqual(stored.context.purpose, purpose)

    def test_secret_write_requires_authenticated_owner_and_exact_scope(self):
        job_id = str(uuid4())
        with self.assertRaises(ValueError):
            self.repository.create_job(
                job_id,
                "idem-no-owner-{}".format(uuid4()),
                "b" * 64,
                secret_envelopes=[self.envelope(job_id)],
            )

        wrong_owner = self.make_principal(tenant="tenant-b")
        wrong_scope_envelope = self.envelope(job_id, principal=wrong_owner)
        with self.assertRaises(ValueError):
            self.create_owned_job(job_id, envelope=wrong_scope_envelope)

    def test_cross_tenant_and_cross_subject_reads_are_denied(self):
        job_id = str(uuid4())
        envelope = self.envelope(job_id)
        self.create_owned_job(job_id, envelope=envelope)

        for other in (
            self.make_principal(tenant="tenant-b"),
            self.make_principal(subject="subject-2"),
        ):
            with self.assertRaises(AuthorizationError):
                self.repository.get_job_secret_envelope(
                    job_id,
                    other,
                    "webhook.callback",
                )

    def test_idempotent_replay_never_replaces_existing_secret(self):
        job_id = str(uuid4())
        key = "idem-secret-replay-{}".format(uuid4())
        original = self.envelope(job_id, value="https://first.invalid/hook")
        self.create_owned_job(job_id, key=key, envelope=original)

        retry_candidate_id = str(uuid4())
        retry_envelope = self.envelope(
            retry_candidate_id, value="https://second.invalid/hook"
        )
        replay_id, created = self.repository.create_job(
            job_id=retry_candidate_id,
            idempotency_key=key,
            request_hash="a" * 64,
            owner_principal=self.principal,
            secret_envelopes=[retry_envelope],
        )

        self.assertEqual((replay_id, created), (job_id, False))
        persisted = self.repository.get_job_secret_envelope(
            job_id,
            self.principal,
            "webhook.callback",
        )
        self.assertEqual(persisted.secret_id, original.secret_id)
        self.assertEqual(
            resolve_secret(persisted, original.context, self.provider),
            "https://first.invalid/hook",
        )

    def test_iam_worker_can_fetch_only_job_scoped_encrypted_envelope(self):
        job_id = str(uuid4())
        original = self.envelope(job_id)
        self.create_owned_job(job_id, envelope=original)
        worker = WorkflowWorkerIdentity(
            account_id="123456789012",
            arn=(
                "arn:aws:sts::123456789012:assumed-role/"
                "video-workflow-worker/worker-1"
            ),
            role_arn_prefix=(
                "arn:aws:sts::123456789012:assumed-role/"
                "video-workflow-worker/"
            ),
        )
        stored = self.repository.get_job_secret_envelope_for_worker(
            job_id, worker, "webhook.callback", original.secret_id
        )
        self.assertEqual(stored.secret_id, original.secret_id)
        self.assertEqual(
            resolve_secret(stored, original.context, self.provider),
            "https://callback.invalid/hook",
        )
        with self.repository._session() as session:
            audit = session.query(WorkflowEventRecord).filter_by(
                job_id=job_id,
                event_type="JOB_SECRET_RESOLVE_AUTHORIZED",
            ).one()
            self.assertEqual(
                audit.payload_json["worker_arn"], worker.arn
            )
            self.assertNotIn("callback.invalid", repr(audit.payload_json))
        with self.assertRaises(AuthorizationError):
            self.repository.get_job_secret_envelope_for_worker(
                job_id, object(), "webhook.callback", original.secret_id
            )
        with self.assertRaises(AuthorizationError):
            self.repository.get_job_secret_envelope_for_worker(
                job_id, worker, "webhook.callback", "not-the-plan-secret"
            )

    def test_concurrent_idempotent_plan_reuses_accepted_secret_reference(self):
        accepted_job_id = str(uuid4())
        candidate_job_id = str(uuid4())
        key = "idem-plan-secret-{}".format(uuid4())
        accepted = self.envelope(
            accepted_job_id, value="https://first.invalid/callback"
        )
        candidate = self.envelope(
            candidate_job_id, value="https://first.invalid/callback"
        )
        accepted_signing = self.envelope(
            accepted_job_id, value="s" * 48, purpose="webhook.signing"
        )
        candidate_signing = self.envelope(
            candidate_job_id, value="s" * 48, purpose="webhook.signing"
        )

        def plan(job_id, secret_id, signing_id):
            return {
                "schema_version": 1,
                "workflow_name": "video.vod",
                "workflow_version": "1",
                "job_id": job_id,
                "nodes": [{
                    "node_id": "callback",
                    "operation": "video.webhook.deliver",
                    "stage_key": "callback",
                    "depends_on": [],
                    "parameters": {
                        "enabled": True,
                        "webhook_secret_ref": secret_id,
                        "webhook_signing_secret_ref": signing_id,
                    },
                    "retry_policy": {
                        "max_attempts": 3,
                        "backoff": "exponential",
                        "initial_delay_seconds": 1,
                        "max_delay_seconds": 30,
                    },
                    "resource_profile": "control",
                }],
            }

        self.repository.create_job(
            job_id=accepted_job_id,
            idempotency_key=key,
            request_hash="f" * 64,
            execution_plan=plan(
                accepted_job_id, accepted.secret_id, accepted_signing.secret_id
            ),
            owner_principal=self.principal,
            secret_envelopes=[accepted, accepted_signing],
        )
        replay_id, created = self.repository.create_job(
            job_id=candidate_job_id,
            idempotency_key=key,
            request_hash="f" * 64,
            execution_plan=plan(
                candidate_job_id, candidate.secret_id, candidate_signing.secret_id
            ),
            owner_principal=self.principal,
            secret_envelopes=[candidate, candidate_signing],
        )
        self.assertEqual((replay_id, created), (accepted_job_id, False))
        stored = self.repository.get_job_secret_envelope(
            accepted_job_id, self.principal, "webhook.callback"
        )
        self.assertEqual(stored.secret_id, accepted.secret_id)
        self.assertEqual(
            self.repository.get_execution_plan(accepted_job_id)["plan"]
            ["nodes"][0]["parameters"]["webhook_secret_ref"],
            accepted.secret_id,
        )
        self.assertEqual(
            self.repository.get_execution_plan(accepted_job_id)["plan"]
            ["nodes"][0]["parameters"]["webhook_signing_secret_ref"],
            accepted_signing.secret_id,
        )

    def test_idempotency_lookup_returns_existing_job_without_secret_rotation(self):
        job_id = str(uuid4())
        key = "idem-lookup-{}".format(uuid4())
        envelope = self.envelope(job_id)
        self.create_owned_job(job_id, key=key, envelope=envelope)
        self.assertEqual(
            self.repository.find_idempotent_job(
                key, "a" * 64, owner_principal=self.principal
            ),
            job_id,
        )
        with self.assertRaises(IdempotencyConflict):
            self.repository.find_idempotent_job(
                key, "b" * 64, owner_principal=self.principal
            )

    def test_job_creation_failure_rolls_back_secret_and_parent_rows(self):
        job_id = str(uuid4())
        envelope = self.envelope(job_id)
        duplicate_stages = [
            {"stage_key": "same", "operation": "video.package"},
            {"stage_key": "same", "operation": "video.package"},
        ]

        with self.assertRaises(IntegrityError):
            self.create_owned_job(
                job_id,
                envelope=envelope,
                stage_specs=duplicate_stages,
            )

        with self.repository._session() as session:
            self.assertIsNone(session.get(JobRecord, job_id))
            self.assertIsNone(session.get(JobSecretRecord, envelope.secret_id))

    def test_expired_secret_record_is_not_returned(self):
        job_id = str(uuid4())
        envelope = self.envelope(job_id)
        self.create_owned_job(job_id, envelope=envelope)
        with self.repository._session.begin() as session:
            secret = session.get(JobSecretRecord, envelope.secret_id)
            secret.expires_at = utcnow() - timedelta(seconds=1)

        with self.assertRaises(AuthorizationError):
            self.repository.get_job_secret_envelope(
                job_id, self.principal, "webhook.callback"
            )

    def test_resolution_requires_the_dedicated_scope(self):
        job_id = str(uuid4())
        envelope = self.envelope(job_id)
        self.create_owned_job(job_id, envelope=envelope)
        read_only = RequestPrincipal(
            issuer=self.principal.issuer,
            subject=self.principal.subject,
            tenant_id=self.principal.tenant_id,
            scopes=frozenset(["video.jobs:read"]),
            roles=self.principal.roles,
            authentication_method=self.principal.authentication_method,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
        with self.assertRaises(AuthorizationError):
            self.repository.get_job_secret_envelope(
                job_id, read_only, "webhook.callback"
            )

    def test_missing_or_expired_secret_is_not_returned(self):
        job_id = str(uuid4())
        envelope = self.envelope(job_id)
        self.create_owned_job(job_id, envelope=envelope)

        with self.assertRaises(ValueError):
            self.repository.get_job_secret_envelope(
                job_id,
                self.principal,
                "webhook.other-purpose",
            )

        expired_principal = RequestPrincipal(
            issuer=self.principal.issuer,
            subject=self.principal.subject,
            tenant_id=self.principal.tenant_id,
            scopes=self.principal.scopes,
            roles=self.principal.roles,
            authentication_method=self.principal.authentication_method,
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        )
        with self.assertRaises(AuthorizationError):
            self.repository.get_job_secret_envelope(
                job_id,
                expired_principal,
                "webhook.callback",
            )


if __name__ == "__main__":
    unittest.main()
