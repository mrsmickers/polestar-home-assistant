"""Minimal protobuf wire-format helpers.

Shared encoding/decoding utilities for gRPC clients (pccs.py and cep.py)
using manual protobuf wire-format encoding instead of compiled .proto stubs.

Wire types: 0=varint, 1=fixed64, 2=length-delimited, 5=fixed32
"""

from __future__ import annotations

import struct


class _WireInt(int):
    """Integer value retaining the protobuf wire type it was decoded from."""

    wire_type: int

    def __new__(cls, value: int, wire_type: int) -> _WireInt:
        instance = int.__new__(cls, value)
        instance.wire_type = wire_type
        return instance


# ---------------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------------


def _encode_varint(value: int) -> bytes:
    """Encode an integer as a protobuf varint."""
    pieces = []
    while value > 0x7F:
        pieces.append((value & 0x7F) | 0x80)
        value >>= 7
    pieces.append(value & 0x7F)
    return bytes(pieces)


def _encode_field_varint(field_number: int, value: int) -> bytes:
    """Encode a varint field (tag + value)."""
    tag = (field_number << 3) | 0  # wire type 0
    return _encode_varint(tag) + _encode_varint(value)


def _encode_field_bytes(field_number: int, data: bytes) -> bytes:
    """Encode a length-delimited field (tag + length + data)."""
    tag = (field_number << 3) | 2  # wire type 2
    return _encode_varint(tag) + _encode_varint(len(data)) + data


def _encode_field_fixed32(field_number: int, value: float) -> bytes:
    """Encode a float field as fixed32 (tag + 4 bytes, IEEE 754 single-precision)."""
    tag = (field_number << 3) | 5  # wire type 5
    return _encode_varint(tag) + struct.pack("<f", value)


# ---------------------------------------------------------------------------
# Decoding
# ---------------------------------------------------------------------------


def _decode_varint(data: bytes, pos: int) -> tuple[int, int]:
    """Decode a varint starting at pos, return (value, new_pos)."""
    result = 0
    shift = 0
    while pos < len(data):
        b = data[pos]
        result |= (b & 0x7F) << shift
        pos += 1
        if (b & 0x80) == 0:
            return result, pos
        shift += 7
    raise ValueError("Truncated varint")


def _decode_message(data: bytes) -> dict[int, list]:
    """Decode a protobuf message into {field_number: [values]}.

    Returns raw values: ints for varint fields, bytes for length-delimited.
    Fixed32/64 fields are also handled.
    """
    fields: dict[int, list] = {}
    pos = 0
    while pos < len(data):
        tag, pos = _decode_varint(data, pos)
        field_number = tag >> 3
        wire_type = tag & 0x07
        if field_number == 0:
            raise ValueError("Invalid protobuf field number 0")

        if wire_type == 0:  # varint
            decoded, pos = _decode_varint(data, pos)
            value = _WireInt(decoded, wire_type)
        elif wire_type == 2:  # length-delimited
            length, pos = _decode_varint(data, pos)
            if length > len(data) - pos:
                raise ValueError("Truncated length-delimited field")
            value = data[pos : pos + length]
            pos += length
        elif wire_type == 5:  # fixed32
            if len(data) - pos < 4:
                raise ValueError("Truncated fixed32 field")
            value = _WireInt(struct.unpack_from("<I", data, pos)[0], wire_type)
            pos += 4
        elif wire_type == 1:  # fixed64
            if len(data) - pos < 8:
                raise ValueError("Truncated fixed64 field")
            value = _WireInt(struct.unpack_from("<Q", data, pos)[0], wire_type)
            pos += 8
        else:
            raise ValueError(f"Unsupported wire type {wire_type}")

        fields.setdefault(field_number, []).append(value)

    return fields


# ---------------------------------------------------------------------------
# Field extraction helpers
# ---------------------------------------------------------------------------


def _get_optional_int(fields: dict[int, list], field_number: int) -> int | None:
    """Extract the last varint occurrence, or ``None`` if no typed value exists."""
    for value in reversed(fields.get(field_number, [])):
        if isinstance(value, _WireInt) and value.wire_type == 0:
            return int(value)
        if isinstance(value, int) and not isinstance(value, _WireInt):
            # Preserve compatibility with hand-built field dictionaries.
            return value
    return None


def _get_int(fields: dict[int, list], field_number: int, default: int = 0) -> int:
    """Extract the last varint occurrence, ignoring wrong wire types."""
    value = _get_optional_int(fields, field_number)
    return default if value is None else value


def _get_bool(fields: dict[int, list], field_number: int) -> bool:
    """Extract a boolean value from decoded fields."""
    return bool(_get_int(fields, field_number, 0))


def _get_submessage(fields: dict[int, list], field_number: int) -> dict[int, list] | None:
    """Extract a singular sub-message using protobuf merge semantics.

    Repeated occurrences of a singular embedded message are merged in wire
    order. Scalar helpers then select the last value for each merged field.
    """
    vals = [
        bytes(value)
        for value in fields.get(field_number, [])
        if isinstance(value, (bytes, bytearray))
    ]
    if not vals:
        return None

    merged: dict[int, list] = {}
    for raw in vals:
        for nested_field, nested_values in _decode_message(raw).items():
            merged.setdefault(nested_field, []).extend(nested_values)
    return merged


def _get_string(fields: dict[int, list], field_number: int, default: str = "") -> str:
    """Extract the last length-delimited UTF-8 string occurrence."""
    for value in reversed(fields.get(field_number, [])):
        if isinstance(value, (bytes, bytearray)):
            return bytes(value).decode("utf-8", errors="replace")
    return default


def _get_float(fields: dict[int, list], field_number: int) -> float | None:
    """Extract the last correctly typed IEEE 754 fixed32 occurrence."""
    for raw in reversed(fields.get(field_number, [])):
        if isinstance(raw, _WireInt) and raw.wire_type == 5:
            return struct.unpack("<f", struct.pack("<I", int(raw)))[0]
        if isinstance(raw, int) and not isinstance(raw, _WireInt):
            # Preserve compatibility with hand-built field dictionaries.
            return struct.unpack("<f", struct.pack("<I", raw))[0]
    return None


def _get_double(fields: dict[int, list], field_number: int) -> float | None:
    """Extract the last correctly typed IEEE 754 fixed64 occurrence."""
    for raw in reversed(fields.get(field_number, [])):
        if isinstance(raw, _WireInt) and raw.wire_type == 1:
            return struct.unpack("<d", struct.pack("<Q", int(raw)))[0]
        if isinstance(raw, int) and not isinstance(raw, _WireInt):
            # Preserve compatibility with hand-built field dictionaries.
            return struct.unpack("<d", struct.pack("<Q", raw))[0]
    return None


def _decode_packed_varints(data: bytes) -> list[int]:
    """Decode a packed repeated varint field into a list of integers."""
    values: list[int] = []
    pos = 0
    while pos < len(data):
        value, pos = _decode_varint(data, pos)
        values.append(value)
    return values


def _encode_packed_varints(field_number: int, values: list[int]) -> bytes:
    """Encode a list of integers as a packed repeated varint field."""
    if not values:
        return b""
    packed = b""
    for v in values:
        packed += _encode_varint(v)
    return _encode_field_bytes(field_number, packed)


# ---------------------------------------------------------------------------
# Shared response parsers
# ---------------------------------------------------------------------------


def _parse_invocation_response(data: bytes) -> dict:
    """Parse an InvocationResponse from a command response wrapper.

    Used by both PCCS and CEP InvocationService commands.
    The wrapper message (e.g. ClimatizationResponse, WindowControlResponse)
    has field 1 = InvocationResponse sub-message.

    InvocationResponse:
        field 1: id (string)
        field 2: vin (string)
        field 3: status (varint enum)
        field 4: message (string)
        field 5: timestamp (int64)
    """
    empty = {"id": "", "vin": "", "status": 0, "message": ""}
    if not data:
        return empty

    outer = _decode_message(data)
    inner = _get_submessage(outer, 1)
    if inner is None:
        return empty

    return {
        "id": _get_string(inner, 1),
        "vin": _get_string(inner, 2),
        "status": _get_int(inner, 3, 0),
        "message": _get_string(inner, 4),
    }


# ---------------------------------------------------------------------------
# Raw serializer/deserializer for grpc channel methods
# ---------------------------------------------------------------------------


def _identity_serialize(data: bytes) -> bytes:
    return data


def _identity_deserialize(data: bytes) -> bytes:
    return data
