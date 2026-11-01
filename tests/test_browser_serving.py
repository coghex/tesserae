"""Serving the browser page (#84, WEB-2): `serve --browser`, the request rules
and headers, the Origin rule every host applies, and CLI clients unaffected.
docs/designs/browser_contract.md sections 2 to 4."""

import asyncio
import contextlib
import io
import json
import re
import signal
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from tesserae import server
from tesserae.credentials import new_token, write_token_file
from tesserae.referee import runner
from tesserae.referee.store import RefereeStore
from tesserae.server import GameHost, HostConfigError
from tesserae.store import GameStore, StoreError
from tesserae.web import ASSETS, GAME_HOST, LOBBY_HOST, AssetsError, load_assets

PACKAGE = Path(__file__).resolve().parents[1] / "tesserae" / "web"


async def never(job):
    raise AssertionError("no referee job runs in these tests")


def make_game(directory: Path, referee: bool = True) -> tuple[Path, str]:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "game.sqlite3"
    token = new_token()
    with GameStore(path) as store:
        store.add_participant("Alice", token)
        if referee:
            RefereeStore(store).enable()
    return path, token


async def http(port, method, path, headers):
    """One raw HTTP/1.1 request: (status, headers with lowercase names, body)."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    lines = [f"{method} {path} HTTP/1.1", *(f"{k}: {v}" for k, v in headers)]
    writer.write(("\r\n".join(lines) + "\r\n\r\n").encode())
    await writer.drain()
    data = await asyncio.wait_for(reader.read(), 5)
    writer.close()
    head, _, body = data.partition(b"\r\n\r\n")
    status, *fields = head.decode().split("\r\n")
    parsed = {}
    for field in fields:
        name, _, value = field.partition(":")
        parsed[name.strip().lower()] = value.strip()
    return int(status.split()[1]), parsed, body


class HostTests(unittest.IsolatedAsyncioTestCase):
    browser = True

    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path, self.token = make_game(Path(directory.name))
        assets = load_assets() if self.browser else None
        host = GameHost(
            self.path,
            authenticated=True,
            referee_executor=never,
            browser_assets=assets,
        )
        loop = asyncio.get_running_loop()
        self.stop, ready = loop.create_future(), loop.create_future()
        task = asyncio.create_task(host.run(0, self.stop, on_ready=ready.set_result))

        async def shutdown():
            self.stop.done() or self.stop.set_result(None)
            await asyncio.wait_for(task, 10)

        self.addAsyncCleanup(shutdown)
        self.uri = await asyncio.wait_for(ready, 5)
        self.port = int(self.uri.rsplit(":", 1)[1].strip("/"))
        self.host = f"127.0.0.1:{self.port}"

    async def get(self, path, *, method="GET", host=None, extra=()):
        headers = [("Host", host or self.host), *extra]
        return await http(self.port, method, path, headers)

    async def join(self, uri=None, **options):
        async with connect(uri or self.uri, **options) as connection:
            await connection.send(
                json.dumps({"version": 1, "type": "join", "token": self.token})
            )
            return json.loads(await asyncio.wait_for(connection.recv(), 5))


class BrowserModeTests(HostTests):
    async def test_serves_exactly_the_packaged_assets_with_their_headers(self):
        for path, name in ASSETS.items():
            status, headers, body = await self.get(path)
            self.assertEqual(status, 200, path)
            self.assertEqual(body, (PACKAGE / name).read_bytes(), path)
        status, headers, _ = await self.get("/", host=f"localhost:{self.port}")
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "text/html; charset=utf-8")
        self.assertEqual(headers["cache-control"], "no-store")
        self.assertEqual(headers["x-content-type-options"], "nosniff")
        self.assertEqual(headers["referrer-policy"], "no-referrer")
        self.assertEqual(headers["cross-origin-opener-policy"], "same-origin")
        policy = dict(
            part.strip().split(" ", 1)
            for part in headers["content-security-policy"].split(";")
        )
        self.assertEqual(policy["default-src"], "'none'")
        self.assertEqual(policy["script-src"], "'self'")
        self.assertEqual(
            policy["connect-src"],
            f"ws://127.0.0.1:{self.port} ws://localhost:{self.port}",
        )
        self.assertEqual(policy["require-trusted-types-for"], "'script'")
        _, headers, _ = await self.get("/app.js")
        self.assertEqual(headers["content-type"], "text/javascript; charset=utf-8")

    async def test_refuses_other_paths_methods_and_hosts(self):
        port = self.port
        cases = [  # path, method, Host header (None: the host's own), status
            ("/index.html", "GET", None, 404),
            ("/?token=x", "GET", None, 404),
            ("/../pyproject.toml", "GET", None, 404),
            ("/tesserae/web/app.js", "GET", None, 404),
            ("/", "POST", None, 405),
            ("/", "HEAD", None, 405),
            ("/", "GET", "evil.example", 421),  # DNS rebinding
            ("/", "GET", f"evil.example:{port}", 421),
            ("/", "GET", "127.0.0.1", 421),  # no port
            ("/", "GET", f"127.0.0.1:{port + 1}", 421),
        ]
        for path, method, host, expected in cases:
            status, headers, body = await self.get(path, method=method, host=host)
            self.assertEqual(status, expected, (path, method, host))
            self.assertNotIn(b"Tesserae", body)  # nothing of the page or game
        _, headers, _ = await self.get("/", method="POST")
        self.assertEqual(headers["allow"], "GET")
        two_hosts = [("Host", self.host), ("Host", self.host)]
        status, _, _ = await http(self.port, "GET", "/", two_hosts)
        self.assertEqual(status, 421)

    async def test_handshakes_from_the_cli_and_the_page_but_no_other_origin(self):
        self.assertEqual((await self.join())["type"], "snapshot")  # the CLI: no Origin
        localhost = f"ws://localhost:{self.port}/"
        for uri, origin in [
            (self.uri, f"http://127.0.0.1:{self.port}"),
            (localhost, f"http://localhost:{self.port}"),
        ]:
            snapshot = await self.join(uri, origin=origin)
            self.assertEqual(snapshot["type"], "snapshot", origin)
        for origin in [
            "http://evil.example",
            f"http://127.0.0.1:{self.port + 1}",
            f"https://127.0.0.1:{self.port}",
            "null",  # a file:// page
        ]:
            with self.assertRaises(InvalidStatus) as caught:
                await self.join(origin=origin)
            self.assertEqual(caught.exception.response.status_code, 403, origin)
        with self.assertRaises(InvalidStatus) as caught:  # a rebinding page's socket
            await self.join(
                origin="http://evil.example",
                additional_headers={"Host": f"evil.example:{self.port}"},
            )
        self.assertEqual(caught.exception.response.status_code, 421)


class CliOnlyHostTests(HostTests):
    browser = False

    async def test_without_browser_mode_no_page_and_no_browser_origin(self):
        status, _, body = await self.get("/")
        self.assertEqual(status, 426)  # websockets' answer, as before
        self.assertNotIn(b"<html", body)
        self.assertEqual((await self.join())["type"], "snapshot")
        with self.assertRaises(InvalidStatus) as caught:
            await self.join(origin=f"http://127.0.0.1:{self.port}")
        self.assertEqual(caught.exception.response.status_code, 403)


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)

    def test_the_host_refuses_browser_mode_outside_its_scope(self):
        path, _ = make_game(self.directory)
        plain, _ = make_game(self.directory / "plain", referee=False)
        assets = load_assets()
        cases = [
            (path, {"authenticated": False}, "--authenticated"),
            (plain, {"authenticated": True}, "referee mode"),
            (path, {"authenticated": True, "host": "127.0.0.2"}, "this computer"),
            (path, {"authenticated": True, "ssl_context": object()}, "without TLS"),
        ]
        for database, options, message in cases:
            with self.assertRaisesRegex(HostConfigError, message):
                GameHost(
                    database,
                    referee_executor=never,
                    browser_assets=assets,
                    **options,
                )

    def test_serve_refuses_browser_conflicts_before_listening(self):
        path, _ = make_game(self.directory)
        plain, _ = make_game(self.directory / "plain", referee=False)
        cases = [
            ([], path, "requires --authenticated"),
            (["--authenticated", "--host", "192.168.1.5"], path, "this computer"),
            (["--authenticated", "--tls-cert", "c", "--tls-key", "k"], path, "TLS"),
            (["--authenticated"], plain, "referee mode"),
        ]
        for options, database, message in cases:
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code = server.run_foreground(
                    str(database),
                    0,
                    browser=True,
                    authenticated="--authenticated" in options,
                    host=_option(options, "--host", "127.0.0.1"),
                    tls_cert=_option(options, "--tls-cert"),
                    tls_key=_option(options, "--tls-key"),
                    referee_dir=self.directory / "no-referee",
                )
            self.assertEqual(code, 2, options)
            self.assertIn(message, stderr.getvalue(), options)
            self.assertIn("nothing was started", stderr.getvalue())

    def test_browser_mode_never_creates_a_game_and_leaves_no_files_on_refusal(self):
        missing = self.directory / "new" / "game.sqlite3"
        missing.parent.mkdir()
        with self.assertRaisesRegex(StoreError, "No game store"):
            GameHost(
                missing,
                authenticated=True,
                referee_executor=never,
                browser_assets=load_assets(),
            )
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = server.run_foreground(
                str(missing), 0, authenticated=True, browser=True
            )
        self.assertEqual(code, 1)
        self.assertIn("No game store", stderr.getvalue())
        self.assertEqual(list(missing.parent.iterdir()), [])  # nothing created
        plain, _ = make_game(self.directory / "plain", referee=False)
        with contextlib.redirect_stderr(io.StringIO()):
            code = server.run_foreground(
                str(plain), 0, authenticated=True, browser=True
            )
        self.assertEqual(code, 2)
        self.assertEqual(list(plain.parent.iterdir()), [plain])  # no -wal or -shm

    def test_a_missing_packaged_file_is_an_incomplete_installation(self):
        partial = self.directory / "web"
        partial.mkdir()
        for name in ASSETS.values():
            if name != "app.js":
                (partial / name).write_bytes((PACKAGE / name).read_bytes())
        with self.assertRaisesRegex(AssetsError, "app.js .*installation is incomplete"):
            load_assets(partial)
        self.assertEqual(set(load_assets()), set(ASSETS))  # the real package
        (partial / "app.js").write_bytes((PACKAGE / "app.js").read_bytes())
        page = (PACKAGE / "index.html").read_bytes()
        (partial / "index.html").write_bytes(page.replace(GAME_HOST, b""))
        with self.assertRaisesRegex(AssetsError, "does not name its host"):
            load_assets(partial)
        # A lobby host's page differs from a game's only in naming its host.
        game, lobby = load_assets()["/"][1], load_assets(lobby=True)["/"][1]
        self.assertEqual(lobby.replace(LOBBY_HOST, GAME_HOST), game)


def _option(options, name, default=None):
    return options[options.index(name) + 1] if name in options else default


class ServeProcessTests(unittest.TestCase):
    def test_serve_browser_prints_the_page_and_stops_cleanly_with_a_cli_player(self):
        directory = tempfile.TemporaryDirectory(dir="/tmp", prefix="tb")  # short paths
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        path, token = make_game(root)
        folder = runner.setup(root / "r")
        token_file = root / "alice.token"
        write_token_file(token_file, token)
        host = subprocess.Popen(
            [sys.executable, "-m", "tesserae", "serve", "--authenticated", "--browser",
             "--database", str(path), "--port", "0", "--referee-dir", str(folder.root)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )  # fmt: skip
        self.addCleanup(lambda: host.poll() is None and host.kill())
        listening = host.stdout.readline()
        page = host.stdout.readline()
        port = int(re.search(r"127\.0\.0\.1:(\d+)/", listening).group(1))
        self.assertEqual(
            page,
            f"Browser: http://127.0.0.1:{port}/ (this computer only; join with a "
            "personal token)\n",
        )
        client = subprocess.run(
            [sys.executable, "-m", "tesserae", "connect", f"ws://127.0.0.1:{port}/",
             "--token-file", str(token_file), "--json"],
            input="", capture_output=True, text=True, timeout=30,
        )  # fmt: skip
        self.assertEqual(client.returncode, 0, client.stderr)
        self.assertEqual(json.loads(client.stdout.splitlines()[0])["type"], "snapshot")
        self.assertNotIn(token, client.stdout + client.stderr + listening + page)
        host.send_signal(signal.SIGINT)
        self.assertEqual(host.wait(15), 0)
        self.assertIn("Tesserae host stopped.", host.stdout.read())
        host.stdout.close()
        host.stderr.close()
        with self.assertRaises(ConnectionRefusedError):
            socket.create_connection(("127.0.0.1", port), timeout=2).close()
        sockets = [p for p in folder.run.iterdir() if p.is_socket()]
        self.assertEqual(sockets, [])  # lock files stay, recording the end


if __name__ == "__main__":
    unittest.main()
