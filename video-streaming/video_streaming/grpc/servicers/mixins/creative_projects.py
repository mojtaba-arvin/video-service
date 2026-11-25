"""Authenticated tenant-scoped gRPC operations for creative Projects."""

import json

import grpc

from video_streaming.grpc.authentication import get_request_principal
from video_streaming.security import AuthorizationError, require_scope
from video_streaming.workflows.service import get_workflow_repository


class CreativeProjectsMixin:
    """Expose the canonical Project/Revision repository without duplicating policy."""

    @staticmethod
    def _principal(context, scope):
        principal = get_request_principal()
        try:
            require_scope(principal, scope)
        except AuthorizationError:
            context.abort(
                grpc.StatusCode.PERMISSION_DENIED,
                "Creative Project permission is required",
            )
        return principal

    @staticmethod
    def _not_found(context):
        context.abort(grpc.StatusCode.NOT_FOUND, "Resource not found")

    @staticmethod
    def _invalid(context, exc):
        context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))

    @staticmethod
    def _iso(value):
        return value.isoformat() if value is not None else ""

    def _project_response(self, item):
        return self.pb2.CreativeProjectResponse(
            id=item["id"],
            tenant_id=item["tenant_id"],
            name=item["name"],
            created_at=self._iso(item.get("created_at")),
            updated_at=self._iso(item.get("updated_at")),
            latest_revision_number=item.get("latest_revision_number", 0),
        )

    def _revision_response(self, item):
        return self.pb2.CreativeProjectRevisionResponse(
            id=item["id"],
            project_id=item["project_id"],
            tenant_id=item["tenant_id"],
            revision_number=item["revision_number"],
            content_json=json.dumps(
                item["content"], sort_keys=True, separators=(",", ":")
            ),
            content_sha256=item["content_sha256"],
            created_at=self._iso(item.get("created_at")),
        )

    def _entity_response(self, item):
        return self.pb2.CreativeProjectEntity(
            id=item["id"],
            project_id=item["project_id"],
            tenant_id=item["tenant_id"],
            name=item["name"],
            sort_order=item["sort_order"],
            scene_id=item.get("scene_id", ""),
            artifact_id=item.get("artifact_id", ""),
            source_job_id=item.get("source_job_id", ""),
            asset_type=item.get("asset_type", ""),
            content_sha256=item.get("content_sha256", ""),
            media_type=item.get("media_type", ""),
            size_bytes=item.get("size_bytes", 0),
            created_at=self._iso(item.get("created_at")),
            updated_at=self._iso(item.get("updated_at")),
        )

    def create_creative_project(self, request, context):
        principal = self._principal(context, "video.projects:create")
        repository = get_workflow_repository(required=True)
        try:
            result = repository.create_creative_project(
                request.name, principal, project_id=request.project_id or None
            )
        except AuthorizationError:
            self._not_found(context)
        except ValueError as exc:
            self._invalid(context, exc)
        return self._project_response(result)

    def get_creative_project(self, request, context):
        principal = self._principal(context, "video.projects:read")
        repository = get_workflow_repository(required=True)
        try:
            result = repository.get_creative_project(request.project_id, principal)
        except AuthorizationError:
            self._not_found(context)
        except ValueError as exc:
            self._invalid(context, exc)
        return self._project_response(result)

    def list_creative_projects(self, request, context):
        principal = self._principal(context, "video.projects:read")
        repository = get_workflow_repository(required=True)
        try:
            rows = repository.list_creative_projects(
                principal, limit=request.limit or 100
            )
        except ValueError as exc:
            self._invalid(context, exc)
        return self.pb2.ListCreativeProjectsResponse(
            projects=[self._project_response(item) for item in rows]
        )

    def create_creative_project_revision(self, request, context):
        principal = self._principal(context, "video.projects:write")
        repository = get_workflow_repository(required=True)
        try:
            content = json.loads(request.content_json)
            result = repository.create_creative_project_revision(
                request.project_id, content, principal
            )
        except AuthorizationError:
            self._not_found(context)
        except (TypeError, json.JSONDecodeError, ValueError) as exc:
            self._invalid(context, exc)
        return self._revision_response(result)

    def get_creative_project_revision(self, request, context):
        principal = self._principal(context, "video.projects:read")
        repository = get_workflow_repository(required=True)
        try:
            result = repository.get_creative_project_revision(
                request.project_id, request.revision_number, principal
            )
        except AuthorizationError:
            self._not_found(context)
        except ValueError as exc:
            self._invalid(context, exc)
        return self._revision_response(result)

    def list_creative_project_revisions(self, request, context):
        principal = self._principal(context, "video.projects:read")
        repository = get_workflow_repository(required=True)
        try:
            rows = repository.list_creative_project_revisions(
                request.project_id, principal, limit=request.limit or 100
            )
        except AuthorizationError:
            self._not_found(context)
        except ValueError as exc:
            self._invalid(context, exc)
        return self.pb2.ListCreativeProjectRevisionsResponse(
            revisions=[self._revision_response(item) for item in rows]
        )

    def create_creative_scene(self, request, context):
        principal = self._principal(context, "video.projects:write")
        repository = get_workflow_repository(required=True)
        try:
            item = repository.create_creative_scene(
                request.project_id, request.name, principal,
                sort_order=request.sort_order, scene_id=request.scene_id or None,
            )
        except AuthorizationError:
            self._not_found(context)
        except ValueError as exc:
            self._invalid(context, exc)
        return self.pb2.CreativeProjectEntityResponse(
            entity=self._entity_response(item)
        )

    def create_creative_shot(self, request, context):
        principal = self._principal(context, "video.projects:write")
        repository = get_workflow_repository(required=True)
        try:
            item = repository.create_creative_shot(
                request.project_id, request.scene_id, request.name, principal,
                sort_order=request.sort_order, shot_id=request.shot_id or None,
            )
        except AuthorizationError:
            self._not_found(context)
        except ValueError as exc:
            self._invalid(context, exc)
        return self.pb2.CreativeProjectEntityResponse(
            entity=self._entity_response(item)
        )

    def create_creative_character(self, request, context):
        principal = self._principal(context, "video.projects:write")
        repository = get_workflow_repository(required=True)
        try:
            item = repository.create_creative_character(
                request.project_id, request.name, principal,
                sort_order=request.sort_order,
                character_id=request.character_id or None,
            )
        except AuthorizationError:
            self._not_found(context)
        except ValueError as exc:
            self._invalid(context, exc)
        return self.pb2.CreativeProjectEntityResponse(
            entity=self._entity_response(item)
        )

    def create_creative_asset(self, request, context):
        principal = self._principal(context, "video.projects:write")
        repository = get_workflow_repository(required=True)
        try:
            item = repository.create_creative_asset(
                request.project_id, request.artifact_id, request.name,
                request.asset_type, principal, sort_order=request.sort_order,
                asset_id=request.asset_id or None,
            )
        except AuthorizationError:
            self._not_found(context)
        except ValueError as exc:
            self._invalid(context, exc)
        return self.pb2.CreativeProjectEntityResponse(
            entity=self._entity_response(item)
        )

    def update_creative_project_entity(self, request, context):
        principal = self._principal(context, "video.projects:write")
        repository = get_workflow_repository(required=True)
        try:
            item = repository.update_creative_project_entity(
                request.entity_type,
                request.project_id,
                request.entity_id,
                principal,
                name=request.name if request.update_name else None,
                sort_order=request.sort_order if request.update_sort_order else None,
            )
        except AuthorizationError:
            self._not_found(context)
        except ValueError as exc:
            self._invalid(context, exc)
        return self.pb2.CreativeProjectEntityResponse(
            entity=self._entity_response(item)
        )

    def list_creative_project_entities(self, request, context):
        principal = self._principal(context, "video.projects:read")
        repository = get_workflow_repository(required=True)
        try:
            result = repository.list_creative_project_entities(
                request.project_id, principal
            )
        except AuthorizationError:
            self._not_found(context)
        except ValueError as exc:
            self._invalid(context, exc)
        return self.pb2.ListCreativeProjectEntitiesResponse(
            scenes=[self._entity_response(item) for item in result["scenes"]],
            shots=[self._entity_response(item) for item in result["shots"]],
            characters=[self._entity_response(item) for item in result["characters"]],
            assets=[self._entity_response(item) for item in result["assets"]],
        )
