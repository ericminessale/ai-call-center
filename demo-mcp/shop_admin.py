"""
shop_admin.py — hosted-demo-only reset listener for the DemoShop DB.

The bundled DemoShop SQLite DB is shared by every visitor of the public
hosted demo, and ``start_return`` writes to it — so RMAs and flipped
order statuses pile up for the life of the ``demo_mcp_data`` volume, and
one visitor's call leaves debris in the next visitor's. The backend's
workspace GC can't help: the volume is mounted only here. This listener
gives the nightly ``demo-reset`` cron a way to restore seed state, the
same way it drives ``/api/internal/demo-reset`` on the backend.

Three gates, all of which must pass:

  1. **Hosted-demo mode** — ``TENANCY_MODE``/``DEMO_MODE`` = true,
     mirroring the backend's ``utils/demo_config.tenancy_mode_active()``.
     A clone-and-own install leaves both unset, so :func:`main` exits
     without binding a socket and this container's only surface stays the
     gateway on 8100. That install owns its shop data; nothing should be
     able to wipe it on a timer.
  2. **HTTP Basic auth** — ``SHOP_RESET_USER``/``SHOP_RESET_PASSWORD``,
     falling back to the ``DEMO_MCP_USER``/``DEMO_MCP_PASSWORD`` pair
     that already guards this container's gateway port (same trust
     boundary, so no new secret has to be distributed). Fails closed:
     no password resolvable → every request is refused.
  3. **Network reach** — the service publishes no ``ports:``, so this
     port is reachable only from sibling containers on the compose
     network, never from the internet.

Endpoint::

    POST /internal/shop-reset  →  200 {"ok": true, "customers": 5, ...}

Deliberately stdlib-only: this runs as a side process next to
mcp-gateway in a container whose deps are the SDK plus ``mcp``, and a
one-route reset endpoint doesn't justify another framework.
"""

from __future__ import annotations

import base64
import hmac
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

from shop_seed import seed

RESET_PATH = "/internal/shop-reset"
DEFAULT_PORT = 8101


def hosted_demo_mode() -> bool:
    """True on the public hosted demo, false for clone-and-own.

    Mirror of the backend's ``tenancy_mode_active()``
    (``backend/app/utils/demo_config.py``) — same two env vars, same
    "read it live" semantics. Keep the two in sync.
    """
    return (
        os.environ.get('TENANCY_MODE', '').strip().lower() == 'true'
        or os.environ.get('DEMO_MODE', '').strip().lower() == 'true'
    )


def _credentials() -> tuple[str, str]:
    """The Basic-auth pair this listener accepts, ``(user, password)``.

    Dedicated ``SHOP_RESET_*`` values win; otherwise we reuse the
    gateway's own ``DEMO_MCP_*`` credentials. The username default
    matches ``gateway-config.json``'s ``${DEMO_MCP_USER|demo}``. An
    empty password is the fail-closed signal, never a wildcard.
    """
    user = (
        os.environ.get('SHOP_RESET_USER')
        or os.environ.get('DEMO_MCP_USER')
        or 'demo'
    ).strip()
    password = (
        os.environ.get('SHOP_RESET_PASSWORD')
        or os.environ.get('DEMO_MCP_PASSWORD')
        or ''
    )
    return user, password


def _authorized(header: str | None) -> bool:
    """Validate an ``Authorization: Basic …`` header, constant-time."""
    user, password = _credentials()
    if not password:
        return False
    if not header or not header.startswith('Basic '):
        return False
    try:
        decoded = base64.b64decode(header[6:].strip(), validate=True).decode('utf-8')
    except Exception:
        return False
    got_user, sep, got_password = decoded.partition(':')
    if not sep:
        return False
    # Compare on bytes (compare_digest rejects non-ASCII str), and
    # evaluate both halves before combining — a short-circuiting `and`
    # would leak "the username was wrong" in the response timing.
    user_ok = hmac.compare_digest(got_user.encode('utf-8'), user.encode('utf-8'))
    password_ok = hmac.compare_digest(
        got_password.encode('utf-8'), password.encode('utf-8')
    )
    return user_ok and password_ok


def _port() -> int:
    raw = os.environ.get('SHOP_ADMIN_PORT', '').strip()
    try:
        return max(1, min(int(raw), 65535))
    except ValueError:
        return DEFAULT_PORT


class _AdminHandler(BaseHTTPRequestHandler):
    """One route, one verb. Everything else 404s or 405s."""

    server_version = 'demoshop-admin/1.0'
    # HTTP/1.0 (the default) closes the connection after each response, so
    # there's no keep-alive framing to keep in sync with an ignored body.

    def _respond(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        if status == 401:
            self.send_header('WWW-Authenticate', 'Basic realm="demoshop-admin"')
        self.end_headers()
        self.wfile.write(body)

    def _drain_body(self) -> None:
        """Read and discard any request body — the reset takes no input."""
        try:
            length = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            return
        if length > 0:
            self.rfile.read(min(length, 64 * 1024))

    def do_POST(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler API
        if self.path.split('?', 1)[0] != RESET_PATH:
            self._respond(404, {'ok': False, 'error': 'not found'})
            return
        # Re-check the demo flag per request even though main() already
        # gated on it: same "read the env live" posture as the backend, and
        # it costs nothing to refuse twice.
        if not hosted_demo_mode():
            self._respond(404, {'ok': False, 'error': 'not found'})
            return
        if not _authorized(self.headers.get('Authorization')):
            self.log_message('shop reset REFUSED (bad or missing credentials)')
            self._respond(401, {'ok': False, 'error': 'unauthorized'})
            return

        self._drain_body()
        try:
            result = seed(force=True)
        except Exception as exc:  # sqlite lock timeout, disk full, …
            self.log_message('shop reset FAILED: %s', exc)
            self._respond(500, {'ok': False, 'error': str(exc)})
            return
        self.log_message('shop reset ok: %s', result)
        self._respond(200, {'ok': True, **result})

    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler API
        # No read surface here, and a GET must never trigger the write.
        if self.path.split('?', 1)[0] == RESET_PATH:
            self._respond(405, {'ok': False, 'error': 'use POST'})
        else:
            self._respond(404, {'ok': False, 'error': 'not found'})

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write(
            "[demo-mcp:admin] %s %s\n" % (self.address_string(), fmt % args)
        )
        sys.stderr.flush()


def main() -> int:
    if not hosted_demo_mode():
        print(
            "[demo-mcp:admin] TENANCY_MODE/DEMO_MODE not set — shop-reset "
            "listener disabled (clone-and-own owns its shop data).",
            flush=True,
        )
        return 0

    if not _credentials()[1]:
        print(
            "[demo-mcp:admin] WARNING: neither SHOP_RESET_PASSWORD nor "
            "DEMO_MCP_PASSWORD is set — every reset request will 401. Wire "
            "creds in the demo overlay's env.",
            flush=True,
        )

    port = _port()
    server = HTTPServer(('0.0.0.0', port), _AdminHandler)
    print(
        f"[demo-mcp:admin] hosted demo — POST {RESET_PATH} on :{port} "
        "restores the seeded shop DB",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
