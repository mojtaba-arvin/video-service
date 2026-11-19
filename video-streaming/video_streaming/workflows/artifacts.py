"""Pure helpers for immutable, attempt-scoped artifact identities."""

import re


_ATTEMPT_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def attempt_scoped_object_key(public_key, attempt_id):
    """Return a private immutable object key beneath one Attempt namespace.

    The public key is used only as a logical destination/name. Uploading to the
    attempt namespace prevents an expired publisher from overwriting another
    Attempt's object. PostgreSQL Artifact publication remains the authority
    that makes one immutable object discoverable.
    """
    if (
            not isinstance(public_key, str)
            or not public_key
            or public_key.startswith("/")
            or "\\" in public_key
            or "\x00" in public_key):
        raise ValueError("public_key must be a safe relative object key")
    parts = public_key.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError("public_key must not contain empty or traversal segments")
    if not isinstance(attempt_id, str) or not _ATTEMPT_ID.fullmatch(attempt_id):
        raise ValueError("attempt_id must be a bounded opaque identifier")

    filename = parts[-1]
    prefix = "/".join(parts[:-1])
    attempt_prefix = (
        "{}/.attempts/{}".format(prefix, attempt_id)
        if prefix else ".attempts/{}".format(attempt_id)
    )
    return "{}/{}".format(attempt_prefix, filename)


def attempt_scoped_local_output_path(base_path, attempt_id):
    """Return a unique local output path for one processing Attempt."""
    import os

    if not isinstance(base_path, str) or not base_path:
        raise ValueError("base_path must be a non-empty local path")
    if not isinstance(attempt_id, str) or not _ATTEMPT_ID.fullmatch(attempt_id):
        raise ValueError("attempt_id must be a bounded opaque identifier")

    absolute_path = os.path.abspath(base_path)
    filename = os.path.basename(absolute_path)
    if filename in ("", ".", "..") or "\\" in filename or "\x00" in filename:
        raise ValueError("base_path must identify a safe output filename")

    attempt_directory = os.path.join(
        os.path.dirname(absolute_path),
        ".attempts",
        attempt_id,
    )
    return os.path.join(attempt_directory, filename), attempt_directory
