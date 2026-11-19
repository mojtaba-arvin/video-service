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
