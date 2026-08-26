"""HTTP transport and lifecycle for Carbon's Massive Ads client."""

from __future__ import annotations

from dataclasses import dataclass
from email.utils import formatdate
from itertools import count
import json
import logging
from pathlib import Path
import socket
from threading import Event, Lock, Thread
import time
from urllib.parse import urlsplit

from carbon.core.config import Endpoint
from carbon.mad.campaigns import MADCampaignCatalog
from carbon.mad.protocol import (
    MADAssetCatalogEntry,
    MADProtocolError,
    encode_authenticated_open_session_response,
    encode_asset_catalogs_enter_zone_response,
    encode_close_session_response,
    encode_exit_zone_response,
    encode_impression_update_response,
    encode_locate_service_response,
    parse_close_session_request,
    parse_enter_zone_request,
    parse_exit_zone_request,
    parse_impression_update_request,
    parse_locate_service_request,
    parse_open_session_request,
    verify_message_authenticator,
)


log = logging.getLogger(__name__)

MAX_HEADER_SIZE = 16 * 1024
MAX_BODY_SIZE = 1024 * 1024
HORIZONTAL_ASSET_PATH = "/adsrv/assets/nfs-online-billboard-horizontal.dds"
VERTICAL_ASSET_PATH = "/adsrv/assets/nfs-online-billboard-vertical.dds"
PANORAMIC_ASSET_PATH = "/adsrv/assets/nfs-online-billboard-panoramic.dds"
_ASSET_DIRECTORY = Path(__file__).with_name("assets")
_HORIZONTAL_SOURCE = _ASSET_DIRECTORY / "nfs-online-billboard-horizontal.dds"
_VERTICAL_SOURCE = _ASSET_DIRECTORY / "nfs-online-billboard-vertical.dds"
_PANORAMIC_SOURCE = _ASSET_DIRECTORY / "nfs-online-billboard-panoramic.dds"
HORIZONTAL_ASSET_BODY = _HORIZONTAL_SOURCE.read_bytes()
VERTICAL_ASSET_BODY = _VERTICAL_SOURCE.read_bytes()
PANORAMIC_ASSET_BODY = _PANORAMIC_SOURCE.read_bytes()
ASSET_BODIES = {
    HORIZONTAL_ASSET_PATH: HORIZONTAL_ASSET_BODY,
    VERTICAL_ASSET_PATH: VERTICAL_ASSET_BODY,
    PANORAMIC_ASSET_PATH: PANORAMIC_ASSET_BODY,
}
_BUILTIN_ASSETS = {
    "horizontal": HORIZONTAL_ASSET_BODY,
    "vertical": VERTICAL_ASSET_BODY,
    "panoramic": PANORAMIC_ASSET_BODY,
}
_BUILTIN_PATHS = {
    "horizontal": _HORIZONTAL_SOURCE,
    "vertical": _VERTICAL_SOURCE,
    "panoramic": _PANORAMIC_SOURCE,
}
HORIZONTAL_PLACEMENT_NAMES = (
    "ADS_AUTOZONEDURALAST_01_D",
    "ADS_AUTOZONE_01_D",
    "ADS_CASTROLGTX_01_D",
    "ADS_CASTROL_01_D",
    "ADS_COOPER_01_D",
    "ADS_FAKE1_01_D",
    "ADS_FAKE2_01_D",
    "ADS_FASTFOOD_01_D",
    "ADS_KNN_01_D",
    "ADS_MAZDAMX5_01_D",
    "ADS_MAZDARX8_01_D",
    "ADS_MAZDASPEED3_01_D",
    "ADS_PROGRESSIVE_01_D",
    "ADS_SILVERTON_01_D",
    "ADS_TMOBILE_01_D",
)
VERTICAL_PLACEMENT_NAMES = (
    "ADS_AUTOZONE_02_D",
    "ADS_COOPER_02_D",
    "ADS_FAKE2_02_D",
    "ADS_FASTFOOD_02_D",
    "ADS_KNN_02_D",
    "ADS_MAZDAMX5_02_D",
    "ADS_MAZDASPEED3_02_D",
    "ADS_TMOBILE_02_D",
)
PANORAMIC_PLACEMENT_NAMES = (
    "ADS_CASTROLGTX_03_D",
    "ADS_COOPER_03_D",
    "ADS_KNN_03_D",
    "ADS_MAZDAMX5_03_D",
    "ADS_MAZDARX8_03_D",
    "ADS_MAZDASPEED3_03_D",
    "ADS_PROGRESSIVE_03_D",
    "ADS_TMOBILE_03_D",
)
_LAYOUT_PLACEMENTS = {
    "horizontal": HORIZONTAL_PLACEMENT_NAMES,
    "vertical": VERTICAL_PLACEMENT_NAMES,
    "panoramic": PANORAMIC_PLACEMENT_NAMES,
}
_LAYOUT_INDEX = {"horizontal": 0, "vertical": 1, "panoramic": 2}
# Backward-compatible names retained for protocol tests and diagnostic imports.
TEST_ASSET_PATH = HORIZONTAL_ASSET_PATH
TEST_ASSET_BODY = HORIZONTAL_ASSET_BODY
TEST_PLACEMENT_NAMES = (
    HORIZONTAL_PLACEMENT_NAMES
    + VERTICAL_PLACEMENT_NAMES
    + PANORAMIC_PLACEMENT_NAMES
)


@dataclass(frozen=True)
class MADHTTPRequest:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes


@dataclass
class MADSession:
    session_id: int
    session_key: bytes
    client_id: str
    platform: str
    created_at: float
    last_seen_at: float
    current_zone: str | None = None
    entered_zone_at: float | None = None
    campaign_id: str | None = None
    impression_updates: int = 0


class MADImpressionLog:
    """Append-only JSONL audit for impression and interaction reports."""

    def __init__(self, path: str | Path | None) -> None:
        self.path = Path(path) if path else None
        self._lock = Lock()

    def append(self, event: dict[str, object]) -> None:
        if self.path is None:
            return
        rendered = json.dumps(event, ensure_ascii=True, separators=(",", ":"))
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(rendered + "\n")


def normalize_request_target(target: str) -> str:
    """Return the path from either HTTP origin-form or retail absolute-form."""
    parsed = urlsplit(target)
    return parsed.path


def _receive_request(conn: socket.socket, stop_event: Event) -> MADHTTPRequest:
    data = bytearray()
    header_end = -1
    while not stop_event.is_set():
        chunk = conn.recv(4096)
        if not chunk:
            raise MADProtocolError("peer closed before the HTTP request completed")
        data.extend(chunk)
        header_end = data.find(b"\r\n\r\n")
        if header_end >= 0:
            break
        if len(data) > MAX_HEADER_SIZE:
            raise MADProtocolError("MAD HTTP headers exceed 16384 bytes")
    if header_end < 0:
        raise MADProtocolError("server stopped before the HTTP request completed")

    raw_headers = bytes(data[:header_end])
    try:
        lines = raw_headers.decode("iso-8859-1").split("\r\n")
        method, path, version = lines[0].split(" ", 2)
    except (UnicodeDecodeError, ValueError) as exc:
        raise MADProtocolError("malformed MAD HTTP request line") from exc
    if version != "HTTP/1.1":
        raise MADProtocolError(f"unsupported MAD HTTP version {version!r}")

    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" not in line:
            raise MADProtocolError("malformed MAD HTTP header")
        name, value = line.split(":", 1)
        headers[name.strip().casefold()] = value.strip()
    try:
        content_length = int(headers.get("content-length", "0"))
    except ValueError as exc:
        raise MADProtocolError("invalid MAD Content-Length") from exc
    if not 0 <= content_length <= MAX_BODY_SIZE:
        raise MADProtocolError("MAD request body is too large")

    body_start = header_end + 4
    required = body_start + content_length
    while len(data) < required and not stop_event.is_set():
        chunk = conn.recv(min(4096, required - len(data)))
        if not chunk:
            raise MADProtocolError("peer closed before the MAD body completed")
        data.extend(chunk)
    if len(data) < required:
        raise MADProtocolError("server stopped before the MAD body completed")
    return MADHTTPRequest(method, path, headers, bytes(data[body_start:required]))


def _http_response(
    status: str,
    body: bytes = b"",
    *,
    content_type: str | None = None,
) -> bytes:
    type_header = f"Content-Type: {content_type}\r\n" if content_type else ""
    headers = (
        f"HTTP/1.1 {status}\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"{type_header}"
        "Connection: close\r\n"
        f"Date: {formatdate(usegmt=True)}\r\n"
        "\r\n"
    ).encode("ascii")
    return headers + body


class CarbonMADService:
    def __init__(
        self,
        public_endpoint: Endpoint,
        *,
        campaign_path: str | Path | None = None,
        rotation_seconds: int = 300,
        session_timeout_seconds: float = 900.0,
        impression_log_path: str | Path | None = None,
    ) -> None:
        if rotation_seconds < 0:
            raise ValueError("MAD rotation seconds must be zero or positive")
        if session_timeout_seconds <= 0:
            raise ValueError("MAD session timeout must be positive")
        self.public_endpoint = public_endpoint
        self.session_timeout_seconds = float(session_timeout_seconds)
        self.campaigns = MADCampaignCatalog.load(
            campaign_path,
            fallback_assets=_BUILTIN_ASSETS,
            fallback_paths=_BUILTIN_PATHS,
            default_rotation_seconds=rotation_seconds,
        )
        self.impressions = MADImpressionLog(impression_log_path)
        self._session_ids = count(1)
        self._session_lock = Lock()
        self._sessions: dict[int, MADSession] = {}
        self._asset_bodies: dict[str, bytes] = dict(ASSET_BODIES)
        self._asset_downloads: dict[str, int] = {}
        self._campaign_urls: dict[tuple[str, str], str] = {}
        self._janitor_stop = Event()
        self._janitor_thread: Thread | None = None
        self._register_campaign_assets()

    def start(self) -> None:
        if self._janitor_thread is not None and self._janitor_thread.is_alive():
            return
        self._janitor_stop.clear()
        self._janitor_thread = Thread(
            target=self._janitor_loop,
            name="carbon-mad-session-janitor",
            daemon=True,
        )
        self._janitor_thread.start()

    def stop(self) -> None:
        self._janitor_stop.set()
        thread = self._janitor_thread
        if thread is not None:
            thread.join(timeout=2.0)
        self._janitor_thread = None

    def expire_sessions(self) -> tuple[MADSession, ...]:
        with self._session_lock:
            expired = self._cleanup_sessions_locked(time.monotonic())
        self._log_expired(expired)
        return expired

    @property
    def active_session_count(self) -> int:
        with self._session_lock:
            self._cleanup_sessions_locked(time.monotonic())
            return len(self._sessions)

    def session_snapshot(self, session_id: int) -> MADSession | None:
        with self._session_lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            return MADSession(**session.__dict__)

    def handle_connection(
        self,
        conn: socket.socket,
        addr: tuple[str, int],
        stop_event: Event,
    ) -> None:
        conn.settimeout(5.0)
        try:
            request = _receive_request(conn, stop_event)
        except (MADProtocolError, OSError) as exc:
            log.warning(
                "Carbon MAD invalid request: peer=%s:%d error=%s",
                addr[0],
                addr[1],
                exc,
            )
            try:
                conn.sendall(_http_response("400 Bad Request"))
            except OSError:
                pass
            return

        request_path = normalize_request_target(request.path)
        asset_body = self._asset_bodies.get(request_path)
        if request.method == "GET":
            if asset_body is None:
                conn.sendall(_http_response("404 Not Found"))
                return
            conn.sendall(
                _http_response(
                    "200 OK",
                    asset_body,
                    content_type="application/octet-stream",
                )
            )
            self._asset_downloads[request_path] = (
                self._asset_downloads.get(request_path, 0) + 1
            )
            log.info(
                "Carbon MAD asset download: peer=%s:%d path=%s bytes=%d count=%d",
                addr[0],
                addr[1],
                request_path,
                len(asset_body),
                self._asset_downloads[request_path],
            )
            return
        if request.method != "POST":
            log.warning(
                "Carbon MAD unsupported HTTP request: peer=%s:%d method=%s "
                "target=%s normalized_path=%s",
                addr[0],
                addr[1],
                request.method,
                request.path,
                request_path,
            )
            conn.sendall(_http_response("405 Method Not Allowed"))
            return

        handlers = {
            "/adsrv/locateService": self._handle_locate_service,
            "/adsrv/openSession": self._handle_open_session,
            "/adsrv/enterZone": self._handle_enter_zone,
            "/adsrv/exitZone": self._handle_exit_zone,
            "/impsrv/impressionUpdate": self._handle_impression_update,
            "/adsrv/closeSession": self._handle_close_session,
        }
        handler = handlers.get(request_path)
        if handler is not None:
            handler(conn, addr, request)
            return

        log.warning(
            "Carbon MAD unhandled request: peer=%s:%d path=%s content=%d raw=%s",
            addr[0],
            addr[1],
            request.path,
            len(request.body),
            request.body.hex(),
        )
        conn.sendall(_http_response("501 Not Implemented"))

    def _handle_locate_service(
        self,
        conn: socket.socket,
        addr: tuple[str, int],
        request: MADHTTPRequest,
    ) -> None:
        try:
            locate = parse_locate_service_request(request.body)
            body = encode_locate_service_response(self.public_endpoint.host)
        except MADProtocolError as exc:
            self._reject(conn, addr, "LocateService", request, exc)
            return
        conn.sendall(_http_response("200 OK", body))
        log.info(
            "Carbon MAD LocateService: peer=%s:%d product=%s version=%s "
            "services=%s:%d body=%d",
            addr[0],
            addr[1],
            locate.product,
            locate.version,
            self.public_endpoint.host,
            self.public_endpoint.port,
            len(body),
        )

    def _handle_open_session(
        self,
        conn: socket.socket,
        addr: tuple[str, int],
        request: MADHTTPRequest,
    ) -> None:
        try:
            opened = parse_open_session_request(request.body)
            try:
                session_key = opened.client_id.encode("ascii")
            except UnicodeEncodeError as exc:
                raise MADProtocolError("OpenSession client id is not ASCII") from exc
            if not verify_message_authenticator(request.body, session_key):
                raise MADProtocolError("OpenSession authenticator mismatch")
            now = time.monotonic()
            with self._session_lock:
                expired = self._cleanup_sessions_locked(now)
                session_id = next(self._session_ids)
                session = MADSession(
                    session_id=session_id,
                    session_key=session_key,
                    client_id=opened.client_id,
                    platform=opened.platform,
                    created_at=now,
                    last_seen_at=now,
                )
                self._sessions[session_id] = session
            body = encode_authenticated_open_session_response(session_id, session_key)
        except MADProtocolError as exc:
            self._reject(conn, addr, "OpenSession", request, exc)
            return
        self._log_expired(expired)
        conn.sendall(_http_response("200 OK", body))
        log.info(
            "Carbon MAD OpenSession: peer=%s:%d product=%s version=%s "
            "protocol=%d client=%s platform=%s session=%d key_exchange=%d "
            "identity=%d",
            addr[0],
            addr[1],
            opened.product,
            opened.version,
            opened.protocol,
            opened.client_id,
            opened.platform,
            session_id,
            len(opened.key_exchange),
            len(opened.identity),
        )

    def _handle_enter_zone(
        self,
        conn: socket.socket,
        addr: tuple[str, int],
        request: MADHTTPRequest,
    ) -> None:
        try:
            entered = parse_enter_zone_request(request.body)
            now = time.monotonic()
            with self._session_lock:
                expired = self._cleanup_sessions_locked(now)
                session = self._require_session_locked(
                    entered.session_id,
                    request.body,
                    operation="EnterZone",
                )
                if entered.result != 1:
                    raise MADProtocolError(
                        f"EnterZone carries unsuccessful OpenSession result {entered.result}"
                    )
                selection = self.campaigns.select(entered.zone, unix_time=time.time())
                session.last_seen_at = now
                session.current_zone = entered.zone
                session.entered_zone_at = now
                session.campaign_id = selection.campaign.campaign_id
                session_key = session.session_key
            catalogs = self._catalog_entries(selection.campaign.campaign_id)
            body = encode_asset_catalogs_enter_zone_response(
                session_key,
                catalogs=catalogs,
            )
        except MADProtocolError as exc:
            self._reject(conn, addr, "EnterZone", request, exc)
            return
        self._log_expired(expired)
        conn.sendall(_http_response("200 OK", body))
        log.info(
            "Carbon MAD EnterZone: peer=%s:%d session=%d zone=%s campaign=%s "
            "rotation_slot=%d assets=%d creatives=%d network=%s/%s:%s",
            addr[0],
            addr[1],
            entered.session_id,
            entered.zone,
            selection.campaign.campaign_id,
            selection.rotation_slot,
            len(catalogs),
            len(TEST_PLACEMENT_NAMES),
            entered.public_address,
            entered.private_address,
            entered.port,
        )

    def _handle_exit_zone(
        self,
        conn: socket.socket,
        addr: tuple[str, int],
        request: MADHTTPRequest,
    ) -> None:
        try:
            exited = parse_exit_zone_request(request.body)
            now = time.monotonic()
            with self._session_lock:
                expired = self._cleanup_sessions_locked(now)
                session = self._require_session_locked(
                    exited.session_id,
                    request.body,
                    operation="ExitZone",
                )
                previous_zone = session.current_zone
                duration = (
                    max(0.0, now - session.entered_zone_at)
                    if session.entered_zone_at is not None
                    else None
                )
                session.last_seen_at = now
                session.current_zone = None
                session.entered_zone_at = None
                session.campaign_id = None
                body = encode_exit_zone_response(session.session_key)
        except MADProtocolError as exc:
            self._reject(conn, addr, "ExitZone", request, exc)
            return
        self._log_expired(expired)
        conn.sendall(_http_response("200 OK", body))
        log.info(
            "Carbon MAD ExitZone: peer=%s:%d session=%d requested_zone=%s "
            "active_zone=%s duration=%.3f result=%d",
            addr[0],
            addr[1],
            exited.session_id,
            exited.zone,
            previous_zone,
            duration or 0.0,
            exited.result,
        )

    def _handle_impression_update(
        self,
        conn: socket.socket,
        addr: tuple[str, int],
        request: MADHTTPRequest,
    ) -> None:
        try:
            update = parse_impression_update_request(request.body)
            now = time.monotonic()
            with self._session_lock:
                expired = self._cleanup_sessions_locked(now)
                session = self._require_session_locked(
                    update.session_id,
                    request.body,
                    operation="ImpressionUpdate",
                )
                session.last_seen_at = now
                session.impression_updates += 1
                snapshot = MADSession(**session.__dict__)
                body = encode_impression_update_response(session.session_key)
        except MADProtocolError as exc:
            self._reject(conn, addr, "ImpressionUpdate", request, exc)
            return
        self._log_expired(expired)
        self.impressions.append(
            {
                "timestamp": time.time(),
                "session_id": update.session_id,
                "client_id": snapshot.client_id,
                "platform": snapshot.platform,
                "zone": snapshot.current_zone,
                "campaign": snapshot.campaign_id,
                "result": update.result,
                "impression_records": len(update.impression_records),
                "interaction_records": len(update.interaction_records),
                "impression_record_bytes": sum(map(len, update.impression_records)),
                "interaction_record_bytes": sum(map(len, update.interaction_records)),
            }
        )
        conn.sendall(_http_response("200 OK", body))
        log.info(
            "Carbon MAD ImpressionUpdate: peer=%s:%d session=%d zone=%s "
            "campaign=%s impressions=%d interactions=%d update=%d",
            addr[0],
            addr[1],
            update.session_id,
            snapshot.current_zone,
            snapshot.campaign_id,
            len(update.impression_records),
            len(update.interaction_records),
            snapshot.impression_updates,
        )

    def _handle_close_session(
        self,
        conn: socket.socket,
        addr: tuple[str, int],
        request: MADHTTPRequest,
    ) -> None:
        try:
            closed = parse_close_session_request(request.body)
            now = time.monotonic()
            with self._session_lock:
                expired = self._cleanup_sessions_locked(now)
                session = self._require_session_locked(
                    closed.session_id,
                    request.body,
                    operation="CloseSession",
                )
                body = encode_close_session_response(session.session_key)
                lifetime = max(0.0, now - session.created_at)
                self._sessions.pop(closed.session_id, None)
        except MADProtocolError as exc:
            self._reject(conn, addr, "CloseSession", request, exc)
            return
        self._log_expired(expired)
        conn.sendall(_http_response("200 OK", body))
        log.info(
            "Carbon MAD CloseSession: peer=%s:%d session=%d client=%s "
            "lifetime=%.3f impression_updates=%d result=%d",
            addr[0],
            addr[1],
            closed.session_id,
            session.client_id,
            lifetime,
            session.impression_updates,
            closed.result,
        )

    def _register_campaign_assets(self) -> None:
        for campaign in self.campaigns.campaigns:
            for layout, body in campaign.assets.items():
                path = f"/adsrv/assets/{campaign.campaign_id}-{layout}.dds"
                existing = self._asset_bodies.get(path)
                if existing is not None and existing != body:
                    raise ValueError(f"MAD asset URL collision at {path}")
                self._asset_bodies[path] = body
                self._campaign_urls[(campaign.campaign_id, layout)] = path

    def _catalog_entries(self, campaign_id: str) -> tuple[MADAssetCatalogEntry, ...]:
        campaign_index = next(
            index
            for index, campaign in enumerate(self.campaigns.campaigns)
            if campaign.campaign_id == campaign_id
        )
        campaign = self.campaigns.campaigns[campaign_index]
        asset_base_url = f"http://{self.public_endpoint.host}:{self.public_endpoint.port}"
        catalogs: list[MADAssetCatalogEntry] = []
        for layout in ("horizontal", "vertical", "panoramic"):
            layout_index = _LAYOUT_INDEX[layout]
            catalogs.append(
                MADAssetCatalogEntry(
                    asset_url=asset_base_url
                    + self._campaign_urls[(campaign.campaign_id, layout)],
                    asset_body=campaign.assets[layout],
                    placement_names=_LAYOUT_PLACEMENTS[layout],
                    asset_id=0xC100_0000 + campaign_index * 0x100 + layout_index + 1,
                    placement_id=0xC110_0000
                    + campaign_index * 0x1000
                    + layout_index * 0x100,
                )
            )
        return tuple(catalogs)

    def _require_session_locked(
        self,
        session_id: int,
        body: bytes,
        *,
        operation: str,
    ) -> MADSession:
        session = self._sessions.get(session_id)
        if session is None:
            raise MADProtocolError(f"{operation} references unknown session {session_id}")
        if not verify_message_authenticator(body, session.session_key):
            raise MADProtocolError(f"{operation} authenticator mismatch")
        return session

    def _janitor_loop(self) -> None:
        interval = min(60.0, max(1.0, self.session_timeout_seconds / 2.0))
        while not self._janitor_stop.wait(interval):
            self.expire_sessions()

    def _cleanup_sessions_locked(self, now: float) -> tuple[MADSession, ...]:
        expired_ids = [
            session_id
            for session_id, session in self._sessions.items()
            if now - session.last_seen_at > self.session_timeout_seconds
        ]
        expired = tuple(self._sessions.pop(session_id) for session_id in expired_ids)
        return expired

    @staticmethod
    def _log_expired(expired: tuple[MADSession, ...]) -> None:
        for session in expired:
            log.info(
                "Carbon MAD session expired: session=%d client=%s zone=%s",
                session.session_id,
                session.client_id,
                session.current_zone,
            )

    @staticmethod
    def _reject(
        conn: socket.socket,
        addr: tuple[str, int],
        operation: str,
        request: MADHTTPRequest,
        error: Exception,
    ) -> None:
        log.warning(
            "Carbon MAD malformed %s: peer=%s:%d error=%s raw=%s",
            operation,
            addr[0],
            addr[1],
            error,
            request.body.hex(),
        )
        conn.sendall(_http_response("400 Bad Request"))
