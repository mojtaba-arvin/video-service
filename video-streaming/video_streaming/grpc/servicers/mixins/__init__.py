"""Lazy exports for independently importable gRPC request mixins."""

from importlib import import_module


_EXPORTS = {
    "GetResultsMixin": ".get_results",
    "CreateJobMixin": ".create_job",
    "RevokeJobsMixin": ".revoke_jobs",
    "RevokeOutputsMixin": ".revoke_outputs",
    "PlaybackQoEMixin": ".playback_qoe",
}

__all__ = list(_EXPORTS)


def __getattr__(name):
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(name)
    value = getattr(import_module(module_name, __name__), name)
    globals()[name] = value
    return value
