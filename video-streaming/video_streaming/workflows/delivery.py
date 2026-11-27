"""Provider-neutral, privacy-minimized CDN delivery event contract.

This module defines normalized domain data only. It has no provider SDK,
database, transport, or raw-log ingestion dependency.
"""

from dataclasses import asdict, dataclass
import ipaddress
from datetime import datetime, timezone
import hashlib
import json
import re


class DeliveryEventContractError(ValueError):
    """A provider event cannot be represented safely by the canonical contract."""


_CACHE_STATUSES = frozenset({
    "HIT", "MISS", "BYPASS", "REVALIDATED", "STALE", "UNKNOWN",
})
_SOURCE_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_REGION_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")


def _required_identifier(value, name, max_length):
    if (
            not isinstance(value, str)
            or not value
            or len(value) > max_length
            or value.strip() != value
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise DeliveryEventContractError("{} is invalid".format(name))
    return value


def _optional_identifier(value, name, max_length):
    if value is None:
        return None
    return _required_identifier(value, name, max_length)


def _optional_non_negative_integer(value, name, maximum):
    if value is None:
        return None
    if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            or value > maximum):
        raise DeliveryEventContractError("{} is invalid".format(name))
    return value


@dataclass(frozen=True)
class DeliveryEvent:
    """One normalized CDN response for one immutable published manifest."""

    source: str
    source_event_id: str
    source_timestamp: datetime
    artifact_id: str
    session_id: str = None
    asset_version: str = None
    delivery_config_id: str = None
    edge_region: str = None
    cache_status: str = "UNKNOWN"
    http_status: int = None
    response_time_ms: int = None
    bytes_sent: int = None
    schema_version: int = 1

    def __post_init__(self):
        source = _required_identifier(self.source, "source", 64)
        if not _SOURCE_PATTERN.fullmatch(source):
            raise DeliveryEventContractError("source is invalid")
        _required_identifier(self.source_event_id, "source_event_id", 128)
        _required_identifier(self.artifact_id, "artifact_id", 36)
        _optional_identifier(self.session_id, "session_id", 36)
        _optional_identifier(self.asset_version, "asset_version", 128)
        _optional_identifier(self.delivery_config_id, "delivery_config_id", 128)
        _optional_identifier(self.edge_region, "edge_region", 64)
        if self.edge_region is not None:
            if not _REGION_PATTERN.fullmatch(self.edge_region):
                raise DeliveryEventContractError("edge_region must be a coarse region label")
            try:
                ipaddress.ip_address(self.edge_region)
            except ValueError:
                pass
            else:
                raise DeliveryEventContractError("edge_region must not contain an IP address")

        timestamp = self.source_timestamp
        if (
                not isinstance(timestamp, datetime)
                or timestamp.tzinfo is None
                or timestamp.utcoffset() is None):
            raise DeliveryEventContractError(
                "source_timestamp must be timezone-aware"
            )
        object.__setattr__(
            self, "source_timestamp", timestamp.astimezone(timezone.utc)
        )

        if (
                not isinstance(self.cache_status, str)
                or self.cache_status not in _CACHE_STATUSES):
            raise DeliveryEventContractError("cache_status is invalid")
        _optional_non_negative_integer(
            self.http_status, "http_status", 599
        )
        if self.http_status is not None and (
                self.http_status < 100 or self.http_status > 599):
            raise DeliveryEventContractError("http_status is invalid")
        _optional_non_negative_integer(
            self.response_time_ms, "response_time_ms", 86400000
        )
        _optional_non_negative_integer(
            self.bytes_sent, "bytes_sent", 2 ** 53 - 1
        )
        if (
                isinstance(self.schema_version, bool)
                or not isinstance(self.schema_version, int)
                or self.schema_version != 1):
            raise DeliveryEventContractError("schema_version is unsupported")

    def canonical_payload(self):
        """Return only allow-listed normalized fields; never raw provider data."""
        payload = asdict(self)
        payload["source_timestamp"] = (
            self.source_timestamp.isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )
        return payload

    def fingerprint(self):
        """Stable digest for idempotency and conflicting source-ID detection."""
        encoded = json.dumps(
            self.canonical_payload(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()
