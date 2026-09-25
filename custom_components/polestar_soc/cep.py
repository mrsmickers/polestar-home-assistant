"""CEP (Volvo Connected Experience Platform) gRPC client.

Communicates with the Volvo CEP gRPC API at cepmobtoken.eu.prod.c3.volvocars.com
for reading vehicle state data (climate status, battery, etc.) and sending
vehicle commands (window control) via the InvocationService.

Uses the web OAuth access token for reads and the PCCS 2FA token for writes.
"""

from __future__ import annotations

import datetime
import logging
import math
import re
import uuid

import grpc
from homeassistant.exceptions import HomeAssistantError

from .const import (
    _INVOCATION_INTERMEDIATE_STATUSES,
    CEP_API_HOST,
    CLIMATE_RUNNING_STATUS_MAP,
    HEATING_INTENSITY_MAP,
    INVOCATION_STATUS_MAP,
    VIN_PATTERN,
)
from .proto import (
    _decode_message,
    _encode_field_bytes,
    _encode_field_varint,
    _get_double,
    _get_float,
    _get_int,
    _get_optional_int,
    _get_submessage,
    _identity_deserialize,
    _identity_serialize,
    _parse_invocation_response,
)

_LOGGER = logging.getLogger(__name__)

_MAX_TRACKER_TIMESTAMP_MS = 253_402_300_799_999

# gRPC service method paths
_METHOD_GET_CLIMATE = (
    "/services.vehiclestates.parkingclimatization"
    ".ParkingClimatizationService/GetLatestParkingClimatization"
)
_METHOD_GET_BATTERY = "/services.vehiclestates.battery.BatteryService/GetLatestBattery"
_METHOD_GET_EXTERIOR = "/services.vehiclestates.exterior.ExteriorService/GetLatestExterior"
_METHOD_GET_AVAILABILITY = (
    "/services.vehiclestates.availability.AvailabilityService/GetLatestAvailability"
)
_METHOD_GET_HEALTH = "/services.vehiclestates.health.HealthService/GetHealth"
_METHOD_GET_LOCATION = "/dtlinternet.DtlInternetService/GetLastKnownLocation"
_METHOD_GET_PARKED_LOCATION = "/dtlinternet.DtlInternetService/GetLastParkedLocation"
_METHOD_GET_MYCARS = "/car_information.CarInformation/GetMyCars"
_SVC_CHARGE_NOW = "/chronos.services.v1.ChargeNowService"
_METHOD_CHARGE_NOW_START = f"{_SVC_CHARGE_NOW}/StartOverrideChargeTimer"
_METHOD_CHARGE_NOW_STOP = f"{_SVC_CHARGE_NOW}/StopOverrideChargeTimer"
_SVC_CHARGE_LOCATION = "/chronos.services.v1.ChargeLocationService"
_METHOD_GET_CHARGE_LOCATIONS = f"{_SVC_CHARGE_LOCATION}/GetChargeLocations"
_METHOD_GET_CURRENT_CHARGE_LOCATION = f"{_SVC_CHARGE_LOCATION}/isAtALocation"
_SVC_INVOCATION = "/invocation.InvocationService"
_METHOD_WINDOW_CONTROL = f"{_SVC_INVOCATION}/WindowControl"
_METHOD_HONK_FLASH = f"{_SVC_INVOCATION}/HonkFlash"
_METHOD_CEP_UNLOCK = f"{_SVC_INVOCATION}/Unlock"

# BatteryState field numbers captured in raw_fields for debugging.
_RAW_BATTERY_FIELD_NUMBERS = (5, 7, 8, 10, 17, 26, 28)
_SOFTWARE_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+() /-]{0,63}$")
_CHARGE_COMMAND_ACCEPTED = frozenset({1})


class CepDataError(HomeAssistantError):
    """A CEP response could not be attributed or validated safely."""


def _grpc_status_name(err: grpc.RpcError) -> str:
    """Return an allowlisted gRPC status name without backend details."""
    try:
        code = err.code()
    except Exception:
        return "RPC_ERROR"
    return code.name if isinstance(code, grpc.StatusCode) else "RPC_ERROR"


def _require_valid_vin(vin: str, *, source: str) -> None:
    """Reject missing or malformed VINs before identity-sensitive work."""
    if not isinstance(vin, str) or VIN_PATTERN.fullmatch(vin) is None:
        raise CepDataError(f"GetMyCars received an invalid {source} VIN")


def _get_strict_utf8(
    fields: dict[int, list],
    field_number: int,
    label: str,
    *,
    source: str = "GetMyCars",
) -> str:
    """Decode the last value of a singular string field as strict UTF-8."""
    values = fields.get(field_number)
    if not values:
        return ""
    raw = values[-1]
    if not isinstance(raw, (bytes, bytearray)):
        raise CepDataError(f"{source} {label} field has the wrong wire type")
    try:
        return bytes(raw).decode("utf-8", errors="strict")
    except UnicodeDecodeError as err:
        raise CepDataError(f"{source} {label} field contains invalid UTF-8") from err


def _validate_response_vin(
    fields: dict[int, list],
    field_number: int,
    requested_vin: str,
    *,
    source: str,
) -> None:
    """Require a valid response VIN matching the requested vehicle."""
    _require_valid_vin(requested_vin, source="requested")
    response_vin = _get_strict_utf8(fields, field_number, "VIN", source=source)
    if VIN_PATTERN.fullmatch(response_vin) is None or response_vin != requested_vin:
        raise CepDataError(f"{source} returned a missing or mismatched VIN")


# ---------------------------------------------------------------------------
# Request builders
# ---------------------------------------------------------------------------


def _build_vin_request(vin: str) -> bytes:
    """Build a request with VIN as field 2 (string)."""
    return _encode_field_bytes(2, vin.encode("utf-8"))


def _build_location_request(vin: str) -> bytes:
    """Build a request with VIN as field 1 (DtlInternetService uses field 1, not field 2)."""
    return _encode_field_bytes(1, vin.encode("utf-8"))


def _build_get_mycars_request(vin: str) -> bytes:
    """Build a GetMyCars request with request ID and VIN."""
    _require_valid_vin(vin, source="requested")
    return _encode_field_bytes(1, str(uuid.uuid4()).encode("utf-8")) + _encode_field_bytes(
        2, vin.encode("utf-8")
    )


def _build_charge_now_request(vin: str) -> bytes:
    """Build a C3 Chronos request for charge-override commands."""
    _require_valid_vin(vin, source="requested")
    request = b""
    request += _encode_field_bytes(1, str(uuid.uuid4()).encode("utf-8"))
    request += _encode_field_bytes(2, vin.encode("utf-8"))
    request += _encode_field_bytes(3, b"RCS")
    utc_offset = datetime.datetime.now(datetime.UTC).astimezone().utcoffset()
    offset_minutes = int((utc_offset or datetime.timedelta()).total_seconds()) // 60
    encoded_offset = offset_minutes if offset_minutes >= 0 else offset_minutes + (1 << 64)
    request += _encode_field_bytes(4, _encode_field_varint(1, encoded_offset))
    return _encode_field_bytes(1, request)


def _build_cep_invocation_request(vin: str) -> bytes:
    """Build a CEP InvocationRequest sub-message.

    CEP InvocationRequest (invocation.InvocationRequest):
        field 1: vin (string)

    This is simpler than the PCCS InvocationRequest which also has
    id (UUID) and expirationTimestamp fields.
    """
    return _encode_field_bytes(1, vin.encode("utf-8"))


def _build_window_control_request(vin: str, control_type: int) -> bytes:
    """Build WindowControlRequest bytes for CEP.

    WindowControlRequest:
        field 1: InvocationRequest (message) — CEP format (VIN only)
        field 2: windowsControl (WindowControlType enum)
            0 = WINDOW_CONTROL_TYPE_UNSPECIFIED
            1 = OPEN_ALL
            2 = CLOSE_ALL
    """
    msg = _encode_field_bytes(1, _build_cep_invocation_request(vin))
    msg += _encode_field_varint(2, control_type)
    return msg


def _build_honk_flash_request(vin: str, action: int) -> bytes:
    """Build HonkAndFlashRequest for action 0=both, 1=honk, 2=flash."""
    _require_valid_vin(vin, source="requested")
    if action not in {0, 1, 2}:
        raise ValueError("Invalid honk/flash action")
    return _encode_field_bytes(
        1, _build_cep_invocation_request(vin)
    ) + _encode_field_varint(2, action)


def _build_trunk_unlock_request(vin: str) -> bytes:
    """Build CarUnlockRequest with trunk-only unlock type."""
    _require_valid_vin(vin, source="requested")
    return _encode_field_bytes(
        1, _build_cep_invocation_request(vin)
    ) + _encode_field_varint(2, 1)


# ---------------------------------------------------------------------------
# Response parsers
# ---------------------------------------------------------------------------


def _parse_mycars_response(data: bytes, vin: str) -> dict:
    """Parse GetMyCars and return the entry matching ``vin``.

    GetMyCarsResponse field 1 is a repeated MyCarEntry. Each entry's field
    1 contains car details, where fields 1/6/7/9/10 are VIN, model name,
    model year, installed software version, and market respectively.
    """
    _require_valid_vin(vin, source="requested")
    if not data:
        raise CepDataError("GetMyCars returned an empty response")

    entries: list[dict] = []
    try:
        raw_entries = _decode_message(data).get(1, [])
        for raw_entry in raw_entries:
            if not isinstance(raw_entry, (bytes, bytearray)):
                raise ValueError("GetMyCars entry has the wrong wire type")
            details = _get_submessage(_decode_message(bytes(raw_entry)), 1)
            if details is None:
                raise ValueError("GetMyCars entry omitted car details")
            entry = {
                "vin": _get_strict_utf8(details, 1, "VIN"),
                "model_name": _get_strict_utf8(details, 6, "model name"),
                "model_year": _get_strict_utf8(details, 7, "model year"),
                "installed_software_version": _get_strict_utf8(
                    details, 9, "software version"
                ),
                "market": _get_strict_utf8(details, 10, "market"),
            }
            if entry["vin"] and VIN_PATTERN.fullmatch(entry["vin"]) is None:
                raise ValueError("GetMyCars entry contains an invalid VIN")
            entries.append(entry)
    except ValueError as err:
        raise CepDataError("GetMyCars returned malformed protobuf data") from err

    matches = [entry for entry in entries if entry["vin"] == vin]
    if not matches:
        raise CepDataError(
            f"GetMyCars returned {len(entries)} entries but none matched the requested VIN"
        )
    if len(matches) != 1:
        raise CepDataError("GetMyCars returned duplicate entries for the requested VIN")
    matching = matches[0]
    version = matching["installed_software_version"]
    if not version:
        raise CepDataError("GetMyCars matched the requested VIN but omitted software version")
    if _SOFTWARE_VERSION_PATTERN.fullmatch(version) is None or not any(
        "0" <= char <= "9" for char in version
    ):
        raise CepDataError("GetMyCars returned an invalid installed software version")
    return matching


def _parse_charge_now_response(data: bytes) -> int:
    """Parse the direct ResponseStatus field from a charge-now response."""
    status = _get_optional_int(_decode_message(data), 1) if data else None
    if status is None:
        raise CepDataError("Charge-now response is missing status")
    return status


def _parse_charge_locations_response(data: bytes, vin: str) -> list[dict]:
    """Parse saved charge locations without exposing precise coordinates."""
    response = _decode_message(data)
    _validate_response_vin(response, 2, vin, source="Charge locations")
    locations: list[dict] = []
    for raw_location in response.get(3, []):
        if not isinstance(raw_location, (bytes, bytearray)):
            raise CepDataError("Charge locations entry has the wrong wire type")
        fields = _decode_message(bytes(raw_location))
        location_id = _get_strict_utf8(
            fields, 2, "location ID", source="Charge locations"
        )
        if not location_id:
            raise CepDataError("Charge locations entry omitted location ID")
        locations.append(
            {
                "location_id": location_id,
                "alias": _get_strict_utf8(
                    fields, 3, "alias", source="Charge locations"
                ),
                "amp_limit": _get_optional_int(fields, 5),
                "minimum_soc": _get_optional_int(fields, 6),
                "optimised_charging": bool(_get_optional_int(fields, 7)),
                "bidirectional_charging": bool(_get_optional_int(fields, 8)),
                "available_optimised_charging": _get_optional_int(fields, 9),
                "location_type": _get_optional_int(fields, 12),
            }
        )
    return locations


def _parse_current_charge_location_response(data: bytes) -> dict:
    """Parse whether the vehicle is at a saved charge location."""
    fields = _decode_message(data)
    status = _get_optional_int(fields, 1)
    if status is None:
        raise CepDataError("Current charge location response is missing status")
    if status != 1:
        raise CepDataError(f"Current charge location failed with status {status}")
    return {
        "status": status,
        "location_id": _get_strict_utf8(
            fields, 2, "location ID", source="Current charge location"
        ),
        "arrived_at": _get_optional_int(fields, 3),
    }


def _format_climate_status(value: int) -> str:
    """Map climate running status enum to string."""
    mapped = CLIMATE_RUNNING_STATUS_MAP.get(value)
    if mapped is not None:
        return mapped
    _LOGGER.debug("Unknown climate running status: %d", value)
    return f"Unknown ({value})"


def _format_heating_intensity(value: int) -> str:
    """Map heating intensity enum to string."""
    mapped = HEATING_INTENSITY_MAP.get(value)
    if mapped is not None:
        return mapped
    _LOGGER.debug("Unknown heating intensity: %d", value)
    return f"Unknown ({value})"


def _parse_climate_response(data: bytes) -> dict:
    """Parse GetLatestParkingClimatization response.

    Two-level decode: outer envelope has field 3 = state sub-message.
    """
    empty = {
        "status": None,
        "driver_seat_heating": None,
        "passenger_seat_heating": None,
        "rear_left_seat_heating": None,
        "rear_right_seat_heating": None,
        "steering_wheel_heating": None,
    }
    if not data:
        return empty

    outer = _decode_message(data)
    state = _get_submessage(outer, 3)
    if state is None:
        return empty

    return {
        "status": _format_climate_status(_get_int(state, 2, 0)),
        "driver_seat_heating": _format_heating_intensity(_get_int(state, 9, 0)),
        "passenger_seat_heating": _format_heating_intensity(_get_int(state, 10, 0)),
        "rear_left_seat_heating": _format_heating_intensity(_get_int(state, 11, 0)),
        "rear_right_seat_heating": _format_heating_intensity(_get_int(state, 12, 0)),
        "steering_wheel_heating": _format_heating_intensity(_get_int(state, 13, 0)),
    }


def _parse_battery_response(data: bytes) -> dict:
    """Parse GetLatestBattery response.

    Two-level decode: outer envelope has field 3 = battery state sub-message.

    Field mapping (BatteryState proto):
        field 2:  battery_charge_level_percentage (double)
        field 3:  average_energy_consumption_kwh_per_100_km (double)
        field 4:  estimated_distance_to_empty_km (varint)
        field 5:  estimated_charging_time_to_full_minutes (varint)
        field 6:  charger_connection_status (enum: 1=CONNECTED, 2=DISCONNECTED, 3=FAULT)
        field 7:  charging_status (enum: 1=CHARGING, 2=IDLE, 3=SCHEDULED, ...)
        field 8:  estimated_distance_to_empty_miles (varint)
        field 10: charging_power_watts (varint)
        field 17: charging_type (enum)
        field 26: charger_power_status (enum)
        field 28: unknown (CEP-specific?)
    """
    empty = {
        "soc": None,
        "estimated_range_km": None,
        "charger_connection_status": None,
        "charging_status": None,
        "avg_energy_consumption_kwh_per_100km": None,
        "estimated_charging_time_minutes": None,
        "estimated_range_miles": None,
        "charging_power_watts": None,
        "charging_type": None,
        "raw_fields": {},
    }
    if not data:
        return empty

    outer = _decode_message(data)
    state = _get_submessage(outer, 3)
    if state is None:
        return empty

    raw_fields = {}
    for fn in _RAW_BATTERY_FIELD_NUMBERS:
        value = _get_optional_int(state, fn)
        if value is not None:
            raw_fields[fn] = value

    def _int_or_none(field_num: int) -> int | None:
        return _get_optional_int(state, field_num)

    return {
        "soc": _get_double(state, 2),
        "estimated_range_km": _int_or_none(4),
        "charger_connection_status": _get_int(state, 6) or None,
        "charging_status": _int_or_none(7),
        "avg_energy_consumption_kwh_per_100km": _get_double(state, 3),
        "estimated_charging_time_minutes": _int_or_none(5),
        "estimated_range_miles": _int_or_none(8),
        "charging_power_watts": _int_or_none(10),
        "charging_type": _int_or_none(17),
        "raw_fields": raw_fields,
    }


# ExteriorState field numbers → dict keys
_EXTERIOR_FIELDS: tuple[tuple[int, str], ...] = (
    (2, "central_lock"),
    (3, "front_left_door"),
    (4, "front_right_door"),
    (5, "rear_left_door"),
    (6, "rear_right_door"),
    (7, "front_left_window"),
    (8, "front_right_window"),
    (9, "rear_left_window"),
    (10, "rear_right_window"),
    (11, "hood"),
    (12, "tailgate"),
    (13, "tank_lid"),
    (14, "sunroof"),
    (15, "alarm"),
)


def _parse_exterior_response(data: bytes) -> dict:
    """Parse GetLatestExterior response.

    Two-level decode: outer envelope has field 3 = ExteriorState sub-message.
    Returns raw integer enum values (0-3) for each field, or None if missing.
    """
    empty: dict = {key: None for _, key in _EXTERIOR_FIELDS}
    if not data:
        return empty

    outer = _decode_message(data)
    state = _get_submessage(outer, 3)
    if state is None:
        return empty

    result: dict = {}
    for field_num, key in _EXTERIOR_FIELDS:
        val = _get_int(state, field_num)
        result[key] = val if val else None
    return result


def _parse_availability_response(data: bytes) -> dict:
    """Parse GetLatestAvailability response.

    Two-level decode: outer envelope has field 3 = Availability state sub-message.

    Availability state fields:
        field 3: availability_status (varint: 1=AVAILABLE, 2=UNAVAILABLE)
        field 4: unavailable_reason (varint: 1=NO_INTERNET, 2=POWER_SAVING, ...)
        field 5: usage_mode (varint: 1=ABANDONED, 2=INACTIVE, ..., 5=DRIVING)
    """
    empty = {"availability_status": None, "unavailable_reason": None, "usage_mode": None}
    if not data:
        return empty

    outer = _decode_message(data)
    state = _get_submessage(outer, 3)
    if state is None:
        return empty

    return {
        "availability_status": _get_int(state, 3) or None,
        "unavailable_reason": _get_int(state, 4) or None,
        "usage_mode": _get_int(state, 5) or None,
    }


# Health light warning field numbers → dict keys
_LIGHT_WARNING_FIELDS: tuple[tuple[int, str], ...] = (
    (14, "brake_light_left_warning"),
    (15, "brake_light_center_warning"),
    (16, "brake_light_right_warning"),
    (17, "fog_light_front_warning"),
    (18, "fog_light_rear_warning"),
    (19, "position_light_front_left_warning"),
    (20, "position_light_front_right_warning"),
    (21, "position_light_rear_left_warning"),
    (22, "position_light_rear_right_warning"),
    (23, "high_beam_left_warning"),
    (24, "high_beam_right_warning"),
    (25, "low_beam_left_warning"),
    (26, "low_beam_right_warning"),
    (27, "daytime_running_light_left_warning"),
    (28, "daytime_running_light_right_warning"),
    (30, "turn_indication_front_left_warning"),
    (31, "turn_indication_front_right_warning"),
    (32, "turn_indication_rear_left_warning"),
    (33, "turn_indication_rear_right_warning"),
    (34, "registration_plate_light_warning"),
    (35, "side_mark_lights_warning"),
)


def _parse_health_response(data: bytes) -> dict:
    """Parse GetHealth response.

    Two-level decode: outer envelope has field 3 = Health sub-message.

    Health state field mapping:
        field 3:  days_to_service (int32)
        field 4:  distance_to_service_km (int32)
        field 5:  service_warning (ServiceWarning enum)
        field 6:  brake_fluid_level_warning (enum)
        field 7:  engine_coolant_level_warning (enum)
        field 8:  oil_level_warning (enum)
        field 9:  front_left_tyre_pressure_warning (enum)
        field 10: front_right_tyre_pressure_warning (enum)
        field 11: rear_left_tyre_pressure_warning (enum)
        field 12: rear_right_tyre_pressure_warning (enum)
        field 13: washer_fluid_level_warning (enum)
        field 14-35: light warnings (ExteriorLightWarning enum)
        field 38: low_voltage_battery_warning (enum)
        field 39: front_left_tyre_pressure_kpa (float/fixed32)
        field 40: front_right_tyre_pressure_kpa (float/fixed32)
        field 41: rear_left_tyre_pressure_kpa (float/fixed32)
        field 42: rear_right_tyre_pressure_kpa (float/fixed32)
        field 43: front_tyres_reference_pressure_kpa (float/fixed32)
        field 44: rear_tyres_reference_pressure_kpa (float/fixed32)
    """
    empty: dict = {
        "days_to_service": None,
        "distance_to_service_km": None,
        "service_warning": None,
        "brake_fluid_level_warning": None,
        "engine_coolant_level_warning": None,
        "oil_level_warning": None,
        "front_left_tyre_pressure_warning": None,
        "front_right_tyre_pressure_warning": None,
        "rear_left_tyre_pressure_warning": None,
        "rear_right_tyre_pressure_warning": None,
        "washer_fluid_level_warning": None,
        "low_voltage_battery_warning": None,
        "front_left_tyre_pressure_kpa": None,
        "front_right_tyre_pressure_kpa": None,
        "rear_left_tyre_pressure_kpa": None,
        "rear_right_tyre_pressure_kpa": None,
        "front_tyres_reference_pressure_kpa": None,
        "rear_tyres_reference_pressure_kpa": None,
    }
    for _, key in _LIGHT_WARNING_FIELDS:
        empty[key] = None

    if not data:
        return empty

    outer = _decode_message(data)
    state = _get_submessage(outer, 3)
    if state is None:
        return empty

    def _int_or_none(field_num: int) -> int | None:
        return _get_int(state, field_num) if field_num in state else None

    def _pressure(field_num: int) -> float | None:
        val = _get_float(state, field_num)
        return round(val, 1) if val is not None else None

    def _warning(field_num: int) -> int | None:
        val = _get_int(state, field_num)
        return val if val else None  # 0 (UNSPECIFIED) → None

    result: dict = {
        "days_to_service": _int_or_none(3),
        "distance_to_service_km": _int_or_none(4),
        "service_warning": _warning(5),
        "brake_fluid_level_warning": _warning(6),
        "engine_coolant_level_warning": _warning(7),
        "oil_level_warning": _warning(8),
        "front_left_tyre_pressure_warning": _warning(9),
        "front_right_tyre_pressure_warning": _warning(10),
        "rear_left_tyre_pressure_warning": _warning(11),
        "rear_right_tyre_pressure_warning": _warning(12),
        "washer_fluid_level_warning": _warning(13),
        "low_voltage_battery_warning": _warning(38),
        "front_left_tyre_pressure_kpa": _pressure(39),
        "front_right_tyre_pressure_kpa": _pressure(40),
        "rear_left_tyre_pressure_kpa": _pressure(41),
        "rear_right_tyre_pressure_kpa": _pressure(42),
        "front_tyres_reference_pressure_kpa": _pressure(43),
        "rear_tyres_reference_pressure_kpa": _pressure(44),
    }
    for field_num, key in _LIGHT_WARNING_FIELDS:
        result[key] = _warning(field_num)

    return result


def _parse_location_response(data: bytes, vin: str) -> dict:
    """Parse a VIN-bound location response.

    Unlike climate/battery, location fields are at the top level (no envelope).
    Field mapping:
        field 1 (string): VIN
        field 2 (double): longitude
        field 3 (double): latitude
        field 4 (varint): timestamp_ms (milliseconds since epoch)
    """
    empty: dict = {"latitude": None, "longitude": None, "timestamp_ms": None}
    fields = _decode_message(data)
    _validate_response_vin(fields, 1, vin, source="Location")
    latitude = _get_double(fields, 3)
    longitude = _get_double(fields, 2)
    if latitude is None or longitude is None:
        return empty

    return {
        "latitude": latitude,
        "longitude": longitude,
        "timestamp_ms": _get_optional_int(fields, 4),
    }


def _parse_parked_location_response(data: bytes, vin: str) -> dict:
    """Parse the APK-native nested LastParkedLocation response."""
    fields = _decode_message(data)
    _validate_response_vin(fields, 1, vin, source="Parked location")
    location = _get_submessage(fields, 2)
    if location is None:
        raise CepDataError("Parked location response is missing location")

    longitude = _get_double(location, 1)
    latitude = _get_double(location, 2)
    timestamp = _get_submessage(location, 3)
    if longitude is None or latitude is None or timestamp is None:
        raise CepDataError("Parked location response is incomplete")
    if (
        not math.isfinite(longitude)
        or not math.isfinite(latitude)
        or not -180 <= longitude <= 180
        or not -90 <= latitude <= 90
    ):
        raise CepDataError("Parked location coordinates are invalid")

    seconds = _get_optional_int(timestamp, 1)
    nanos = _get_optional_int(timestamp, 2) or 0
    if seconds is None or not 0 <= nanos < 1_000_000_000:
        raise CepDataError("Parked location timestamp is invalid")
    timestamp_ms = seconds * 1000 + nanos // 1_000_000
    if not 0 <= timestamp_ms <= _MAX_TRACKER_TIMESTAMP_MS:
        raise CepDataError("Parked location timestamp is not representable")

    return {
        "latitude": latitude,
        "longitude": longitude,
        "timestamp_ms": timestamp_ms,
    }


# ---------------------------------------------------------------------------
# CepClient
# ---------------------------------------------------------------------------


class CepError(HomeAssistantError):
    """Error returned by a CEP InvocationService command."""


class CepClient:
    """Client for the Volvo CEP gRPC API.

    Uses ``access_token`` for read operations (web client token) and
    ``write_access_token`` for command operations (PCCS 2FA token).
    """

    def __init__(self, access_token: str, write_access_token: str | None = None) -> None:
        self._access_token = access_token
        self._write_access_token = write_access_token
        self._channel: grpc.Channel | None = None

    @property
    def access_token(self) -> str:
        return self._access_token

    @access_token.setter
    def access_token(self, value: str) -> None:
        self._access_token = value

    @property
    def write_access_token(self) -> str | None:
        return self._write_access_token

    @write_access_token.setter
    def write_access_token(self, value: str | None) -> None:
        self._write_access_token = value

    def _get_channel(self) -> grpc.Channel:
        if self._channel is None:
            credentials = grpc.ssl_channel_credentials()
            self._channel = grpc.secure_channel(f"{CEP_API_HOST}:443", credentials)
        return self._channel

    def _metadata(self, vin: str) -> list[tuple[str, str]]:
        return [
            ("authorization", f"Bearer {self._access_token}"),
            ("vin", vin),
        ]

    def _write_metadata(self, vin: str) -> list[tuple[str, str]]:
        """Build gRPC call metadata for write/command operations.

        Uses the write token (PCCS 2FA) when available, otherwise falls
        back to the regular read token.
        """
        token = self._write_access_token or self._access_token
        if not self._write_access_token:
            _LOGGER.debug("No CEP write token available, falling back to web token")
        return [
            ("authorization", f"Bearer {token}"),
            ("vin", vin),
        ]

    def close(self) -> None:
        if self._channel is not None:
            self._channel.close()
            self._channel = None

    def get_parking_climatization(self, vin: str) -> dict:
        """Get current parking climatization state."""
        channel = self._get_channel()
        method = channel.unary_unary(
            _METHOD_GET_CLIMATE,
            request_serializer=_identity_serialize,
            response_deserializer=_identity_deserialize,
        )
        try:
            response = method(_build_vin_request(vin), metadata=self._metadata(vin), timeout=30)
            return _parse_climate_response(response)
        except grpc.RpcError as err:
            _LOGGER.debug("CEP parking climatization failed: %s", _grpc_status_name(err))
            raise

    def get_mycars(self, vin: str) -> dict:
        """Get vehicle identity and currently installed software version."""
        channel = self._get_channel()
        method = channel.unary_unary(
            _METHOD_GET_MYCARS,
            request_serializer=_identity_serialize,
            response_deserializer=_identity_deserialize,
        )
        try:
            response = method(
                _build_get_mycars_request(vin),
                metadata=tuple(self._metadata(vin)),
                timeout=30,
            )
            return _parse_mycars_response(response, vin)
        except grpc.RpcError as err:
            _LOGGER.debug("CEP GetMyCars failed: %s", _grpc_status_name(err))
            raise

    def _get_charge_location_data(self, vin: str, method_path: str) -> bytes:
        """Fetch one read-only charge-location response."""
        channel = self._get_channel()
        method = channel.unary_unary(
            method_path,
            request_serializer=_identity_serialize,
            response_deserializer=_identity_deserialize,
        )
        try:
            return method(
                _build_charge_now_request(vin),
                metadata=tuple(self._metadata(vin)),
                timeout=30,
            )
        except grpc.RpcError as err:
            _LOGGER.debug("CEP charge-location call failed: %s", _grpc_status_name(err))
            raise

    def get_charge_locations(self, vin: str) -> list[dict]:
        """Get saved charge locations and their charging settings."""
        return _parse_charge_locations_response(
            self._get_charge_location_data(vin, _METHOD_GET_CHARGE_LOCATIONS), vin
        )

    def get_current_charge_location(self, vin: str) -> dict:
        """Get the saved charge location currently containing the vehicle."""
        return _parse_current_charge_location_response(
            self._get_charge_location_data(vin, _METHOD_GET_CURRENT_CHARGE_LOCATION)
        )

    def _set_charge_override(self, vin: str, method_path: str) -> dict:
        """Start or stop the current charging-schedule override."""
        channel = self._get_channel()
        method = channel.unary_unary(
            method_path,
            request_serializer=_identity_serialize,
            response_deserializer=_identity_deserialize,
        )
        try:
            response = method(
                _build_charge_now_request(vin),
                metadata=tuple(self._write_metadata(vin)),
                timeout=30,
            )
        except grpc.RpcError as err:
            _LOGGER.debug("CEP charge override call failed: %s", _grpc_status_name(err))
            raise

        status = _parse_charge_now_response(response)
        if status not in _CHARGE_COMMAND_ACCEPTED:
            raise CepError(f"Charge command failed with status {status}")
        return {"status": status}

    def start_charging(self, vin: str) -> dict:
        """Start charging by overriding the configured charging timer."""
        return self._set_charge_override(vin, _METHOD_CHARGE_NOW_START)

    def stop_charging(self, vin: str) -> dict:
        """Stop the current charging-timer override."""
        return self._set_charge_override(vin, _METHOD_CHARGE_NOW_STOP)

    def get_battery(self, vin: str) -> dict:
        """Get current battery state."""
        channel = self._get_channel()
        method = channel.unary_unary(
            _METHOD_GET_BATTERY,
            request_serializer=_identity_serialize,
            response_deserializer=_identity_deserialize,
        )
        try:
            response = method(_build_vin_request(vin), metadata=self._metadata(vin), timeout=30)
            return _parse_battery_response(response)
        except grpc.RpcError as err:
            _LOGGER.debug("CEP battery call failed: %s", _grpc_status_name(err))
            raise

    def get_exterior(self, vin: str) -> dict:
        """Get current exterior state (lock, doors, windows, etc.)."""
        channel = self._get_channel()
        method = channel.unary_unary(
            _METHOD_GET_EXTERIOR,
            request_serializer=_identity_serialize,
            response_deserializer=_identity_deserialize,
        )
        try:
            response = method(_build_vin_request(vin), metadata=self._metadata(vin), timeout=30)
            return _parse_exterior_response(response)
        except grpc.RpcError as err:
            _LOGGER.debug("CEP exterior call failed: %s", _grpc_status_name(err))
            raise

    def get_availability(self, vin: str) -> dict:
        """Get current vehicle availability state."""
        channel = self._get_channel()
        method = channel.unary_unary(
            _METHOD_GET_AVAILABILITY,
            request_serializer=_identity_serialize,
            response_deserializer=_identity_deserialize,
        )
        try:
            response = method(_build_vin_request(vin), metadata=self._metadata(vin), timeout=30)
            return _parse_availability_response(response)
        except grpc.RpcError as err:
            _LOGGER.debug("CEP availability call failed: %s", _grpc_status_name(err))
            raise

    def get_health(self, vin: str) -> dict:
        """Get vehicle health state (tyre pressure, fluid levels, service info).

        GetHealth is SERVER_STREAMING (no GetLatest* unary variant).
        Takes the first response from the stream and returns parsed data.
        """
        channel = self._get_channel()
        method = channel.unary_stream(
            _METHOD_GET_HEALTH,
            request_serializer=_identity_serialize,
            response_deserializer=_identity_deserialize,
        )
        try:
            responses = method(_build_vin_request(vin), metadata=self._metadata(vin), timeout=30)
            for response in responses:
                return _parse_health_response(response)
        except grpc.RpcError as err:
            _LOGGER.debug("CEP health call failed: %s", _grpc_status_name(err))
            raise
        # Stream yielded no responses
        return _parse_health_response(b"")

    def get_location(self, vin: str) -> dict:
        """Get last known vehicle location."""
        channel = self._get_channel()
        method = channel.unary_unary(
            _METHOD_GET_LOCATION,
            request_serializer=_identity_serialize,
            response_deserializer=_identity_deserialize,
        )
        try:
            response = method(
                _build_location_request(vin), metadata=self._metadata(vin), timeout=30
            )
            return _parse_location_response(response, vin)
        except grpc.RpcError as err:
            _LOGGER.debug("CEP last-known location failed: %s", _grpc_status_name(err))
            raise

    def get_parked_location(self, vin: str) -> dict:
        """Get the vehicle's last parked location."""
        channel = self._get_channel()
        method = channel.unary_unary(
            _METHOD_GET_PARKED_LOCATION,
            request_serializer=_identity_serialize,
            response_deserializer=_identity_deserialize,
        )
        try:
            response = method(
                _build_location_request(vin), metadata=self._metadata(vin), timeout=30
            )
            return _parse_parked_location_response(response, vin)
        except grpc.RpcError as err:
            _LOGGER.debug("CEP parked location failed: %s", _grpc_status_name(err))
            raise

    # -- Window Control (InvocationService) ---------------------------------

    def _send_invocation(
        self,
        vin: str,
        method_path: str,
        request: bytes,
        *,
        command_name: str = "Command",
        allow_delivered_on_cancel: bool = False,
        require_response_vin: bool = False,
    ) -> dict:
        """Send a CEP InvocationService command and wait for terminal status.

        CEP InvocationService methods are SERVER_STREAMING. The stream emits
        intermediate statuses (SENT, DELIVERED) before a terminal status
        (SUCCESS or an error). We iterate until we reach a terminal status.

        Security-sensitive controls require terminal SUCCESS. Legacy window
        controls may explicitly retain their historical DELIVERED behaviour.
        """
        channel = self._get_channel()
        method = channel.unary_stream(
            method_path,
            request_serializer=_identity_serialize,
            response_deserializer=_identity_deserialize,
        )
        result = _parse_invocation_response(b"")
        try:
            responses = method(request, metadata=self._write_metadata(vin), timeout=60)
            for response in responses:
                result = _parse_invocation_response(response)
                status = result.get("status", 0)
                if status not in _INVOCATION_INTERMEDIATE_STATUSES:
                    break
        except grpc.RpcError:
            if result.get("status") == 4:  # DELIVERED
                if allow_delivered_on_cancel:
                    _LOGGER.debug("CEP command stream ended after delivery")
                    return result
                raise CepError(
                    f"{command_name} delivered but execution result unavailable"
                ) from None
            _LOGGER.debug("CEP command stream failed before terminal success")
            raise

        status = result.get("status", 0)
        if status == 4 and not allow_delivered_on_cancel:
            raise CepError(
                f"{command_name} delivered but execution result unavailable"
            )
        if status != 6 and not (status == 4 and allow_delivered_on_cancel):
            status_name = INVOCATION_STATUS_MAP.get(status, f"STATUS_{status}")
            server_msg = result.get("message", "")
            msg = f"{command_name} failed: {status_name}"
            if server_msg:
                msg += f" - {server_msg}"
            raise CepError(msg)

        if status == 6 and require_response_vin:
            response_vin = result.get("vin", "")
            _require_valid_vin(response_vin, source=f"{command_name} response")
            if response_vin != vin:
                raise CepDataError(f"{command_name} response VIN does not match request")

        return result

    def honk_flash(self, vin: str, action: int) -> dict:
        """Honk, flash, or honk and flash the vehicle."""
        request = _build_honk_flash_request(vin, action)
        return self._send_invocation(
            vin,
            _METHOD_HONK_FLASH,
            request,
            command_name="Honk/flash",
            require_response_vin=True,
        )

    def unlock_trunk(self, vin: str) -> dict:
        """Unlock the vehicle trunk only."""
        request = _build_trunk_unlock_request(vin)
        return self._send_invocation(
            vin,
            _METHOD_CEP_UNLOCK,
            request,
            command_name="Trunk unlock",
            require_response_vin=True,
        )

    def window_open(self, vin: str) -> dict:
        """Open all vehicle windows.

        Requires the PCCS 2FA token (customer:attributes:write scope).
        WindowControl is only available on CEP, not PCCS.
        """
        request = _build_window_control_request(vin, 1)  # OPEN_ALL
        return self._send_invocation(
            vin,
            _METHOD_WINDOW_CONTROL,
            request,
            command_name="Window control",
            allow_delivered_on_cancel=True,
        )

    def window_close(self, vin: str) -> dict:
        """Close all vehicle windows.

        Requires the PCCS 2FA token (customer:attributes:write scope).
        WindowControl is only available on CEP, not PCCS.
        """
        request = _build_window_control_request(vin, 2)  # CLOSE_ALL
        return self._send_invocation(
            vin,
            _METHOD_WINDOW_CONTROL,
            request,
            command_name="Window control",
            allow_delivered_on_cancel=True,
        )
