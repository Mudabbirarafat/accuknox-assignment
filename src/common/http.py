"""The one place this project talks to the network.

Two of the three tasks fetch from a REST API. `requests.get(url).json()` would
satisfy both of them and is what the one-line version of this project would
say. It is also the line that turns a data-fetching script into a liability,
for reasons that have nothing to do with the happy path:

* **No timeout means no timeout.** `requests` has none by default. A server
  that accepts the connection and then says nothing will hang the process
  until somebody notices, and a cron job that hangs instead of failing is
  worse than one that fails, because nothing alerts on it.
* **A response is as big as the server decides.** `.json()` reads the whole
  body into memory first. A hostile or simply broken endpoint returning a
  multi-gigabyte body is a one-line denial of service against whatever is
  importing the data.
* **A redirect can change the scheme.** An `https://` URL that redirects to
  `file:///etc/passwd` or to `http://169.254.169.254/` (the cloud instance
  metadata endpoint) is the classic shape of SSRF. `requests` follows
  redirects by default and will happily change scheme while doing it.

So the fetcher caps the body, demands a deadline, allows only http and https
at every hop, and retries only the failures that are worth retrying.

What is deliberately *not* here: no authentication, because none of these
APIs need a key. There is no credential in this repository and no
environment variable standing in for one.
"""
from __future__ import annotations

import json
import time
from typing import Any
from urllib.parse import urlparse

import requests

#: Enough for thousands of records, small enough that a runaway response
#: fails fast rather than filling memory.
MAX_BYTES = 4 * 1024 * 1024

#: Connect and read deadlines. Separate, because a slow DNS lookup and a
#: server that stalls mid-body are different problems.
TIMEOUT = (5.0, 15.0)

ALLOWED_SCHEMES = frozenset({"http", "https"})

USER_AGENT = "accuknox-assignment/1.0 (+https://github.com/Mudabbirarafat)"


class FetchError(RuntimeError):
    """A fetch that failed in a way the caller has to deal with."""


def _check_scheme(url: str) -> None:
    scheme = (urlparse(url).scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        raise FetchError(f"refusing scheme {scheme!r}: only http and https are allowed")


def get_json(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    timeout: tuple[float, float] = TIMEOUT,
    max_bytes: int = MAX_BYTES,
    attempts: int = 3,
) -> Any:
    """GET `url` and parse it as JSON, under a size cap and a deadline.

    Retries on timeouts, connection failures and 5xx, with a widening pause.
    Does not retry a 4xx: the request itself is wrong and sending it again
    only adds load to somebody else's server.
    """
    _check_scheme(url)
    last: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            with requests.Session() as session:
                # Redirects are followed by hand so each hop's scheme can be
                # checked. `allow_redirects=True` would check only the first.
                current = url
                for _ in range(5):
                    _check_scheme(current)
                    response = session.get(
                        current,
                        params=params if current == url else None,
                        timeout=timeout,
                        stream=True,
                        allow_redirects=False,
                        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                    )
                    if response.is_redirect or response.is_permanent_redirect:
                        location = response.headers.get("Location")
                        response.close()
                        if not location:
                            raise FetchError("redirect without a Location header")
                        current = requests.compat.urljoin(current, location)
                        continue
                    break
                else:
                    raise FetchError("too many redirects")

                with response:
                    if response.status_code >= 500:
                        raise FetchError(f"server error {response.status_code}")
                    if response.status_code >= 400:
                        # Not retried: see the docstring.
                        raise FetchError(f"request rejected: {response.status_code}")

                    # The declared length is a hint, not a promise, so it is
                    # checked first as a cheap rejection and the real cap is
                    # enforced while reading.
                    declared = response.headers.get("Content-Length")
                    if declared and declared.isdigit() and int(declared) > max_bytes:
                        raise FetchError(f"response declares {declared} bytes, over the cap")

                    chunks: list[bytes] = []
                    total = 0
                    for chunk in response.iter_content(chunk_size=65536):
                        total += len(chunk)
                        if total > max_bytes:
                            raise FetchError(f"response exceeded {max_bytes} bytes")
                        chunks.append(chunk)

                body = b"".join(chunks)

            try:
                return json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                # Not retried either. A body that is not JSON now will not be
                # JSON in two seconds.
                raise FetchError(f"response was not JSON: {exc}") from exc

        except (requests.Timeout, requests.ConnectionError) as exc:
            last = exc
        except FetchError as exc:
            if "server error" not in str(exc):
                raise
            last = exc

        if attempt < attempts:
            time.sleep(0.5 * (2 ** (attempt - 1)))

    raise FetchError(f"giving up on {url} after {attempts} attempts: {last}")
