"""Responsive self-service Carbon DLC store.

The store deliberately has no third-party dependencies. It authenticates with
shared EA accounts, stores only opaque in-memory browser sessions, and writes
per-account DLC selections through :class:`CarbonDLCAssignmentStore`.
"""

from __future__ import annotations

from dataclasses import dataclass
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import html
import hmac
import json
import logging
import secrets
from threading import RLock, Thread
import time
from typing import Mapping
from urllib.parse import parse_qs, urlsplit

from carbon.core.config import Endpoint
from carbon.dlc import CarbonDLCConfigError, CarbonDLCInventory
from common.accounts import SQLiteAccountDatabase


log = logging.getLogger(__name__)
_MAX_FORM_BYTES = 64 * 1024
_MAX_SESSIONS = 10_000
_COOKIE_NAME = "nfs_dlc_session"


@dataclass(frozen=True)
class _BrowserSession:
    account_name: str
    csrf_token: str
    expires_at: float


class _StoreHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


class CarbonDLCStoreServer:
    """Own a mobile-first DLC page and its authenticated account sessions."""

    def __init__(
        self,
        endpoint: Endpoint,
        database: SQLiteAccountDatabase,
        inventory: CarbonDLCInventory,
        *,
        session_seconds: float = 43_200.0,
        cookie_secure: str = "auto",
    ) -> None:
        if inventory.assignment_store is None:
            raise ValueError("Carbon DLC inventory requires a writable assignment store")
        secure_mode = str(cookie_secure or "auto").strip().casefold()
        if secure_mode not in {"auto", "always", "never"}:
            raise ValueError("DLC store cookie mode must be auto, always or never")
        if float(session_seconds) <= 0:
            raise ValueError("DLC store session lifetime must be positive")
        self.endpoint = endpoint
        self.database = database
        self.inventory = inventory
        self.assignment_store = inventory.assignment_store
        self.session_seconds = float(session_seconds)
        self.cookie_secure = secure_mode
        self._sessions: dict[str, _BrowserSession] = {}
        self._session_lock = RLock()
        self._httpd: _StoreHTTPServer | None = None
        self._thread: Thread | None = None

    def start(self) -> Endpoint:
        if self._httpd is not None:
            host, port = self._httpd.server_address[:2]
            return Endpoint(str(host), int(port))

        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version = "NFSOnlineDLC/1.1"
            sys_version = ""

            def setup(self) -> None:
                super().setup()
                self.connection.settimeout(15.0)

            def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
                owner._handle_get(self)

            def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
                owner._handle_post(self)

            def log_message(self, format: str, *args: object) -> None:
                log.info("DLC store %s - %s", self.address_string(), format % args)

        httpd = _StoreHTTPServer(
            (self.endpoint.host, self.endpoint.port),
            Handler,
        )
        thread = Thread(
            target=httpd.serve_forever,
            kwargs={"poll_interval": 0.25},
            name="carbon-dlc-store",
            daemon=True,
        )
        self._httpd = httpd
        self._thread = thread
        thread.start()
        host, port = httpd.server_address[:2]
        return Endpoint(str(host), int(port))

    def stop(self) -> None:
        httpd = self._httpd
        thread = self._thread
        self._httpd = None
        self._thread = None
        if httpd is None:
            return
        httpd.shutdown()
        httpd.server_close()
        if thread is not None:
            thread.join(timeout=3.0)
        with self._session_lock:
            self._sessions.clear()

    def _handle_get(self, request: BaseHTTPRequestHandler) -> None:
        path = urlsplit(request.path).path.rstrip("/") or "/"
        if path == "/":
            self._redirect(request, "/dlc")
            return
        if path == "/health":
            self._send_json(
                request,
                {
                    "ok": True,
                    "service": "carbon-dlc-store",
                    "groups": len(self.inventory.catalog.groups),
                },
            )
            return
        if path != "/dlc":
            self._send_error_page(request, HTTPStatus.NOT_FOUND, "Page not found.")
            return

        session = self._authenticated_session(request)
        if session is None:
            self._send_html(request, self._login_page())
            return
        query = parse_qs(urlsplit(request.path).query, keep_blank_values=True)
        saved = query.get("saved", [""])[0] == "1"
        self._send_html(request, self._store_page(session, saved=saved))

    def _handle_post(self, request: BaseHTTPRequestHandler) -> None:
        path = urlsplit(request.path).path.rstrip("/") or "/"
        try:
            fields = self._read_form(request)
        except ValueError as exc:
            self._send_error_page(request, HTTPStatus.BAD_REQUEST, str(exc))
            return

        if path == "/dlc/login":
            self._login(request, fields)
            return
        if path == "/dlc/save":
            self._save(request, fields)
            return
        if path == "/dlc/logout":
            self._logout(request, fields)
            return
        self._send_error_page(request, HTTPStatus.NOT_FOUND, "Page not found.")

    def _read_form(self, request: BaseHTTPRequestHandler) -> dict[str, list[str]]:
        content_type = request.headers.get("Content-Type", "").split(";", 1)[0].strip().casefold()
        if content_type != "application/x-www-form-urlencoded":
            raise ValueError("Invalid form.")
        try:
            length = int(request.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Invalid form length.") from exc
        if not 0 <= length <= _MAX_FORM_BYTES:
            raise ValueError("The form is too large.")
        body = request.rfile.read(length)
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("The form is not valid UTF-8.") from exc
        try:
            return parse_qs(
                text,
                keep_blank_values=True,
                strict_parsing=False,
                max_num_fields=250,
            )
        except ValueError as exc:
            raise ValueError("Invalid form.") from exc

    @staticmethod
    def _one(
        fields: Mapping[str, list[str]],
        name: str,
        *,
        strip: bool = True,
    ) -> str:
        values = fields.get(name, [])
        value = str(values[0] if values else "")
        return value.strip() if strip else value

    def _login(
        self,
        request: BaseHTTPRequestHandler,
        fields: Mapping[str, list[str]],
    ) -> None:
        account = self._one(fields, "account")
        password = self._one(fields, "password", strip=False)
        if not account or not password:
            self._send_html(
                request,
                self._login_page("Enter the account name and password."),
                status=HTTPStatus.UNAUTHORIZED,
            )
            return
        result = self.database.authenticate(account, password)
        if not result.accepted:
            self._send_html(
                request,
                self._login_page(
                    "Authentication failed. Check the credentials or try again later."
                ),
                status=HTTPStatus.UNAUTHORIZED,
            )
            return
        canonical = result.account_name or account
        token = secrets.token_urlsafe(32)
        session = _BrowserSession(
            account_name=canonical,
            csrf_token=secrets.token_urlsafe(24),
            expires_at=time.time() + self.session_seconds,
        )
        with self._session_lock:
            self._prune_sessions_locked()
            if len(self._sessions) >= _MAX_SESSIONS:
                oldest = min(
                    self._sessions,
                    key=lambda key: self._sessions[key].expires_at,
                )
                self._sessions.pop(oldest, None)
            self._sessions[token] = session
        self._redirect(
            request,
            "/dlc",
            cookie=self._session_cookie(request, token),
        )

    def _save(
        self,
        request: BaseHTTPRequestHandler,
        fields: Mapping[str, list[str]],
    ) -> None:
        token, session = self._session_with_token(request)
        if session is None or token is None:
            self._redirect(request, "/dlc", cookie=self._expired_cookie(request))
            return
        if not hmac.compare_digest(self._one(fields, "csrf"), session.csrf_token):
            self._send_error_page(request, HTTPStatus.FORBIDDEN, "The request has expired or is invalid.")
            return

        selected: list[str] = []
        seen: set[str] = set()
        for raw in fields.get("group", []):
            key = str(raw or "").strip().casefold()
            if key not in self.inventory.catalog.groups:
                self._send_error_page(request, HTTPStatus.BAD_REQUEST, "Invalid DLC selection.")
                return
            if key not in seen:
                seen.add(key)
                selected.append(key)
        catalog_order = tuple(self.inventory.catalog.groups)
        selected_set = set(selected)
        ordered = tuple(key for key in catalog_order if key in selected_set)
        if len(ordered) == len(catalog_order):
            selectors = ("all",)
        elif ordered:
            selectors = ordered
        else:
            selectors = ("none",)
        try:
            self.assignment_store.set_account(session.account_name, selectors)
        except CarbonDLCConfigError as exc:
            log.exception("failed to update DLC selection for %s", session.account_name)
            self._send_error_page(
                request,
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "The selection could not be saved. Try again.",
            )
            return
        self._redirect(request, "/dlc?saved=1")

    def _logout(
        self,
        request: BaseHTTPRequestHandler,
        fields: Mapping[str, list[str]],
    ) -> None:
        token, session = self._session_with_token(request)
        if session is not None and not hmac.compare_digest(
            self._one(fields, "csrf"), session.csrf_token
        ):
            self._send_error_page(request, HTTPStatus.FORBIDDEN, "The request has expired or is invalid.")
            return
        if token is not None:
            with self._session_lock:
                self._sessions.pop(token, None)
        self._redirect(request, "/dlc", cookie=self._expired_cookie(request))

    def _authenticated_session(
        self,
        request: BaseHTTPRequestHandler,
    ) -> _BrowserSession | None:
        _, session = self._session_with_token(request)
        return session

    def _session_with_token(
        self,
        request: BaseHTTPRequestHandler,
    ) -> tuple[str | None, _BrowserSession | None]:
        cookie = SimpleCookie()
        try:
            cookie.load(request.headers.get("Cookie", ""))
        except Exception:
            return None, None
        morsel = cookie.get(_COOKIE_NAME)
        if morsel is None:
            return None, None
        token = morsel.value
        now = time.time()
        with self._session_lock:
            session = self._sessions.get(token)
            if session is None:
                return token, None
            if session.expires_at <= now:
                self._sessions.pop(token, None)
                return token, None
        account = self.database.resolve_account(session.account_name)
        if account is None or not account.enabled or account.banned:
            with self._session_lock:
                self._sessions.pop(token, None)
            return token, None
        return token, session

    def _prune_sessions_locked(self) -> None:
        now = time.time()
        expired = [
            token
            for token, session in self._sessions.items()
            if session.expires_at <= now
        ]
        for token in expired:
            self._sessions.pop(token, None)

    def _is_secure_request(self, request: BaseHTTPRequestHandler) -> bool:
        if self.cookie_secure == "always":
            return True
        if self.cookie_secure == "never":
            return False
        forwarded_proto = request.headers.get("X-Forwarded-Proto", "")
        if forwarded_proto.split(",", 1)[0].strip().casefold() == "https":
            return True
        forwarded = request.headers.get("Forwarded", "").casefold()
        return "proto=https" in forwarded

    def _session_cookie(self, request: BaseHTTPRequestHandler, token: str) -> str:
        parts = [
            f"{_COOKIE_NAME}={token}",
            "Path=/",
            "HttpOnly",
            "SameSite=Lax",
            f"Max-Age={int(self.session_seconds)}",
        ]
        if self._is_secure_request(request):
            parts.append("Secure")
        return "; ".join(parts)

    def _expired_cookie(self, request: BaseHTTPRequestHandler) -> str:
        parts = [
            f"{_COOKIE_NAME}=",
            "Path=/",
            "HttpOnly",
            "SameSite=Lax",
            "Max-Age=0",
        ]
        if self._is_secure_request(request):
            parts.append("Secure")
        return "; ".join(parts)

    def _store_page(self, session: _BrowserSession, *, saved: bool) -> str:
        selected = set(self.assignment_store.effective_group_keys(session.account_name))
        categories = sorted({group.category for group in self.inventory.catalog.groups.values()})
        cards: list[str] = []
        for group in sorted(
            self.inventory.catalog.groups.values(),
            key=lambda item: (item.category, item.label.casefold(), item.key),
        ):
            checked = " checked" if group.key in selected else ""
            search_value = html.escape(
                f"{group.label} {group.key} {group.category}".casefold(),
                quote=True,
            )
            cards.append(
                f"""
<label class="dlc-card" data-category="{html.escape(group.category, quote=True)}" data-search="{search_value}">
  <input class="dlc-check" type="checkbox" name="group" value="{html.escape(group.key, quote=True)}"{checked}>
  <span class="checkmark" aria-hidden="true"></span>
  <span class="card-copy">
    <span class="card-top"><strong>{html.escape(group.label)}</strong><span class="free">FREE</span></span>
    <span class="meta">{html.escape(group.category.replace('_', ' ').title())} · {len(group.tokens)} {'unlock' if len(group.tokens) == 1 else 'unlocks'}</span>
  </span>
</label>""".strip()
            )
        options = "".join(
            f'<option value="{html.escape(category, quote=True)}">{html.escape(category.replace("_", " ").title())}</option>'
            for category in categories
        )
        notice = (
            '<div class="notice success">The selection was saved. Reconnect to Carbon to see it in game.</div>'
            if saved
            else ""
        )
        body = f"""
<header class="topbar">
  <div><span class="eyebrow">NFS ONLINE</span><h1>Carbon DLC Store</h1></div>
  <form method="post" action="/dlc/logout">
    <input type="hidden" name="csrf" value="{html.escape(session.csrf_token, quote=True)}">
    <button class="ghost" type="submit">Sign out</button>
  </form>
</header>
<main class="shell">
  <section class="hero">
    <div><span class="pill">ALL FREE</span><h2>Choose DLC for your account</h2>
      <p>Account: <strong>{html.escape(session.account_name)}</strong>. The selection is stored per account and does not affect other players.</p>
    </div>
    <div class="summary"><strong id="selectedCount">{len(selected)}</strong><span>of {len(self.inventory.catalog.groups)} packages</span></div>
  </section>
  {notice}
  <form method="post" action="/dlc/save" id="dlcForm">
    <input type="hidden" name="csrf" value="{html.escape(session.csrf_token, quote=True)}">
    <section class="toolbar" aria-label="DLC filters">
      <label class="search"><span>Search</span><input id="searchInput" type="search" placeholder="Car, vinyl, performance…" autocomplete="off"></label>
      <label><span>Category</span><select id="categoryFilter"><option value="">All</option>{options}</select></label>
      <div class="toolbar-buttons"><button type="button" class="secondary" id="selectAll">Select all</button><button type="button" class="secondary" id="clearAll">Clear</button></div>
    </section>
    <section class="grid" id="dlcGrid">{''.join(cards)}</section>
    <div class="empty" id="emptyState" hidden>No DLC matches the current filter.</div>
    <section class="savebar">
      <p>Changes apply the next time you reconnect to Carbon.</p>
      <button class="primary" type="submit">Save to account</button>
    </section>
  </form>
</main>
<script>
(() => {{
  const cards = [...document.querySelectorAll('.dlc-card')];
  const checks = [...document.querySelectorAll('.dlc-check')];
  const search = document.getElementById('searchInput');
  const category = document.getElementById('categoryFilter');
  const count = document.getElementById('selectedCount');
  const empty = document.getElementById('emptyState');
  const updateCount = () => count.textContent = checks.filter(x => x.checked).length;
  const filter = () => {{
    const query = search.value.trim().toLowerCase();
    const selectedCategory = category.value;
    let visible = 0;
    cards.forEach(card => {{
      const show = (!query || card.dataset.search.includes(query)) && (!selectedCategory || card.dataset.category === selectedCategory);
      card.hidden = !show;
      if (show) visible++;
    }});
    empty.hidden = visible !== 0;
  }};
  checks.forEach(check => check.addEventListener('change', updateCount));
  search.addEventListener('input', filter);
  category.addEventListener('change', filter);
  document.getElementById('selectAll').addEventListener('click', () => {{ checks.forEach(x => x.checked = true); updateCount(); }});
  document.getElementById('clearAll').addEventListener('click', () => {{ checks.forEach(x => x.checked = false); updateCount(); }});
}})();
</script>
"""
        return self._document("Carbon DLC Store", body)

    def _login_page(self, error: str = "") -> str:
        alert = f'<div class="notice error">{html.escape(error)}</div>' if error else ""
        body = f"""
<main class="login-shell">
  <section class="login-card">
    <span class="eyebrow">NFS ONLINE</span>
    <h1>Carbon DLC Store</h1>
    <p>Sign in with the same account name and password used in game.</p>
    {alert}
    <form method="post" action="/dlc/login" class="login-form">
      <label><span>Account</span><input name="account" autocomplete="username" required maxlength="256"></label>
      <label><span>Password</span><input name="password" type="password" autocomplete="current-password" required maxlength="1024"></label>
      <button class="primary" type="submit">Open DLC Store</button>
    </form>
    <small>All DLC is free. Selections are stored separately for each account.</small>
  </section>
</main>
"""
        return self._document("Carbon DLC Store — Login", body)

    @staticmethod
    def _document(title: str, body: str) -> str:
        return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="dark">
<title>{html.escape(title)}</title>
<style>
:root{{--bg:#080b10;--panel:#111722;--panel2:#171f2c;--line:#293345;--text:#f5f7fb;--muted:#9ba8ba;--accent:#ff7a18;--accent2:#ffb347;--ok:#39d98a;--danger:#ff6677;--radius:18px;font-family:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}}
*{{box-sizing:border-box}}html{{background:var(--bg);color:var(--text)}}body{{margin:0;min-height:100vh;background:radial-gradient(circle at 15% 0,#20283a 0,transparent 35rem),var(--bg)}}button,input,select{{font:inherit}}button{{cursor:pointer}}.topbar{{display:flex;align-items:center;justify-content:space-between;gap:16px;max-width:1240px;margin:auto;padding:22px 22px 6px}}h1,h2,p{{margin-top:0}}h1{{font-size:clamp(1.45rem,5vw,2.2rem);margin-bottom:0}}h2{{font-size:clamp(1.55rem,5vw,2.65rem);margin-bottom:10px}}.eyebrow{{font-size:.72rem;letter-spacing:.22em;font-weight:800;color:var(--accent2)}}.shell{{max-width:1240px;margin:auto;padding:18px 22px 110px}}.hero{{display:flex;justify-content:space-between;align-items:end;gap:24px;padding:30px;background:linear-gradient(135deg,#171e2b,#10151e);border:1px solid var(--line);border-radius:var(--radius)}}.hero p{{color:var(--muted);margin-bottom:0;line-height:1.55}}.pill,.free{{display:inline-flex;align-items:center;border-radius:999px;font-weight:900;letter-spacing:.06em}}.pill{{background:rgba(255,122,24,.15);color:var(--accent2);padding:7px 10px;font-size:.72rem;margin-bottom:14px}}.free{{background:rgba(57,217,138,.14);color:var(--ok);padding:5px 8px;font-size:.65rem;white-space:nowrap}}.summary{{min-width:130px;text-align:right}}.summary strong{{display:block;font-size:2.7rem;line-height:1}}.summary span{{display:block;color:var(--muted);font-size:.82rem;margin-top:8px}}.notice{{padding:14px 16px;border-radius:13px;margin:16px 0;font-weight:700}}.notice.success{{background:rgba(57,217,138,.12);border:1px solid rgba(57,217,138,.35);color:#8ff0bd}}.notice.error{{background:rgba(255,102,119,.12);border:1px solid rgba(255,102,119,.35);color:#ff9eaa}}.toolbar{{position:sticky;top:0;z-index:5;display:grid;grid-template-columns:minmax(220px,1fr) 210px auto;gap:12px;margin:18px 0;padding:14px;background:rgba(8,11,16,.92);backdrop-filter:blur(14px);border:1px solid var(--line);border-radius:15px}}label>span{{display:block;color:var(--muted);font-size:.76rem;font-weight:750;margin:0 0 7px 3px}}input,select{{width:100%;min-height:46px;border:1px solid var(--line);border-radius:11px;background:#0c1119;color:var(--text);padding:0 13px;outline:none}}input:focus,select:focus{{border-color:var(--accent);box-shadow:0 0 0 3px rgba(255,122,24,.15)}}.toolbar-buttons{{display:flex;align-items:end;gap:8px}}.primary,.secondary,.ghost{{min-height:46px;border-radius:11px;border:1px solid transparent;padding:0 16px;font-weight:850;color:var(--text)}}.primary{{background:linear-gradient(135deg,var(--accent),#e85500);box-shadow:0 8px 24px rgba(255,90,0,.22)}}.secondary,.ghost{{background:#171e29;border-color:var(--line)}}.ghost{{min-height:40px}}.grid{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}}.dlc-card{{position:relative;display:flex;align-items:center;gap:13px;min-height:94px;padding:17px;border:1px solid var(--line);border-radius:15px;background:var(--panel);cursor:pointer;transition:.14s transform,.14s border-color,.14s background}}.dlc-card:hover{{transform:translateY(-1px);border-color:#46536a}}.dlc-card:has(input:checked){{border-color:rgba(255,122,24,.8);background:linear-gradient(135deg,rgba(255,122,24,.12),var(--panel2))}}.dlc-card input{{position:absolute;left:0;top:0;width:1px;height:1px;min-height:1px;opacity:0;pointer-events:none}}.dlc-card:has(input:focus-visible){{outline:3px solid rgba(255,122,24,.4);outline-offset:2px}}.checkmark{{width:25px;height:25px;flex:0 0 25px;border:2px solid #657086;border-radius:8px;display:grid;place-items:center}}.dlc-card input:checked+.checkmark{{background:var(--accent);border-color:var(--accent)}}.dlc-card input:checked+.checkmark:after{{content:"✓";font-weight:1000;color:white}}.card-copy{{min-width:0;flex:1}}.card-top{{display:flex;align-items:start;justify-content:space-between;gap:10px}}.card-top strong{{font-size:.96rem;line-height:1.25}}.meta{{display:block;color:var(--muted);font-size:.76rem;margin-top:8px}}.savebar{{position:fixed;left:50%;bottom:max(14px,env(safe-area-inset-bottom));transform:translateX(-50%);width:min(calc(100% - 28px),1196px);z-index:10;display:flex;align-items:center;justify-content:space-between;gap:16px;padding:13px 14px 13px 18px;border:1px solid var(--line);border-radius:16px;background:rgba(17,23,34,.96);box-shadow:0 16px 50px rgba(0,0,0,.45);backdrop-filter:blur(16px)}}.savebar p{{margin:0;color:var(--muted);font-size:.86rem}}.empty{{text-align:center;color:var(--muted);padding:40px}}.login-shell{{min-height:100vh;display:grid;place-items:center;padding:22px}}.login-card{{width:min(100%,440px);padding:30px;border:1px solid var(--line);border-radius:22px;background:rgba(17,23,34,.96);box-shadow:0 30px 90px rgba(0,0,0,.45)}}.login-card h1{{font-size:2rem;margin:8px 0 10px}}.login-card>p,.login-card small{{color:var(--muted);line-height:1.5}}.login-form{{display:grid;gap:15px;margin:24px 0 18px}}.login-form .primary{{margin-top:4px}}
@media(max-width:900px){{.grid{{grid-template-columns:repeat(2,minmax(0,1fr))}}.toolbar{{grid-template-columns:1fr 180px}}.toolbar-buttons{{grid-column:1/-1}}}}
@media(max-width:600px){{.topbar{{padding:17px 14px 4px}}.shell{{padding:13px 14px 110px}}.hero{{padding:21px;align-items:start;flex-direction:column}}.summary{{text-align:left}}.summary strong{{font-size:2rem}}.toolbar{{position:static;grid-template-columns:1fr;padding:12px}}.toolbar-buttons{{grid-column:auto}}.toolbar-buttons button{{flex:1;padding:0 10px}}.grid{{grid-template-columns:1fr}}.dlc-card{{min-height:88px;padding:15px}}.savebar{{align-items:stretch;flex-direction:column;padding:12px}}.savebar p{{display:none}}.savebar .primary{{width:100%}}.login-card{{padding:24px}}}}
</style>
</head>
<body>{body}</body>
</html>"""

    def _send_html(
        self,
        request: BaseHTTPRequestHandler,
        document: str,
        *,
        status: HTTPStatus = HTTPStatus.OK,
    ) -> None:
        payload = document.encode("utf-8")
        request.send_response(int(status))
        self._security_headers(request)
        request.send_header("Content-Type", "text/html; charset=utf-8")
        request.send_header("Content-Length", str(len(payload)))
        request.end_headers()
        request.wfile.write(payload)

    def _send_json(self, request: BaseHTTPRequestHandler, value: object) -> None:
        payload = (json.dumps(value, separators=(",", ":")) + "\n").encode("utf-8")
        request.send_response(HTTPStatus.OK)
        self._security_headers(request)
        request.send_header("Content-Type", "application/json; charset=utf-8")
        request.send_header("Content-Length", str(len(payload)))
        request.end_headers()
        request.wfile.write(payload)

    def _redirect(
        self,
        request: BaseHTTPRequestHandler,
        location: str,
        *,
        cookie: str | None = None,
    ) -> None:
        request.send_response(HTTPStatus.SEE_OTHER)
        self._security_headers(request)
        request.send_header("Location", location)
        if cookie is not None:
            request.send_header("Set-Cookie", cookie)
        request.send_header("Content-Length", "0")
        request.end_headers()

    def _send_error_page(
        self,
        request: BaseHTTPRequestHandler,
        status: HTTPStatus,
        message: str,
    ) -> None:
        body = f"""
<main class="login-shell"><section class="login-card">
<span class="eyebrow">NFS ONLINE</span><h1>{int(status)} · {html.escape(status.phrase)}</h1>
<div class="notice error">{html.escape(message)}</div>
<a href="/dlc" style="color:#ffb347">Back to DLC Store</a>
</section></main>"""
        self._send_html(request, self._document("DLC Store Error", body), status=status)

    @staticmethod
    def _security_headers(request: BaseHTTPRequestHandler) -> None:
        request.send_header("Cache-Control", "no-store, max-age=0")
        request.send_header("Pragma", "no-cache")
        request.send_header("X-Content-Type-Options", "nosniff")
        request.send_header("X-Frame-Options", "DENY")
        request.send_header("Referrer-Policy", "no-referrer")
        request.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
            "img-src 'self' data:; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
        )
