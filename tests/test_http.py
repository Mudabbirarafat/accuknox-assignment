"""Tests for the fetcher.

These run against a real HTTP server on a loopback port rather than against
mocks. A mocked `requests.get` would prove that the code calls the function I
wrote, which is not in doubt; what is in doubt is whether the size cap
actually stops a large body and whether a redirect can walk off http. Those
only get answered by something on the other end of a socket.
"""
from __future__ import annotations

import json
import threading
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from src.common import http as fetcher

HITS: Counter = Counter()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # keep the test output readable
        pass

    def _send(self, status: int, body: bytes, content_type="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802  (the stdlib names it)
        path = self.path.split("?")[0]
        HITS[path] += 1

        if path == "/ok.json":
            self._send(200, json.dumps({"ok": True, "docs": [1, 2, 3]}).encode())
        elif path == "/big":
            self._send(200, b"x" * 50_000)
        elif path == "/to-file":
            self.send_response(302)
            self.send_header("Location", "file:///etc/passwd")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif path == "/to-ok":
            self.send_response(302)
            self.send_header("Location", "/ok.json")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif path == "/loop":
            self.send_response(302)
            self.send_header("Location", "/loop")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif path == "/notjson":
            self._send(200, b"hello, this is not json", content_type="text/plain")
        elif path == "/boom":
            self._send(500, b'{"error": "internal"}')
        elif path == "/gone":
            self._send(404, b'{"error": "not found"}')
        else:
            self._send(404, b'{"error": "unknown route"}')


class QuietServer(ThreadingHTTPServer):
    """A server that does not shout when a client hangs up mid-response.

    The size-cap tests stop reading as soon as the body passes the limit,
    which is the behaviour being tested, and from the server's side that is a
    connection reset. Letting the default handler print a traceback for each
    one buries the test output in noise about the tests working.
    """

    def handle_error(self, request, client_address):
        pass


@pytest.fixture(scope="module")
def base_url():
    server = QuietServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    yield f"http://{host}:{port}"
    server.shutdown()
    server.server_close()


@pytest.fixture(autouse=True)
def clear_hits():
    HITS.clear()


def test_a_normal_response_is_parsed(base_url):
    payload = fetcher.get_json(base_url + "/ok.json")
    assert payload == {"ok": True, "docs": [1, 2, 3]}


def test_a_body_over_the_cap_is_refused(base_url):
    # 50 kB of body against a 1 kB cap. The server declares the length, so
    # this is caught before a single chunk is read.
    with pytest.raises(fetcher.FetchError) as error:
        fetcher.get_json(base_url + "/big", max_bytes=1000)
    assert "over the cap" in str(error.value)


def test_the_cap_holds_even_without_a_declared_length(base_url, monkeypatch):
    # Pretending the server sent no Content-Length, so the only thing left
    # standing between us and the whole body is the loop that counts bytes.
    real = fetcher.requests.Session.get

    def stripped(self, *args, **kwargs):
        response = real(self, *args, **kwargs)
        response.headers.pop("Content-Length", None)
        return response

    monkeypatch.setattr(fetcher.requests.Session, "get", stripped)
    with pytest.raises(fetcher.FetchError) as error:
        fetcher.get_json(base_url + "/big", max_bytes=1000)
    assert "exceeded 1000 bytes" in str(error.value)


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/x", "/etc/passwd"])
def test_only_http_and_https_are_fetched(url):
    with pytest.raises(fetcher.FetchError) as error:
        fetcher.get_json(url)
    assert "refusing scheme" in str(error.value)


def test_a_redirect_cannot_change_the_scheme(base_url):
    # The SSRF shape: an https URL that hands back a Location pointing at the
    # local filesystem. requests would follow it; this must not.
    with pytest.raises(fetcher.FetchError) as error:
        fetcher.get_json(base_url + "/to-file")
    assert "refusing scheme" in str(error.value)


def test_an_ordinary_redirect_is_still_followed(base_url):
    payload = fetcher.get_json(base_url + "/to-ok")
    assert payload["ok"] is True


def test_a_redirect_loop_terminates(base_url):
    with pytest.raises(fetcher.FetchError) as error:
        fetcher.get_json(base_url + "/loop")
    assert "too many redirects" in str(error.value)


def test_a_body_that_is_not_json_fails_clearly(base_url):
    with pytest.raises(fetcher.FetchError) as error:
        fetcher.get_json(base_url + "/notjson")
    assert "not JSON" in str(error.value)


def test_a_server_error_is_retried(base_url):
    with pytest.raises(fetcher.FetchError):
        fetcher.get_json(base_url + "/boom", attempts=2)
    assert HITS["/boom"] == 2


def test_a_client_error_is_not_retried(base_url):
    # Sending a request the server has already rejected, three times, only
    # adds load to somebody else's service.
    with pytest.raises(fetcher.FetchError) as error:
        fetcher.get_json(base_url + "/gone", attempts=3)
    assert HITS["/gone"] == 1
    assert "rejected: 404" in str(error.value)
