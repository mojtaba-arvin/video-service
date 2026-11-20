"""Shared gRPC adapter guard for durable Job and playback resources."""
from video_streaming.grpc.authentication import get_request_principal
from video_streaming.grpc import exceptions
from video_streaming.security import AuthorizationError
from video_streaming.workflows.service import get_workflow_repository


def require_job_access(job_id, required_scope, context):
    """Authorize a durable Job before touching Redis or any side effect."""
    repository = get_workflow_repository(required=True)
    try:
        repository.authorize_job_access(
            job_id,
            get_request_principal(),
            required_scope,
        )
    except AuthorizationError:
        # Same public response for missing, legacy-unowned and foreign Jobs.
        raise exceptions.ResourceNotFoundOrDeniedException(context=context)
    return repository


def require_playback_session_access(session_id, required_scope, context):
    """Authorize session ownership as well as the session capability token."""
    repository = get_workflow_repository(required=True)
    try:
        repository.authorize_playback_session_access(
            session_id,
            get_request_principal(),
            required_scope,
        )
    except AuthorizationError:
        raise exceptions.ResourceNotFoundOrDeniedException(context=context)
    return repository
