"""SSRF-resistant webhook transport with DNS-pinned outbound connections.

Only globally routable destinations are accepted by default. Loopback is
available solely through an explicit test/development setting. DNS results are
validated once and the socket connects directly to one of those validated IPs,
while HTTP Host and TLS SNI/certificate verification retain the original host.
"""

from dataclasses import dataclass
import http.client
import ipaddress
import socket
import ssl
from urllib.parse import urlsplit


class WebhookDestinationError(ValueError):
    """The requested callback destination violates outbound network policy."""


class WebhookTransportError(ConnectionError):
    """A validated callback destination could not be reached safely."""


@dataclass(frozen=True)
class WebhookTarget:
    scheme: str
    hostname: str
    port: int
    request_target: str
    host_header: str
    addresses: tuple


def _normalize_hostname(hostname):
    if not isinstance(hostname, str) or not hostname or "%" in hostname:
        raise WebhookDestinationError("Webhook destination is not allowed")
    try:
        address = ipaddress.ip_address(hostname)
        return str(address), address
    except ValueError:
        pass

    try:
        normalized = hostname.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise WebhookDestinationError(
            "Webhook destination is not allowed"
        ) from exc
    if normalized.endswith(".") or len(normalized) > 253:
        raise WebhookDestinationError("Webhook destination is not allowed")
    labels = normalized.split(".")
    if any(
            not label
            or len(label) > 63
            or label.startswith("-")
            or label.endswith("-")
            or not all(
                character.isalnum() or character == "-"
                for character in label
            )
            for label in labels):
        raise WebhookDestinationError("Webhook destination is not allowed")
    return normalized, None


def _is_public_address(address):
    if not address.is_global:
        return False
    if address.version == 6:
        # Reject translation/tunnelling forms that can encode a non-public
        # IPv4 destination while appearing globally routable as IPv6.
        if (
                address.ipv4_mapped is not None
                or address.sixtofour is not None
                or address.teredo is not None
                or address in ipaddress.ip_network("64:ff9b::/96")
                or address in ipaddress.ip_network("64:ff9b:1::/48")):
            return False
    return True


def resolve_webhook_target(url, allow_loopback=False, resolver=None):
    """Parse and resolve a callback URL, rejecting unsafe DNS answers.

    If loopback is enabled, every DNS answer must be loopback. Private,
    link-local, multicast, reserved and mixed public/private answers remain
    rejected. HTTP is permitted only for explicitly enabled loopback targets.
    """
    if (
            not isinstance(url, str)
            or not url
            or any(ord(character) <= 32 or ord(character) == 127 for character in url)):
        raise WebhookDestinationError("Webhook destination is not allowed")
    try:
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        hostname = parts.hostname
        port = parts.port
    except ValueError as exc:
        raise WebhookDestinationError(
            "Webhook destination is not allowed"
        ) from exc

    if (
            scheme not in ("http", "https")
            or not hostname
            or parts.username is not None
            or parts.password is not None
            or parts.fragment):
        raise WebhookDestinationError("Webhook destination is not allowed")

    hostname, literal_address = _normalize_hostname(hostname)
    if port is None:
        port = 443 if scheme == "https" else 80
    if port < 1 or port > 65535:
        raise WebhookDestinationError("Webhook destination is not allowed")

    if literal_address is not None:
        addresses = [literal_address]
    else:
        lookup = resolver or socket.getaddrinfo
        try:
            records = lookup(hostname, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            # DNS infrastructure failures are transient transport failures;
            # malformed or unsafe resolved addresses remain terminal denials.
            raise WebhookTransportError(
                "Webhook destination resolution failed"
            ) from exc
        except ValueError as exc:
            raise WebhookDestinationError(
                "Webhook destination could not be validated"
            ) from exc
        addresses = []
        for record in records or ():
            try:
                raw_address = record[4][0]
                if "%" in raw_address:
                    raise ValueError("Scoped IP addresses are not allowed")
                address = ipaddress.ip_address(raw_address)
            except (IndexError, TypeError, ValueError) as exc:
                raise WebhookDestinationError(
                    "Webhook destination could not be validated"
                ) from exc
            if address not in addresses:
                addresses.append(address)

    if not addresses:
        raise WebhookTransportError("Webhook destination resolution returned no addresses")

    loopback_only = all(address.is_loopback for address in addresses)
    if loopback_only and allow_loopback:
        pass
    elif any(not _is_public_address(address) for address in addresses):
        raise WebhookDestinationError("Webhook destination is not allowed")

    if scheme == "http" and not (allow_loopback and loopback_only):
        raise WebhookDestinationError("HTTPS is required for webhook delivery")

    request_target = parts.path or "/"
    if parts.query:
        request_target += "?" + parts.query

    host_header = (
        "[{}]".format(hostname) if ":" in hostname else hostname
    )
    if port != (443 if scheme == "https" else 80):
        host_header += ":{}".format(port)

    return WebhookTarget(
        scheme=scheme,
        hostname=hostname,
        port=port,
        request_target=request_target,
        host_header=host_header,
        addresses=tuple(str(address) for address in addresses),
    )


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(
            self, host, port, connect_ip, connect_timeout, read_timeout):
        self._connect_ip = connect_ip
        self._read_timeout = read_timeout
        super().__init__(host=host, port=port, timeout=connect_timeout)

    def connect(self):
        self.sock = socket.create_connection(
            (self._connect_ip, self.port),
            self.timeout,
        )
        self.sock.settimeout(self._read_timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(
            self, host, port, connect_ip, connect_timeout, read_timeout,
            ssl_context=None):
        self._connect_ip = connect_ip
        self._read_timeout = read_timeout
        context = ssl_context or ssl.create_default_context()
        super().__init__(
            host=host,
            port=port,
            timeout=connect_timeout,
            context=context,
        )

    def connect(self):
        raw_socket = socket.create_connection(
            (self._connect_ip, self.port),
            self.timeout,
        )
        try:
            raw_socket.settimeout(self.timeout)
            self.sock = self._context.wrap_socket(
                raw_socket,
                server_hostname=self.host,
            )
            self.sock.settimeout(self._read_timeout)
        except Exception:
            raw_socket.close()
            raise


class WebhookResponse:
    """Small response facade that closes both response and pinned connection."""

    def __init__(self, response, connection):
        self._response = response
        self._connection = connection

    @property
    def status(self):
        return self._response.status

    @property
    def reason(self):
        return self._response.reason

    def close(self):
        try:
            self._response.close()
        finally:
            self._connection.close()


def request_webhook(
        url,
        body,
        connect_timeout,
        read_timeout,
        allow_loopback=False):
    """POST one callback without redirects, proxy use, or a second DNS lookup."""
    target = resolve_webhook_target(
        url,
        allow_loopback=allow_loopback,
    )
    headers = {
        "Content-Type": "application/json",
        "Host": target.host_header,
    }
    last_connect_error = None

    for address in target.addresses:
        connection_type = (
            _PinnedHTTPSConnection
            if target.scheme == "https"
            else _PinnedHTTPConnection
        )
        connection = connection_type(
            target.hostname,
            target.port,
            address,
            connect_timeout,
            read_timeout,
        )
        try:
            # Establish the socket before sending any bytes. A connect failure
            # may try another already-validated address; no DNS lookup occurs.
            connection.connect()
        except (OSError, TimeoutError) as exc:
            connection.close()
            last_connect_error = exc
            continue

        try:
            connection.request(
                "POST",
                target.request_target,
                body=body,
                headers=headers,
            )
            return WebhookResponse(connection.getresponse(), connection)
        except Exception as exc:
            connection.close()
            raise WebhookTransportError(
                "Webhook delivery failed after connecting to a validated destination"
            ) from exc

    raise WebhookTransportError(
        "Unable to connect to a validated webhook destination"
    ) from last_connect_error
