"""Wire codec for Carbon's Massive Ads bootstrap transactions."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac


MAD_ENVELOPE = 0x03
LOCATE_SERVICE_REQUEST = 0xC9
LOCATE_SERVICE_RESPONSE = 0xCA
OPEN_SESSION_REQUEST = 0xCB
OPEN_SESSION_RESPONSE = 0xCC
ENTER_ZONE_REQUEST = 0xCD
ENTER_ZONE_RESPONSE = 0xCE
EXIT_ZONE_REQUEST = 0xCF
EXIT_ZONE_RESPONSE = 0xD0
CLOSE_SESSION_REQUEST = 0xD1
CLOSE_SESSION_RESPONSE = 0xD2
IMPRESSION_UPDATE_REQUEST = 0xD3
IMPRESSION_UPDATE_RESPONSE = 0xD4
AD_OBJECT_RECORD = 0xDD
ASSET_RECORD = 0xDE
IMPRESSION_RECORD = 0xDF
INTERACTION_RECORD = 0xE0

# The recovered Warehouse response uses this fixed Massive service epoch.
REFERENCE_SERVICE_TIMESTAMP_MS = 1_735_689_600_000


class MADProtocolError(ValueError):
    """Raised when a Massive message cannot be decoded safely."""


@dataclass(frozen=True)
class LocateServiceRequest:
    product: str
    version: str


@dataclass(frozen=True)
class OpenSessionRequest:
    product: str
    version: str
    protocol: int
    client_id: str
    platform: str
    key_exchange: bytes
    identity: bytes
    service_timestamp_ms: int
    authenticator: bytes


@dataclass(frozen=True)
class EnterZoneRequest:
    zone: str
    result: int
    session_id: int
    service_timestamp_ms: int
    authenticator: bytes
    public_address: int | None
    private_address: int | None
    port: int | None


@dataclass(frozen=True)
class CloseSessionRequest:
    result: int
    session_id: int
    service_timestamp_ms: int
    authenticator: bytes


@dataclass(frozen=True)
class ImpressionUpdateRequest:
    result: int
    session_id: int
    service_timestamp_ms: int
    authenticator: bytes
    impression_records: tuple[bytes, ...]
    interaction_records: tuple[bytes, ...]


@dataclass(frozen=True)
class MADAssetCatalogEntry:
    asset_url: str
    asset_body: bytes
    placement_names: tuple[str, ...]
    asset_id: int
    placement_id: int
    asset_type: int = 0
    placement_type: int = 0


def _decode_message(body: bytes, expected_type: int) -> memoryview:
    if len(body) < 6:
        raise MADProtocolError("Massive body is shorter than its six-byte envelope")
    if body[0] != MAD_ENVELOPE:
        raise MADProtocolError(f"unexpected Massive envelope 0x{body[0]:02x}")
    if body[1] != expected_type:
        raise MADProtocolError(
            f"unexpected Massive message 0x{body[1]:02x}, expected 0x{expected_type:02x}"
        )
    declared_size = int.from_bytes(body[2:6], "big")
    payload = memoryview(body)[6:]
    if declared_size != len(payload):
        raise MADProtocolError(
            f"Massive payload length mismatch: declared={declared_size} actual={len(payload)}"
        )
    return payload


def _read_string_field(payload: memoryview, offset: int) -> tuple[str, int]:
    if offset + 2 > len(payload):
        raise MADProtocolError("truncated Massive string length")
    size = int.from_bytes(payload[offset : offset + 2], "big")
    offset += 2
    end = offset + size
    if end > len(payload):
        raise MADProtocolError("truncated Massive string")
    try:
        value = bytes(payload[offset:end]).decode("ascii")
    except UnicodeDecodeError as exc:
        raise MADProtocolError("Massive string is not ASCII") from exc
    return value, end


def _read_bytes_field(payload: memoryview, offset: int) -> tuple[bytes, int]:
    if offset + 2 > len(payload):
        raise MADProtocolError("truncated Massive byte-string length")
    size = int.from_bytes(payload[offset : offset + 2], "big")
    offset += 2
    end = offset + size
    if end > len(payload):
        raise MADProtocolError("truncated Massive byte string")
    return bytes(payload[offset:end]), end


def parse_locate_service_request(body: bytes) -> LocateServiceRequest:
    payload = _decode_message(body, LOCATE_SERVICE_REQUEST)
    offset = 0
    product: str | None = None
    version: str | None = None
    while offset < len(payload):
        tag = int(payload[offset])
        offset += 1
        if tag == 0x3D:
            product, offset = _read_string_field(payload, offset)
        elif tag == 0x3E:
            version, offset = _read_string_field(payload, offset)
        else:
            raise MADProtocolError(f"unsupported LocateService field 0x{tag:02x}")
    if not product:
        raise MADProtocolError("LocateService request has no product")
    if version is None:
        raise MADProtocolError("LocateService request has no version")
    return LocateServiceRequest(product=product, version=version)


def parse_open_session_request(body: bytes) -> OpenSessionRequest:
    payload = _decode_message(body, OPEN_SESSION_REQUEST)
    offset = 0
    fields: dict[int, object] = {}
    while offset < len(payload):
        tag = int(payload[offset])
        offset += 1
        if tag in fields:
            raise MADProtocolError(f"duplicate OpenSession field 0x{tag:02x}")
        if tag in {0x3D, 0x3E, 0x41, 0x42}:
            value, offset = _read_string_field(payload, offset)
        elif tag in {0x1D, 0x1E, 0x45}:
            value, offset = _read_bytes_field(payload, offset)
        elif tag == 0x3C:
            if offset >= len(payload):
                raise MADProtocolError("truncated OpenSession protocol field")
            value = int(payload[offset])
            offset += 1
        elif tag == 0x3B:
            end = offset + 8
            if end > len(payload):
                raise MADProtocolError("truncated OpenSession timestamp")
            value = int.from_bytes(payload[offset:end], "big")
            offset = end
        else:
            raise MADProtocolError(f"unsupported OpenSession field 0x{tag:02x}")
        fields[tag] = value

    required = {0x1D, 0x1E, 0x3B, 0x3C, 0x3D, 0x3E, 0x41, 0x42, 0x45}
    missing = sorted(required.difference(fields))
    if missing:
        rendered = ",".join(f"0x{tag:02x}" for tag in missing)
        raise MADProtocolError(f"OpenSession request is missing fields {rendered}")
    return OpenSessionRequest(
        product=str(fields[0x3D]),
        version=str(fields[0x3E]),
        protocol=int(fields[0x3C]),
        client_id=str(fields[0x41]),
        platform=str(fields[0x42]),
        key_exchange=bytes(fields[0x45]),
        identity=bytes(fields[0x1D]),
        service_timestamp_ms=int(fields[0x3B]),
        authenticator=bytes(fields[0x1E]),
    )


def _parse_zone_request(body: bytes, expected_type: int) -> EnterZoneRequest:
    payload = _decode_message(body, expected_type)
    offset = 0
    fields: dict[int, object] = {}
    while offset < len(payload):
        tag = int(payload[offset])
        offset += 1
        if tag in fields:
            raise MADProtocolError(f"duplicate EnterZone field 0x{tag:02x}")
        if tag == 0x47:
            value, offset = _read_string_field(payload, offset)
        elif tag in {0x1E}:
            value, offset = _read_bytes_field(payload, offset)
        elif tag in {0x10, 0x11, 0x2A, 0x2B}:
            end = offset + 4
            if end > len(payload):
                raise MADProtocolError(f"truncated EnterZone field 0x{tag:02x}")
            value = int.from_bytes(payload[offset:end], "big")
            offset = end
        elif tag == 0x12:
            end = offset + 2
            if end > len(payload):
                raise MADProtocolError("truncated EnterZone port")
            value = int.from_bytes(payload[offset:end], "big")
            offset = end
        elif tag == 0x3B:
            end = offset + 8
            if end > len(payload):
                raise MADProtocolError("truncated EnterZone timestamp")
            value = int.from_bytes(payload[offset:end], "big")
            offset = end
        else:
            raise MADProtocolError(f"unsupported EnterZone field 0x{tag:02x}")
        fields[tag] = value

    required = {0x1E, 0x2A, 0x2B, 0x3B, 0x47}
    missing = sorted(required.difference(fields))
    if missing:
        rendered = ",".join(f"0x{tag:02x}" for tag in missing)
        raise MADProtocolError(f"EnterZone request is missing fields {rendered}")
    return EnterZoneRequest(
        zone=str(fields[0x47]),
        result=int(fields[0x2A]),
        session_id=int(fields[0x2B]),
        service_timestamp_ms=int(fields[0x3B]),
        authenticator=bytes(fields[0x1E]),
        public_address=int(fields[0x10]) if 0x10 in fields else None,
        private_address=int(fields[0x11]) if 0x11 in fields else None,
        port=int(fields[0x12]) if 0x12 in fields else None,
    )


def parse_enter_zone_request(body: bytes) -> EnterZoneRequest:
    return _parse_zone_request(body, ENTER_ZONE_REQUEST)


def parse_exit_zone_request(body: bytes) -> EnterZoneRequest:
    return _parse_zone_request(body, EXIT_ZONE_REQUEST)


def _parse_session_fields(
    body: bytes,
    expected_type: int,
    *,
    allow_records: bool,
) -> tuple[dict[int, object], tuple[bytes, ...], tuple[bytes, ...]]:
    payload = _decode_message(body, expected_type)
    offset = 0
    fields: dict[int, object] = {}
    impressions: list[bytes] = []
    interactions: list[bytes] = []
    while offset < len(payload):
        tag = int(payload[offset])
        offset += 1
        if tag in {IMPRESSION_RECORD, INTERACTION_RECORD}:
            if not allow_records:
                raise MADProtocolError(
                    f"unexpected nested record 0x{tag:02x} in session request"
                )
            end_of_size = offset + 4
            if end_of_size > len(payload):
                raise MADProtocolError("truncated MAD nested record length")
            size = int.from_bytes(payload[offset:end_of_size], "big")
            offset = end_of_size
            end = offset + size
            if end > len(payload):
                raise MADProtocolError("truncated MAD nested record")
            record = bytes(payload[offset:end])
            offset = end
            (impressions if tag == IMPRESSION_RECORD else interactions).append(record)
            continue
        if tag in fields:
            raise MADProtocolError(f"duplicate session field 0x{tag:02x}")
        if tag == 0x1E:
            value, offset = _read_bytes_field(payload, offset)
        elif tag in {0x2A, 0x2B}:
            end = offset + 4
            if end > len(payload):
                raise MADProtocolError(f"truncated session field 0x{tag:02x}")
            value = int.from_bytes(payload[offset:end], "big")
            offset = end
        elif tag == 0x3B:
            end = offset + 8
            if end > len(payload):
                raise MADProtocolError("truncated session timestamp")
            value = int.from_bytes(payload[offset:end], "big")
            offset = end
        else:
            raise MADProtocolError(f"unsupported session field 0x{tag:02x}")
        fields[tag] = value
    required = {0x1E, 0x2A, 0x2B, 0x3B}
    missing = sorted(required.difference(fields))
    if missing:
        rendered = ",".join(f"0x{tag:02x}" for tag in missing)
        raise MADProtocolError(f"session request is missing fields {rendered}")
    return fields, tuple(impressions), tuple(interactions)


def parse_close_session_request(body: bytes) -> CloseSessionRequest:
    fields, impressions, interactions = _parse_session_fields(
        body,
        CLOSE_SESSION_REQUEST,
        allow_records=False,
    )
    if impressions or interactions:
        raise MADProtocolError("CloseSession unexpectedly contains records")
    return CloseSessionRequest(
        result=int(fields[0x2A]),
        session_id=int(fields[0x2B]),
        service_timestamp_ms=int(fields[0x3B]),
        authenticator=bytes(fields[0x1E]),
    )


def parse_impression_update_request(body: bytes) -> ImpressionUpdateRequest:
    fields, impressions, interactions = _parse_session_fields(
        body,
        IMPRESSION_UPDATE_REQUEST,
        allow_records=True,
    )
    return ImpressionUpdateRequest(
        result=int(fields[0x2A]),
        session_id=int(fields[0x2B]),
        service_timestamp_ms=int(fields[0x3B]),
        authenticator=bytes(fields[0x1E]),
        impression_records=impressions,
        interaction_records=interactions,
    )


def _string_field(tag: int, value: str) -> bytes:
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise MADProtocolError("MAD service host must be ASCII") from exc
    if not encoded or len(encoded) > 0xFFFF:
        raise MADProtocolError("MAD service host length is invalid")
    return bytes((tag,)) + len(encoded).to_bytes(2, "big") + encoded


def _bytes_field(tag: int, value: bytes) -> bytes:
    if not value or len(value) > 0xFFFF:
        raise MADProtocolError("MAD byte field length is invalid")
    return bytes((tag,)) + len(value).to_bytes(2, "big") + value


def _uint16_field(tag: int, value: int) -> bytes:
    if not 0 <= value <= 0xFFFF:
        raise MADProtocolError(f"MAD field 0x{tag:02x} is outside uint16")
    return bytes((tag,)) + value.to_bytes(2, "big")


def _uint32_field(tag: int, value: int) -> bytes:
    if not 0 <= value <= 0xFFFF_FFFF:
        raise MADProtocolError(f"MAD field 0x{tag:02x} is outside uint32")
    return bytes((tag,)) + value.to_bytes(4, "big")


def _uint64_field(tag: int, value: int) -> bytes:
    if not 0 <= value <= 0xFFFF_FFFF_FFFF_FFFF:
        raise MADProtocolError(f"MAD field 0x{tag:02x} is outside uint64")
    return bytes((tag,)) + value.to_bytes(8, "big")


def _record(tag: int, payload: bytes) -> bytes:
    if not payload or len(payload) > 0xFFFF_FFFF:
        raise MADProtocolError("MAD nested record length is invalid")
    return bytes((tag,)) + len(payload).to_bytes(4, "big") + payload


def encode_locate_service_response(
    service_host: str,
    *,
    service_timestamp_ms: int = REFERENCE_SERVICE_TIMESTAMP_MS,
) -> bytes:
    """Return the retail-compatible 0xCA service directory response.

    Address tags 0x48, 0x2D and 0x22 represent Massive service indices 3, 4
    and 5.  Carbon obtains their port through MADGetServerPort, which the
    redirector maps to the configured MAD port.
    """

    if not 0 <= service_timestamp_ms <= 0xFFFF_FFFF_FFFF_FFFF:
        raise MADProtocolError("MAD service timestamp is outside uint64")
    payload = b"".join(
        (
            b"\x0d\x00",
            b"\x3b" + service_timestamp_ms.to_bytes(8, "big"),
            _string_field(0x22, service_host),
            _string_field(0x2D, service_host),
            b"\x3a\x00\x04",
            _string_field(0x48, service_host),
        )
    )
    return (
        bytes((MAD_ENVELOPE, LOCATE_SERVICE_RESPONSE))
        + len(payload).to_bytes(4, "big")
        + payload
    )


def encode_open_session_response(session_id: int, authenticator: bytes) -> bytes:
    """Return the retail-compatible 0xCC successful session response."""

    if not 0 < session_id <= 0xFFFF_FFFF:
        raise MADProtocolError("MAD session id is outside non-zero uint32")
    if len(authenticator) != 20:
        raise MADProtocolError("MAD session authenticator must be 20 bytes")
    payload = b"".join(
        (
            b"\x2a\x00\x00\x00\x01",
            b"\x2b" + session_id.to_bytes(4, "big"),
            b"\x1e\x00\x14" + authenticator,
        )
    )
    return (
        bytes((MAD_ENVELOPE, OPEN_SESSION_RESPONSE))
        + len(payload).to_bytes(4, "big")
        + payload
    )


def message_authenticator(body_without_value: bytes, session_key: bytes) -> bytes:
    """Return Massive's HMAC-SHA1 over a message with the envelope removed.

    Massive signs the message type, declared payload length, fields, and final
    0x1e authenticator tag.  The two-byte authenticator length and its 20-byte
    value are excluded.
    """

    if not session_key:
        raise MADProtocolError("MAD session key is empty")
    return hmac.new(session_key, body_without_value, hashlib.sha1).digest()


def verify_message_authenticator(body: bytes, session_key: bytes) -> bool:
    """Verify a retail Massive message whose final field is tag 0x1e."""

    if len(body) < 29 or body[0] != MAD_ENVELOPE:
        return False
    if body[-23:-20] != b"\x1e\x00\x14":
        return False
    expected = message_authenticator(body[1:-22], session_key)
    return hmac.compare_digest(body[-20:], expected)


def encode_authenticated_open_session_response(
    session_id: int,
    session_key: bytes,
) -> bytes:
    """Return a successful 0xCC response signed like the retail client."""

    if not 0 < session_id <= 0xFFFF_FFFF:
        raise MADProtocolError("MAD session id is outside non-zero uint32")
    unsigned = (
        bytes((OPEN_SESSION_RESPONSE,))
        + (33).to_bytes(4, "big")
        + b"\x2a\x00\x00\x00\x01"
        + b"\x2b"
        + session_id.to_bytes(4, "big")
        + b"\x1e"
    )
    authenticator = message_authenticator(unsigned, session_key)
    return encode_open_session_response(session_id, authenticator)


def encode_empty_enter_zone_response(session_key: bytes) -> bytes:
    """Return an authenticated 0xCE response containing no ad assets."""

    payload_without_authenticator = b"\x01\x00\x00\x25\x00\x00\x1e"
    payload_size = len(payload_without_authenticator) + 2 + 20
    unsigned = (
        bytes((ENTER_ZONE_RESPONSE,))
        + payload_size.to_bytes(4, "big")
        + payload_without_authenticator
    )
    authenticator = message_authenticator(unsigned, session_key)
    payload = payload_without_authenticator + b"\x00\x14" + authenticator
    return (
        bytes((MAD_ENVELOPE, ENTER_ZONE_RESPONSE))
        + len(payload).to_bytes(4, "big")
        + payload
    )


def encode_authenticated_empty_response(
    response_type: int,
    session_key: bytes,
) -> bytes:
    """Return a response containing only Massive's final HMAC field."""

    if response_type not in {
        EXIT_ZONE_RESPONSE,
        CLOSE_SESSION_RESPONSE,
        IMPRESSION_UPDATE_RESPONSE,
    }:
        raise MADProtocolError(
            f"unsupported authenticated empty response 0x{response_type:02x}"
        )
    payload_size = 1 + 2 + 20
    unsigned = bytes((response_type,)) + payload_size.to_bytes(4, "big") + b"\x1e"
    authenticator = message_authenticator(unsigned, session_key)
    payload = b"\x1e\x00\x14" + authenticator
    return bytes((MAD_ENVELOPE, response_type)) + len(payload).to_bytes(4, "big") + payload


def encode_exit_zone_response(session_key: bytes) -> bytes:
    return encode_authenticated_empty_response(EXIT_ZONE_RESPONSE, session_key)


def encode_close_session_response(session_key: bytes) -> bytes:
    return encode_authenticated_empty_response(CLOSE_SESSION_RESPONSE, session_key)


def encode_impression_update_response(session_key: bytes) -> bytes:
    return encode_authenticated_empty_response(IMPRESSION_UPDATE_RESPONSE, session_key)


def encode_single_asset_enter_zone_response(
    session_key: bytes,
    *,
    asset_url: str,
    asset_body: bytes,
    placement_name: str,
    asset_id: int = 0xC011_0001,
    placement_id: int = 0xC011_1001,
    asset_type: int = 0,
    placement_type: int = 0,
) -> bytes:
    """Advertise one downloadable asset and one named in-world placement.

    These are the 0xDE ``CMassiveAsset`` and 0xDD ``CMassiveAdObject``
    records consumed by Carbon's retail Massive 3.2.1 client.  A non-zero
    tag 0x15 marks the asset as remotely downloadable, which makes the client
    validate URL, size and the lowercase ASCII MD5 before issuing its GET.
    """

    return encode_asset_catalog_enter_zone_response(
        session_key,
        asset_url=asset_url,
        asset_body=asset_body,
        placement_names=(placement_name,),
        asset_id=asset_id,
        placement_id=placement_id,
        asset_type=asset_type,
        placement_type=placement_type,
    )


def encode_asset_catalog_enter_zone_response(
    session_key: bytes,
    *,
    asset_url: str,
    asset_body: bytes,
    placement_names: tuple[str, ...],
    asset_id: int = 0xC011_0001,
    placement_id: int = 0xC011_1001,
    asset_type: int = 0,
    placement_type: int = 0,
) -> bytes:
    """Advertise one download for every listed in-world MAD texture slot."""

    return encode_asset_catalogs_enter_zone_response(
        session_key,
        catalogs=(
            MADAssetCatalogEntry(
                asset_url=asset_url,
                asset_body=asset_body,
                placement_names=placement_names,
                asset_id=asset_id,
                placement_id=placement_id,
                asset_type=asset_type,
                placement_type=placement_type,
            ),
        ),
    )


def encode_asset_catalogs_enter_zone_response(
    session_key: bytes,
    *,
    catalogs: tuple[MADAssetCatalogEntry, ...],
) -> bytes:
    """Advertise multiple downloadable assets and their named texture slots."""

    if not catalogs or len(catalogs) > 0xFFFF:
        raise MADProtocolError("MAD asset catalog count is invalid")

    asset_records: list[bytes] = []
    placement_records: list[bytes] = []
    asset_ids: set[int] = set()
    placement_ids: set[int] = set()
    placement_names_seen: set[str] = set()
    total_placements = 0

    for catalog in catalogs:
        if not catalog.asset_body:
            raise MADProtocolError("MAD asset body is empty")
        if len(catalog.asset_body) > 0xFFFF_FFFF:
            raise MADProtocolError("MAD asset body is larger than uint32")
        if not 0 < catalog.asset_id <= 0xFFFF_FFFF:
            raise MADProtocolError("MAD asset id is outside non-zero uint32")
        if catalog.asset_id in asset_ids:
            raise MADProtocolError("MAD asset ids contain duplicates")
        asset_ids.add(catalog.asset_id)
        if not 0 < catalog.placement_id <= 0xFFFF_FFFF:
            raise MADProtocolError("MAD placement id is outside non-zero uint32")
        if not catalog.placement_names:
            raise MADProtocolError("MAD placement count is invalid")
        if catalog.placement_id + len(catalog.placement_names) - 1 > 0xFFFF_FFFF:
            raise MADProtocolError("MAD placement id range exceeds uint32")

        asset_payload = b"".join(
            (
                _uint64_field(0x02, 1),
                _bytes_field(
                    0x04,
                    hashlib.md5(catalog.asset_body).hexdigest().encode("ascii"),
                ),
                _uint32_field(0x05, catalog.asset_id),
                _uint32_field(0x06, catalog.placement_id),
                _uint32_field(0x07, len(catalog.asset_body)),
                _uint16_field(0x0A, 100),
                _uint32_field(0x0B, catalog.asset_type),
                _string_field(0x0C, catalog.asset_url),
                _uint32_field(0x15, 1),
            )
        )
        asset_records.append(_record(ASSET_RECORD, asset_payload))

        for index, placement_name in enumerate(catalog.placement_names):
            placement_record_id = catalog.placement_id + index
            if placement_record_id in placement_ids:
                raise MADProtocolError("MAD placement id ranges overlap")
            placement_ids.add(placement_record_id)
            if placement_name in placement_names_seen:
                raise MADProtocolError("MAD placement names contain duplicates")
            placement_names_seen.add(placement_name)
            placement_payload = b"".join(
                (
                    _uint32_field(0x0B, catalog.placement_type),
                    bytes((0x24,))
                    + (1).to_bytes(2, "big")
                    + catalog.asset_id.to_bytes(4, "big"),
                    _uint32_field(0x26, placement_record_id),
                    _string_field(0x27, placement_name),
                    _uint16_field(0x28, 0),
                    b"\x2e\x00",
                )
            )
            placement_records.append(_record(AD_OBJECT_RECORD, placement_payload))
        total_placements += len(catalog.placement_names)

    if total_placements > 0xFFFF:
        raise MADProtocolError("MAD placement count is invalid")

    payload_without_authenticator = b"".join(
        (
            _uint16_field(0x01, len(catalogs)),
            _uint16_field(0x25, total_placements),
            *asset_records,
            *placement_records,
            b"\x1e",
        )
    )
    payload_size = len(payload_without_authenticator) + 2 + 20
    unsigned = (
        bytes((ENTER_ZONE_RESPONSE,))
        + payload_size.to_bytes(4, "big")
        + payload_without_authenticator
    )
    authenticator = message_authenticator(unsigned, session_key)
    payload = payload_without_authenticator + b"\x00\x14" + authenticator
    return (
        bytes((MAD_ENVELOPE, ENTER_ZONE_RESPONSE))
        + len(payload).to_bytes(4, "big")
        + payload
    )
