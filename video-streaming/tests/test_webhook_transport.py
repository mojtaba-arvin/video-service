"""Security tests for webhook destination validation and DNS pinning."""

import socket
import unittest
from unittest.mock import Mock, patch

from video_streaming.ffmpeg.webhook_transport import (
    WebhookDestinationError,
    WebhookTarget,
    WebhookTransportError,
    _PinnedHTTPSConnection,
    request_webhook,
    resolve_webhook_target,
)


def _records(*addresses):
    records = []
    for value in addresses:
        family = socket.AF_INET6 if ":" in value else socket.AF_INET
        sockaddr = (value, 443, 0, 0) if family == socket.AF_INET6 else (value, 443)
        records.append((family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr))
    return records


class WebhookDestinationTests(unittest.TestCase):
    def resolver(self, *addresses):
        return lambda hostname, port, type: _records(*addresses)

    def test_public_dns_answers_are_returned_for_pinned_connections(self):
        target = resolve_webhook_target(
            "https://callback.example.test:8443/hooks/done?source=vod",
            resolver=self.resolver("93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"),
        )

        self.assertEqual(target.scheme, "https")
        self.assertEqual(target.hostname, "callback.example.test")
        self.assertEqual(target.port, 8443)
        self.assertEqual(target.request_target, "/hooks/done?source=vod")
        self.assertEqual(target.host_header, "callback.example.test:8443")
        self.assertEqual(
            target.addresses,
            ("93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"),
        )

    def test_any_non_global_dns_answer_rejects_the_entire_destination(self):
        for unsafe in (
            "127.0.0.1",
            "10.0.0.8",
            "169.254.169.254",
            "192.168.1.10",
            "192.0.2.10",
            "::1",
            "fe80::1",
            "fc00::1",
            "::ffff:7f00:1",
            "64:ff9b::a00:1",
            "2002:7f00:1::",
        ):
            with self.subTest(address=unsafe):
                with self.assertRaises(WebhookDestinationError):
                    resolve_webhook_target(
                        "https://callback.example.test/hook",
                        resolver=self.resolver("93.184.216.34", unsafe),
                    )

    def test_loopback_requires_explicit_test_setting_and_http_is_loopback_only(self):
        resolver = self.resolver("127.0.0.1")
        with self.assertRaises(WebhookDestinationError):
            resolve_webhook_target(
                "http://127.0.0.1:8080/callback",
                resolver=resolver,
            )

        target = resolve_webhook_target(
            "http://127.0.0.1:8080/callback",
            allow_loopback=True,
            resolver=resolver,
        )
        self.assertEqual(target.addresses, ("127.0.0.1",))
        self.assertEqual(target.host_header, "127.0.0.1:8080")

    def test_loopback_setting_does_not_allow_private_or_metadata_addresses(self):
        for unsafe in ("10.0.0.2", "169.254.169.254", "192.168.1.2", "fc00::1"):
            with self.subTest(address=unsafe):
                with self.assertRaises(WebhookDestinationError):
                    resolve_webhook_target(
                        "https://callback.example.test/hook",
                        allow_loopback=True,
                        resolver=self.resolver(unsafe),
                    )

    def test_public_http_is_rejected_even_when_loopback_is_enabled(self):
        with self.assertRaises(WebhookDestinationError):
            resolve_webhook_target(
                "http://callback.example.test/hook",
                allow_loopback=True,
                resolver=self.resolver("93.184.216.34"),
            )

    def test_invalid_url_shapes_are_rejected(self):
        invalid_urls = (
            "ftp://callback.example.test/hook",
            "https://user:password@callback.example.test/hook",
            "https://callback.example.test/hook#fragment",
            "https:///missing-host",
            "https://callback.example.test:99999/hook",
            "https://callback.example.test:0/hook",
            "https://callback.example.test/hook with spaces",
        )
        for url in invalid_urls:
            with self.subTest(url=url):
                with self.assertRaises(WebhookDestinationError):
                    resolve_webhook_target(
                        url,
                        resolver=self.resolver("93.184.216.34"),
                    )

    def test_dns_failure_fails_closed(self):
        def fail_lookup(hostname, port, type):
            raise socket.gaierror("lookup failed")

        with self.assertRaises(WebhookTransportError):
            resolve_webhook_target(
                "https://callback.example.test/hook",
                resolver=fail_lookup,
            )

    def test_ip_literal_does_not_use_dns_resolver(self):
        with patch(
                "video_streaming.ffmpeg.webhook_transport.socket.getaddrinfo") as lookup:
            target = resolve_webhook_target("https://93.184.216.34/hook")
        lookup.assert_not_called()
        self.assertEqual(target.addresses, ("93.184.216.34",))

    def test_https_connection_pins_socket_ip_but_keeps_original_tls_sni(self):
        raw_socket = Mock()
        tls_socket = Mock()
        context = Mock()
        context.wrap_socket.return_value = tls_socket

        with patch(
                "video_streaming.ffmpeg.webhook_transport.socket.create_connection",
                return_value=raw_socket) as connect:
            connection = _PinnedHTTPSConnection(
                "callback.example.test",
                443,
                "93.184.216.34",
                2.0,
                5.0,
                ssl_context=context,
            )
            connection.connect()

        connect.assert_called_once_with(("93.184.216.34", 443), 2.0)
        context.wrap_socket.assert_called_once_with(
            raw_socket,
            server_hostname="callback.example.test",
        )
        tls_socket.settimeout.assert_called_once_with(5.0)
        connection.close()

    def test_http_response_is_returned_without_following_redirects(self):
        target = WebhookTarget(
            scheme="https",
            hostname="callback.example.test",
            port=443,
            request_target="/callback?token=opaque",
            host_header="callback.example.test",
            addresses=("93.184.216.34",),
        )
        response = Mock()
        response.status = 302
        response.reason = "Found"
        connection = Mock()
        connection.getresponse.return_value = response

        with patch(
                "video_streaming.ffmpeg.webhook_transport.resolve_webhook_target",
                return_value=target):
            with patch(
                    "video_streaming.ffmpeg.webhook_transport._PinnedHTTPSConnection",
                    return_value=connection):
                result = request_webhook(
                    "https://callback.example.test/callback?token=opaque",
                    body="{}",
                    connect_timeout=2.0,
                    read_timeout=4.0,
                )

        self.assertEqual(result.status, 302)
        connection.connect.assert_called_once_with()
        connection.request.assert_called_once_with(
            "POST",
            "/callback?token=opaque",
            body="{}",
            headers={
                "Content-Type": "application/json",
                "Host": "callback.example.test",
            },
        )
        result.close()
        response.close.assert_called_once_with()
        connection.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
