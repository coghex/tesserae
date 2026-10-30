import os
import socket
import ssl
import stat
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from tesserae.transport import (
    TransportConfigError,
    check_client,
    check_server,
    client_context,
    server_context,
)

SETUP_HINT = (
    "TLS tests need an openssl executable on PATH: OpenSSL 3.x or LibreSSL 3.3.6 "
    "(macOS /usr/bin/openssl). See docs/tls.md."
)


def openssl_tools(path=None) -> list[str]:
    """Every distinct, compatible openssl on PATH, first one first."""
    found, seen = [], set()
    for directory in (path if path is not None else os.environ.get("PATH", "")).split(
        os.pathsep
    ):
        candidate = os.path.join(directory, "openssl")
        if not os.access(candidate, os.X_OK) or os.path.realpath(candidate) in seen:
            continue
        seen.add(os.path.realpath(candidate))
        version = subprocess.run(
            [candidate, "version"], capture_output=True, text=True, timeout=30
        ).stdout
        if version.startswith(("OpenSSL 3", "LibreSSL 3")):
            found.append(candidate)
    if not found:
        raise RuntimeError(SETUP_HINT)
    return found


def make_certificate(openssl, directory: Path, san: str, name="host"):
    """The docs/tls.md recipe, run with the given openssl executable."""
    certificate, key = directory / f"{name}.crt", directory / f"{name}.key"
    subprocess.run(
        [openssl, "req", "-x509", "-newkey", "ec",
         "-pkeyopt", "ec_paramgen_curve:prime256v1",
         "-pkeyopt", "ec_param_enc:named_curve", "-nodes", "-days", "365",
         "-subj", "/CN=Tesserae host", "-addext", f"subjectAltName={san}",
         "-keyout", str(key), "-out", str(certificate)],
        check=True, capture_output=True, timeout=60,
        preexec_fn=lambda: os.umask(0o077),
    )  # fmt: skip
    return certificate, key


def handshake(server: ssl.SSLContext, client: ssl.SSLContext, server_hostname: str):
    """One real TLS handshake over loopback. Raises the client's SSL error."""
    with socket.create_server(("127.0.0.1", 0)) as listener:
        listener.settimeout(10)

        def accept():
            try:
                connection, _ = listener.accept()
                connection.settimeout(10)
                with server.wrap_socket(connection, server_side=True):
                    pass
            except (OSError, ssl.SSLError):
                pass

        thread = threading.Thread(target=accept)
        thread.start()
        try:
            with socket.create_connection(listener.getsockname(), timeout=10) as raw:
                with client.wrap_socket(raw, server_hostname=server_hostname) as tls:
                    return tls.version()
        finally:
            thread.join(10)


class ServerConfigurationTests(unittest.TestCase):
    def test_allowed_configurations(self):
        cases = [
            ({}, ("127.0.0.1", False, "ws")),
            ({"authenticated": True}, ("127.0.0.1", True, "ws")),
            ({"host": "127.0.0.2"}, ("127.0.0.2", False, "ws")),
            ({"authenticated": True, "tls_cert": "c", "tls_key": "k"}, ("127.0.0.1", True, "wss")),
            ({"host": "192.168.1.50", "authenticated": True, "tls_cert": "c",
              "tls_key": "k"}, ("192.168.1.50", True, "wss")),
        ]  # fmt: skip
        for options, (host, authenticated, scheme) in cases:
            with self.subTest(options):
                transport = check_server(**options)
                self.assertEqual(
                    (transport.host, transport.authenticated, transport.scheme),
                    (host, authenticated, scheme),
                )

    def test_unsafe_or_incomplete_configurations_are_refused(self):
        lan = "192.168.1.50"
        cases = [
            ("TLS without authentication", {"tls_cert": "c", "tls_key": "k"}),
            ("certificate without key", {"authenticated": True, "tls_cert": "c"}),
            ("key without certificate", {"authenticated": True, "tls_key": "k"}),
            ("plaintext LAN", {"host": lan, "authenticated": True}),
            ("unauthenticated LAN", {"host": lan}),
            ("unauthenticated TLS LAN", {"host": lan, "tls_cert": "c", "tls_key": "k"}),
        ]
        secure = {"authenticated": True, "tls_cert": "c", "tls_key": "k"}
        for description, host in [
            ("every interface", "0.0.0.0"),
            ("multicast", "224.0.0.1"),
            ("limited broadcast", "255.255.255.255"),
            ("hostname", "localhost"),
            ("IPv6", "::1"),
            ("not an address", "192.168.1.300"),
            ("empty", ""),
        ]:  # refused even with every security option on
            cases.append((description, {"host": host, **secure}))
        for description, options in cases:
            with self.subTest(description):
                with self.assertRaises(TransportConfigError):
                    check_server(**options)


class ClientPolicyTests(unittest.TestCase):
    def test_plaintext_only_to_this_computer_and_no_name_lookups(self):
        lookups = AssertionError("must not resolve hostnames")
        with (
            patch("socket.getaddrinfo", side_effect=lookups),
            patch("socket.gethostbyname", side_effect=lookups),
        ):
            for uri in ("ws://127.0.0.1:8790/", "ws://127.9.0.1/", "ws://[::1]:8790/",
                        "ws://localhost:8790/"):  # fmt: skip
                with self.subTest(uri):
                    self.assertIsNone(check_client(uri))
            for uri in ("ws://192.168.1.50:8790/", "ws://gamehost.local/",
                        "ws://localhost.example/", "ws://LOCALHOST.:8790/"):  # fmt: skip
                with self.subTest(uri):
                    with self.assertRaisesRegex(TransportConfigError, "wss://"):
                        check_client(uri)

    def test_uri_problems_are_reported_without_echoing_it(self):
        cases = [
            ("ws://127.0.0.1:8790/", "trust.crt", "needs a wss"),
            ("wss://192.168.1.50:8790/?token=hunter2", None, "must not carry"),
            ("wss://hunter2@192.168.1.50:8790/", None, "must not carry"),
            ("https://192.168.1.50/hunter2", None, "must look like"),
            ("wss://192.168.1.50:99999/hunter2", None, "must look like"),
        ]
        for uri, ca_file, message in cases:
            with self.subTest(uri):
                with self.assertRaisesRegex(TransportConfigError, message) as caught:
                    check_client(uri, ca_file)
                self.assertNotIn("hunter2", str(caught.exception))

    def test_both_trust_paths_check_strictly_on_every_python(self):
        defaults = ssl.create_default_context()
        for context in (check_client("wss://192.168.1.50/"), client_context()):
            self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
            self.assertTrue(context.check_hostname)
            self.assertTrue(context.verify_flags & ssl.VERIFY_X509_STRICT)
            # Strictness is added; every other default flag is kept.
            self.assertEqual(
                context.verify_flags, defaults.verify_flags | ssl.VERIFY_X509_STRICT
            )

    def test_strictness_does_not_depend_on_the_python_default(self):
        real = ssl.create_default_context

        def like_python_3_11(*args, **kwargs):  # strict was not a default yet
            context = real(*args, **kwargs)
            context.verify_flags &= ~ssl.VERIFY_X509_STRICT
            return context

        with patch("ssl.create_default_context", like_python_3_11):
            for context in (check_client("wss://192.168.1.50/"), client_context()):
                self.assertTrue(context.verify_flags & ssl.VERIFY_X509_STRICT)


class CertificateRecipeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.tools = openssl_tools()

    def test_recipe_gives_a_strictly_verified_handshake_with_each_tool(self):
        for openssl in self.tools:
            with self.subTest(openssl):
                folder = self.directory / Path(openssl).parent.name
                folder.mkdir(exist_ok=True)
                certificate, key = make_certificate(
                    openssl, folder, "IP:127.0.0.1,DNS:localhost"
                )
                self.assertEqual(stat.S_IMODE(key.stat().st_mode), 0o600)
                text = subprocess.run(
                    [openssl, "x509", "-in", str(certificate), "-noout", "-text"],
                    check=True, capture_output=True, text=True, timeout=30,
                ).stdout  # fmt: skip
                self.assertIn("prime256v1", text)  # a named curve, not explicit
                self.assertNotIn("Field Type", text)
                server = server_context(certificate, key)
                trusted = client_context(certificate)
                for name in ("127.0.0.1", "localhost"):
                    self.assertIn("TLS", handshake(server, trusted, name))

                other, _ = make_certificate(openssl, folder, "IP:127.0.0.1", "other")
                failures = {
                    "untrusted certificate": (client_context(other), "127.0.0.1"),
                    "system trust only": (client_context(), "127.0.0.1"),
                    "name not in certificate": (trusted, "tesserae.invalid"),
                }
                for description, (client, name) in failures.items():
                    with self.subTest(description):
                        with self.assertRaises(ssl.SSLCertVerificationError):
                            handshake(server, client, name)

    def test_unusable_files_fail_clearly_and_never_prompt(self):
        openssl = self.tools[0]
        certificate, key = make_certificate(openssl, self.directory, "IP:127.0.0.1")
        _, other_key = make_certificate(openssl, self.directory, "IP:127.0.0.1", "b")
        encrypted = self.directory / "locked.key"
        subprocess.run(
            [openssl, "genpkey", "-algorithm", "EC", "-pkeyopt",
             "ec_paramgen_curve:prime256v1", "-aes256", "-pass", "pass:hunter2",
             "-out", str(encrypted)],
            check=True, capture_output=True, timeout=60, stdin=subprocess.DEVNULL,
        )  # fmt: skip
        missing = self.directory / "missing.pem"
        cases = [
            (lambda: server_context(missing, key), "Cannot read the certificate"),
            (
                lambda: server_context(certificate, missing),
                "Cannot read the private key",
            ),
            (lambda: server_context(certificate, other_key), "does not belong"),
            (lambda: server_context(key, key), "not a PEM certificate"),
            (
                lambda: server_context(certificate, encrypted),
                "private key is encrypted",
            ),
            (lambda: client_context(missing), "Cannot read the trust file"),
            (lambda: client_context(self.directory), "Cannot read the trust file"),
            (lambda: client_context(key), "does not contain a PEM certificate"),
        ]
        for build, message in cases:
            with self.subTest(message):
                with self.assertRaisesRegex(TransportConfigError, message) as caught:
                    build()
                self.assertNotIn("PRIVATE KEY", str(caught.exception))
                self.assertNotIn("hunter2", str(caught.exception))

    def test_missing_openssl_is_a_clear_setup_error(self):
        with self.assertRaisesRegex(RuntimeError, "need an openssl executable on PATH"):
            openssl_tools(path=str(self.directory))
