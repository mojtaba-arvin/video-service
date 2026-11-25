"""Tests for authenticated tenant-scoped creative Project gRPC operations."""

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import grpc

from video_streaming.grpc.protos import streaming_pb2
from video_streaming.grpc.servicers.mixins.creative_projects import (
    CreativeProjectsMixin,
)
from video_streaming.security import AuthorizationError, RequestPrincipal


class AbortCall(Exception):
    def __init__(self, code, details):
        self.code = code
        self.details = details


class FakeContext:
    def abort(self, code, details):
        raise AbortCall(code, details)


class FakeRepository:
    def __init__(self):
        self.calls = []

    def _record(self, name, result, *args, **kwargs):
        self.calls.append((name, args, kwargs))
        return result

    def create_creative_project(self, name, principal, project_id=None):
        return self._record("create_project", {
            "id": project_id or "project-1",
            "tenant_id": principal.tenant_id,
            "name": name,
            "created_at": datetime(2026, 11, 25, tzinfo=timezone.utc),
        }, name, principal, project_id)

    def get_creative_project(self, project_id, principal):
        return self._record("get_project", {
            "id": project_id, "tenant_id": principal.tenant_id,
            "name": "Project", "latest_revision_number": 2,
            "created_at": datetime(2026, 11, 25, tzinfo=timezone.utc),
            "updated_at": datetime(2026, 11, 25, tzinfo=timezone.utc),
        }, project_id, principal)

    def list_creative_projects(self, principal, limit):
        return self._record("list_projects", [{
            "id": "project-1", "tenant_id": principal.tenant_id,
            "name": "Project",
        }], principal, limit=limit)

    def create_creative_project_revision(self, project_id, content, principal):
        return self._record("create_revision", {
            "id": "revision-1", "project_id": project_id,
            "tenant_id": principal.tenant_id, "revision_number": 1,
            "content": content, "content_sha256": "a" * 64,
            "created_at": datetime(2026, 11, 25, tzinfo=timezone.utc),
        }, project_id, content, principal)

    def get_creative_project_revision(self, project_id, revision_number, principal):
        return self._record("get_revision", {
            "id": "revision-1", "project_id": project_id,
            "tenant_id": principal.tenant_id, "revision_number": revision_number,
            "content": {"title": "Project"}, "content_sha256": "a" * 64,
        }, project_id, revision_number, principal)

    def list_creative_project_revisions(self, project_id, principal, limit):
        return self._record("list_revisions", [{
            "id": "revision-1", "project_id": project_id,
            "tenant_id": principal.tenant_id, "revision_number": 1,
            "content": {"title": "Project"}, "content_sha256": "a" * 64,
        }], project_id, principal, limit=limit)

    def create_creative_scene(self, *args, **kwargs):
        return self._record("create_scene", {
            "id": "scene-1", "project_id": args[0],
            "tenant_id": args[2].tenant_id, "name": args[1],
            "sort_order": kwargs["sort_order"],
            "created_at": datetime(2026, 11, 25, tzinfo=timezone.utc),
        }, *args, **kwargs)

    def create_creative_shot(self, *args, **kwargs):
        return self._record("create_shot", {
            "id": "shot-1", "project_id": args[0],
            "tenant_id": args[3].tenant_id, "name": args[2],
            "sort_order": kwargs["sort_order"], "scene_id": args[1],
        }, *args, **kwargs)

    def create_creative_character(self, *args, **kwargs):
        return self._record("create_character", {
            "id": "character-1", "project_id": args[0],
            "tenant_id": args[2].tenant_id, "name": args[1],
            "sort_order": kwargs["sort_order"],
        }, *args, **kwargs)

    def create_creative_asset(self, *args, **kwargs):
        return self._record("create_asset", {
            "id": "asset-1", "project_id": args[0],
            "tenant_id": args[4].tenant_id, "name": args[2],
            "sort_order": kwargs["sort_order"], "artifact_id": args[1],
            "source_job_id": "job-1", "asset_type": args[3],
            "content_sha256": "b" * 64, "media_type": "video/mp4",
            "size_bytes": 100,
        }, *args, **kwargs)

    def update_creative_project_entity(self, *args, **kwargs):
        return self._record("update_entity", {
            "id": args[2], "project_id": args[1],
            "tenant_id": args[3].tenant_id, "name": kwargs.get("name") or "Asset",
            "sort_order": kwargs.get("sort_order") or 0,
        }, *args, **kwargs)

    def list_creative_project_entities(self, project_id, principal):
        return self._record("list_entities", {
            "scenes": [{"id": "scene-1", "project_id": project_id,
                        "tenant_id": principal.tenant_id, "name": "Scene",
                        "sort_order": 0}],
            "shots": [], "characters": [],
            "assets": [{"id": "asset-1", "project_id": project_id,
                        "tenant_id": principal.tenant_id, "name": "Asset",
                        "sort_order": 0, "artifact_id": "artifact-1",
                        "asset_type": "video", "content_sha256": "b" * 64,
                        "media_type": "video/mp4", "size_bytes": 100}],
        }, project_id, principal)


class CreativeProjectsService(CreativeProjectsMixin):
    pb2 = streaming_pb2


class CreativeProjectsRpcTests(unittest.TestCase):
    def setUp(self):
        self.principal = RequestPrincipal(
            issuer="https://issuer.example.test/",
            subject="user-1",
            tenant_id="tenant-a",
            scopes=frozenset([
                "video.projects:create", "video.projects:read",
                "video.projects:write",
            ]),
            roles=frozenset(),
            authentication_method="bearer",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
        self.repository = FakeRepository()
        self.service = CreativeProjectsService()
        self.context = FakeContext()

    def _patches(self, principal=None):
        principal_patch = patch(
            "video_streaming.grpc.servicers.mixins.creative_projects.get_request_principal",
            return_value=principal or self.principal,
        )
        repository_patch = patch(
            "video_streaming.grpc.servicers.mixins.creative_projects.get_workflow_repository",
            return_value=self.repository,
        )
        return principal_patch, repository_patch

    def test_project_create_read_and_list_use_verified_principal(self):
        p1, p2 = self._patches()
        with p1, p2:
            created = self.service.create_creative_project(
                streaming_pb2.CreateCreativeProjectRequest(name="Demo"),
                self.context,
            )
            self.assertEqual(created.tenant_id, "tenant-a")
            self.assertEqual(self.repository.calls[-1][0], "create_project")

            fetched = self.service.get_creative_project(
                streaming_pb2.GetCreativeProjectRequest(project_id="project-1"),
                self.context,
            )
            self.assertEqual(fetched.latest_revision_number, 2)

            listed = self.service.list_creative_projects(
                streaming_pb2.ListCreativeProjectsRequest(limit=20),
                self.context,
            )
            self.assertEqual(len(listed.projects), 1)
            self.assertEqual(self.repository.calls[-1][2]["limit"], 20)

    def test_revision_json_and_history_are_bounded(self):
        p1, p2 = self._patches()
        with p1, p2:
            created = self.service.create_creative_project_revision(
                streaming_pb2.CreateCreativeProjectRevisionRequest(
                    project_id="project-1",
                    content_json='{"title":"Project"}',
                ),
                self.context,
            )
            self.assertEqual(created.revision_number, 1)
            self.assertEqual(created.content_json, '{"title":"Project"}')

            fetched = self.service.get_creative_project_revision(
                streaming_pb2.GetCreativeProjectRevisionRequest(
                    project_id="project-1", revision_number=1
                ),
                self.context,
            )
            self.assertEqual(fetched.content_sha256, "a" * 64)

            listed = self.service.list_creative_project_revisions(
                streaming_pb2.ListCreativeProjectRevisionsRequest(
                    project_id="project-1", limit=10
                ),
                self.context,
            )
            self.assertEqual(len(listed.revisions), 1)
            self.assertEqual(self.repository.calls[-1][2]["limit"], 10)

    def test_invalid_revision_json_is_rejected_before_repository_write(self):
        p1, p2 = self._patches()
        with p1, p2:
            with self.assertRaises(AbortCall) as caught:
                self.service.create_creative_project_revision(
                    streaming_pb2.CreateCreativeProjectRevisionRequest(
                        project_id="project-1", content_json="{not-json"
                    ),
                    self.context,
                )
        self.assertEqual(caught.exception.code, grpc.StatusCode.INVALID_ARGUMENT)
        self.assertFalse(any(call[0] == "create_revision" for call in self.repository.calls))

    def test_typed_entities_use_tenant_scoped_repository_and_allowlisted_fields(self):
        p1, p2 = self._patches()
        with p1, p2:
            asset = self.service.create_creative_asset(
                streaming_pb2.CreateCreativeAssetRequest(
                    project_id="project-1", artifact_id="artifact-1",
                    name="Final", asset_type="video", sort_order=2,
                ),
                self.context,
            )
            self.assertEqual(asset.entity.artifact_id, "artifact-1")
            self.assertEqual(asset.entity.content_sha256, "b" * 64)
            self.assertEqual(self.repository.calls[-1][0], "create_asset")

            updated = self.service.update_creative_project_entity(
                streaming_pb2.UpdateCreativeProjectEntityRequest(
                    project_id="project-1", entity_type="asset",
                    entity_id="asset-1", name="Final cut", sort_order=3,
                    update_name=True, update_sort_order=True,
                ),
                self.context,
            )
            self.assertEqual(updated.entity.name, "Final cut")
            self.assertEqual(self.repository.calls[-1][0], "update_entity")
            self.assertEqual(self.repository.calls[-1][2]["name"], "Final cut")

            entities = self.service.list_creative_project_entities(
                streaming_pb2.ListCreativeProjectEntitiesRequest(
                    project_id="project-1"
                ),
                self.context,
            )
            self.assertEqual(entities.scenes[0].name, "Scene")
            self.assertEqual(entities.assets[0].source_job_id, "")
            self.assertEqual(entities.assets[0].media_type, "video/mp4")

    def test_unprivileged_principal_is_denied_before_repository_access(self):
        unprivileged = RequestPrincipal(
            issuer=self.principal.issuer,
            subject="read-only",
            tenant_id="tenant-a",
            scopes=frozenset(["video.jobs:read"]),
            roles=frozenset(),
            authentication_method="bearer",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
        p1, p2 = self._patches(unprivileged)
        with p1, p2:
            with self.assertRaises(AbortCall) as caught:
                self.service.create_creative_project(
                    streaming_pb2.CreateCreativeProjectRequest(name="Denied"),
                    self.context,
                )
        self.assertEqual(caught.exception.code, grpc.StatusCode.PERMISSION_DENIED)
        self.assertEqual(self.repository.calls, [])

    def test_foreign_project_is_non_enumerating(self):
        self.repository.get_creative_project = lambda *_args: (
            (_ for _ in ()).throw(AuthorizationError("Resource not found or access denied"))
        )
        p1, p2 = self._patches()
        with p1, p2:
            with self.assertRaises(AbortCall) as caught:
                self.service.get_creative_project(
                    streaming_pb2.GetCreativeProjectRequest(project_id="foreign"),
                    self.context,
                )
        self.assertEqual(caught.exception.code, grpc.StatusCode.NOT_FOUND)


if __name__ == "__main__":
    unittest.main()
