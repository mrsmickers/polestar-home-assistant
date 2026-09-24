"""Tests for CEP gRPC client protobuf parsers and request builder."""

import struct

import pytest

from custom_components.polestar_soc.cep import (
    _METHOD_GET_MYCARS,
    CepDataError,
    _build_get_mycars_request,
    _build_location_request,
    _build_vin_request,
    _format_climate_status,
    _format_heating_intensity,
    _parse_availability_response,
    _parse_battery_response,
    _parse_climate_response,
    _parse_exterior_response,
    _parse_health_response,
    _parse_location_response,
    _parse_mycars_response,
)
from custom_components.polestar_soc.proto import (
    _decode_message,
    _encode_field_bytes,
    _encode_field_fixed32,
    _encode_field_varint,
    _encode_varint,
    _get_submessage,
)
from custom_components.polestar_soc.sensor import (
    _charging_power,
    _charging_time_remaining,
    _software_version,
)

# Synthetic test payloads built with a fake VIN.
# ParkingClimatization: climate off, all seat heaters off
CLIMATE_PAYLOAD = bytes.fromhex(
    "121159534d594b4541453152423030303030311a1e0a0c08b0dcb6cd0610808c8d9e02"
    "10021800300348005000580060006800"
)

# Battery: SOC=76.0, avg_consumption=2.9, range=230km/140mi,
# charger_connection=2(DISCONNECTED), charging_status=2(IDLE),
# est_charging_time=0, charging_type=1, charger_power_status=1, field28=5
BATTERY_PAYLOAD = bytes.fromhex(
    "121159534d594b4541453152423030303030311a350a0c08b0dcb6cd0610808c8d9e02"
    "11000000000000534019333333333333074020e601280030023802408c01"
    "880101d00101e00105"
)

TEST_VIN = "YSMYKEAE1RB000001"


class TestBuildVinRequest:
    def test_produces_field_2_string(self):
        result = _build_vin_request(TEST_VIN)
        fields = _decode_message(result)
        # Field 2 should be the VIN as bytes
        assert 2 in fields
        assert fields[2][0] == TEST_VIN.encode("utf-8")

    def test_roundtrip_vin(self):
        result = _build_vin_request(TEST_VIN)
        # Should be: tag(field 2, wire type 2) + varint(len) + VIN bytes
        expected = _encode_field_bytes(2, TEST_VIN.encode("utf-8"))
        assert result == expected


class TestMyCars:
    @staticmethod
    def _entry(vin: str, version: str) -> bytes:
        details = b"".join(
            (
                _encode_field_bytes(1, vin.encode()),
                _encode_field_bytes(6, b"Polestar 4"),
                _encode_field_bytes(7, b"2026"),
                _encode_field_bytes(9, version.encode()),
                _encode_field_bytes(10, b"GB"),
            )
        )
        return _encode_field_bytes(1, details)

    def test_service_path(self):
        assert _METHOD_GET_MYCARS == "/car_information.CarInformation/GetMyCars"

    def test_request_contains_uuid_and_vin(self):
        fields = _decode_message(_build_get_mycars_request(TEST_VIN))
        assert len(fields[1][0].decode()) == 36
        assert fields[2] == [TEST_VIN.encode()]

    def test_request_rejects_missing_vin(self):
        with pytest.raises(CepDataError, match="invalid requested VIN"):
            _build_get_mycars_request("")

    def test_parser_selects_matching_vin_from_multiple_entries(self):
        other = self._entry("WVWZZZ1JZXW000001", "P4.2.10")
        wanted = self._entry(TEST_VIN, "P4.2.11")
        result = _parse_mycars_response(
            _encode_field_bytes(1, other) + _encode_field_bytes(1, wanted),
            TEST_VIN,
        )
        assert result == {
            "vin": TEST_VIN,
            "model_name": "Polestar 4",
            "model_year": "2026",
            "installed_software_version": "P4.2.11",
            "market": "GB",
        }

    def test_parser_rejects_wrong_explicit_vin(self):
        payload = _encode_field_bytes(1, self._entry("WVWZZZ1JZXW000001", "P4.2.10"))
        with pytest.raises(CepDataError, match="none matched"):
            _parse_mycars_response(payload, TEST_VIN)

    def test_parser_rejects_single_entry_with_omitted_vin(self):
        payload = _encode_field_bytes(1, self._entry("", "P4.2.11"))
        with pytest.raises(CepDataError, match="none matched"):
            _parse_mycars_response(payload, TEST_VIN)

    def test_parser_uses_last_value_for_duplicate_singular_fields(self):
        details = b"".join(
            (
                _encode_field_bytes(1, b"WVWZZZ1JZXW000001"),
                _encode_field_bytes(1, TEST_VIN.encode()),
                _encode_field_bytes(9, b"P4.2.10"),
                _encode_field_bytes(9, b"P4.2.11"),
            )
        )
        payload = _encode_field_bytes(1, _encode_field_bytes(1, details))
        result = _parse_mycars_response(payload, TEST_VIN)
        assert result["vin"] == TEST_VIN
        assert result["installed_software_version"] == "P4.2.11"

    def test_parser_uses_last_duplicate_details_message(self):
        wrong_entry = _decode_message(self._entry("WVWZZZ1JZXW000001", "P4.2.10"))[1][0]
        wanted_entry = _decode_message(self._entry(TEST_VIN, "P4.2.11"))[1][0]
        entry = _encode_field_bytes(1, wrong_entry) + _encode_field_bytes(1, wanted_entry)
        result = _parse_mycars_response(_encode_field_bytes(1, entry), TEST_VIN)
        assert result["installed_software_version"] == "P4.2.11"

    def test_parser_rejects_missing_installed_version(self):
        payload = _encode_field_bytes(1, self._entry(TEST_VIN, ""))
        with pytest.raises(CepDataError, match="omitted software version"):
            _parse_mycars_response(payload, TEST_VIN)

    def test_parser_rejects_empty_response(self):
        with pytest.raises(CepDataError, match="empty response"):
            _parse_mycars_response(b"", TEST_VIN)

    def test_parser_rejects_empty_requested_vin_even_with_empty_entry_vin(self):
        payload = _encode_field_bytes(1, self._entry("", "P4.2.11"))
        with pytest.raises(CepDataError, match="invalid requested VIN"):
            _parse_mycars_response(payload, "")

    def test_parser_rejects_truncated_length_delimited_version(self):
        details = _encode_field_bytes(1, TEST_VIN.encode()) + b"\x4a\x06P4.2"
        payload = _encode_field_bytes(1, _encode_field_bytes(1, details))
        with pytest.raises(CepDataError, match="malformed protobuf"):
            _parse_mycars_response(payload, TEST_VIN)

    def test_parser_rejects_invalid_utf8_version(self):
        details = _encode_field_bytes(1, TEST_VIN.encode()) + _encode_field_bytes(9, b"\xff")
        payload = _encode_field_bytes(1, _encode_field_bytes(1, details))
        with pytest.raises(CepDataError, match="invalid UTF-8"):
            _parse_mycars_response(payload, TEST_VIN)

    def test_parser_rejects_invalid_version_format(self):
        payload = _encode_field_bytes(1, self._entry(TEST_VIN, "4.2.11"))
        with pytest.raises(CepDataError, match="invalid installed software version"):
            _parse_mycars_response(payload, TEST_VIN)

    def test_parser_rejects_unicode_digits_in_version(self):
        payload = _encode_field_bytes(1, self._entry(TEST_VIN, "P٤.٢.١١"))
        with pytest.raises(CepDataError, match="invalid installed software version"):
            _parse_mycars_response(payload, TEST_VIN)

    def test_parser_rejects_duplicate_entries_for_requested_vin(self):
        payload = _encode_field_bytes(
            1, self._entry(TEST_VIN, "P4.2.10")
        ) + _encode_field_bytes(1, self._entry(TEST_VIN, "P4.2.11"))
        with pytest.raises(CepDataError, match="duplicate entries"):
            _parse_mycars_response(payload, TEST_VIN)

    def test_software_sensor_returns_installed_version(self):
        data = {"software": {TEST_VIN: {"installed_software_version": "P4.2.11"}}}
        assert _software_version(data, TEST_VIN) == "P4.2.11"


class TestParseClimateResponse:
    def test_parsed_response(self):
        result = _parse_climate_response(CLIMATE_PAYLOAD)
        assert result["status"] == "Off"
        assert result["driver_seat_heating"] == "Off"
        assert result["passenger_seat_heating"] == "Off"
        assert result["rear_left_seat_heating"] == "Off"
        assert result["rear_right_seat_heating"] == "Off"
        assert result["steering_wheel_heating"] == "Off"

    def test_empty_response(self):
        result = _parse_climate_response(b"")
        assert result["status"] is None
        assert result["driver_seat_heating"] is None

    def test_two_level_decode(self):
        """Verify outer envelope has field 2=VIN and field 3=state."""
        outer = _decode_message(CLIMATE_PAYLOAD)
        # Field 2 should be VIN
        assert outer[2][0] == TEST_VIN.encode("utf-8")
        # Field 3 should be a sub-message (bytes)
        assert isinstance(outer[3][0], (bytes, bytearray))
        # Inner state should have field 2 (running status)
        state = _get_submessage(outer, 3)
        assert state is not None
        assert 2 in state  # running status field

    def test_missing_state_submessage(self):
        """Response with VIN but no field 3 returns None values."""
        # Just a VIN field with no state sub-message
        data = _encode_field_bytes(2, TEST_VIN.encode("utf-8"))
        result = _parse_climate_response(data)
        assert result["status"] is None
        assert result["driver_seat_heating"] is None


class TestParseBatteryResponse:
    def test_parsed_response(self):
        result = _parse_battery_response(BATTERY_PAYLOAD)
        assert result["soc"] == pytest.approx(76.0)
        assert result["estimated_range_km"] == 230
        assert result["avg_energy_consumption_kwh_per_100km"] == pytest.approx(2.9, abs=0.1)
        assert result["charger_connection_status"] == 2  # DISCONNECTED
        assert result["charging_status"] == 2  # IDLE (from field 7)
        assert result["estimated_charging_time_minutes"] == 0  # explicit field 5 = 0
        assert result["estimated_range_miles"] == 140
        assert result["charging_power_watts"] is None  # field 10 not in payload
        assert result["charging_type"] == 1  # NONE (not charging)

    def test_soc_ignores_later_wrong_wire_varint(self):
        state = (
            _encode_varint((2 << 3) | 1)
            + struct.pack("<d", 76.0)
            + _encode_field_varint(2, 0)
        )
        result = _parse_battery_response(_encode_field_bytes(3, state))
        assert result["soc"] == pytest.approx(76.0)

    def test_charging_status_ignores_later_wrong_wire_fixed32(self):
        state = (
            _encode_field_varint(7, 1)
            + _encode_varint((7 << 3) | 5)
            + struct.pack("<I", 2)
        )
        result = _parse_battery_response(_encode_field_bytes(3, state))
        assert result["charging_status"] == 1
        assert result["raw_fields"][7] == 1

    def test_explicit_zero_time_and_power_preserve_wire_presence(self):
        state = b"".join(
            (
                _encode_field_varint(5, 0),
                _encode_field_varint(7, 1),
                _encode_field_varint(10, 0),
                _encode_field_varint(17, 2),
            )
        )
        payload = _encode_field_bytes(3, state)

        result = _parse_battery_response(payload)

        assert result["estimated_charging_time_minutes"] == 0
        assert result["charging_power_watts"] == 0
        assert result["raw_fields"][5] == 0
        assert result["raw_fields"][10] == 0

    @pytest.mark.parametrize(
        ("indicator_fields", "expected_status", "expected_type"),
        (
            (((7, 0), (17, 1)), 0, 1),
            (((7, 2), (17, 0)), 2, 0),
        ),
    )
    def test_explicit_unknown_indicator_blocks_end_to_end_zero_inference(
        self,
        indicator_fields,
        expected_status,
        expected_type,
    ):
        state = b"".join(_encode_field_varint(field, value) for field, value in indicator_fields)
        cep_battery = _parse_battery_response(_encode_field_bytes(3, state))
        data = {"battery": {}, "cep_battery": {TEST_VIN: cep_battery}}

        assert cep_battery["charging_status"] == expected_status
        assert cep_battery["charging_type"] == expected_type
        assert _charging_time_remaining(data, TEST_VIN) is None
        assert _charging_power(data, TEST_VIN) is None

    @pytest.mark.parametrize(
        ("indicator_fields", "expected_status", "expected_type"),
        (
            (((7, 2), (7, 1), (17, 1)), 1, 1),
            (((7, 2), (17, 1), (17, 2)), 2, 2),
        ),
    )
    def test_duplicate_indicators_use_last_value_and_block_contradictory_zero(
        self,
        indicator_fields,
        expected_status,
        expected_type,
    ):
        state = b"".join(_encode_field_varint(field, value) for field, value in indicator_fields)
        cep_battery = _parse_battery_response(_encode_field_bytes(3, state))
        data = {"battery": {}, "cep_battery": {TEST_VIN: cep_battery}}

        assert cep_battery["charging_status"] == expected_status
        assert cep_battery["charging_type"] == expected_type
        assert _charging_time_remaining(data, TEST_VIN) is None
        assert _charging_power(data, TEST_VIN) is None

    def test_raw_fields(self):
        result = _parse_battery_response(BATTERY_PAYLOAD)
        raw = result["raw_fields"]
        assert set(raw.keys()) == {5, 7, 8, 17, 26, 28}
        assert raw[5] == 0  # estimated_charging_time_to_full_minutes
        assert raw[7] == 2  # charging_status (IDLE)
        assert raw[8] == 140  # estimated_distance_to_empty_miles
        assert raw[17] == 1  # charging_type
        assert raw[26] == 1  # charger_power_status
        assert raw[28] == 5  # unknown CEP-specific field

    def test_empty_response(self):
        result = _parse_battery_response(b"")
        assert result["soc"] is None
        assert result["estimated_range_km"] is None
        assert result["charging_status"] is None
        assert result["charger_connection_status"] is None
        assert result["charging_power_watts"] is None
        assert result["charging_type"] is None
        assert result["raw_fields"] == {}

    def test_two_level_decode(self):
        """Verify outer envelope field 3 contains battery state."""
        outer = _decode_message(BATTERY_PAYLOAD)
        assert outer[2][0] == TEST_VIN.encode("utf-8")
        state = _get_submessage(outer, 3)
        assert state is not None
        # Field 2 is SOC (fixed64, stored as uint64)
        assert 2 in state
        raw_soc = state[2][0]
        soc = struct.unpack("<d", struct.pack("<Q", raw_soc))[0]
        assert soc == pytest.approx(76.0)

    def test_missing_state_submessage(self):
        data = _encode_field_bytes(2, TEST_VIN.encode("utf-8"))
        result = _parse_battery_response(data)
        assert result["soc"] is None
        assert result["estimated_range_km"] is None
        assert result["raw_fields"] == {}


class TestFormatClimateStatus:
    def test_known_values(self):
        assert _format_climate_status(0) == "Unknown"
        assert _format_climate_status(2) == "Off"
        assert _format_climate_status(3) == "Pre-conditioning"

    def test_unknown_value(self):
        result = _format_climate_status(99)
        assert result == "Unknown (99)"


class TestFormatHeatingIntensity:
    def test_known_values(self):
        assert _format_heating_intensity(0) == "Off"
        assert _format_heating_intensity(1) == "Low"
        assert _format_heating_intensity(2) == "Medium"
        assert _format_heating_intensity(3) == "High"

    def test_unknown_value(self):
        result = _format_heating_intensity(42)
        assert result == "Unknown (42)"


def _build_location_payload(
    vin: str, longitude: float, latitude: float, timestamp_ms: int
) -> bytes:
    """Build a synthetic GetLastKnownLocation response payload."""
    # field 1 = VIN (string), field 2 = longitude (double/fixed64),
    # field 3 = latitude (double/fixed64), field 4 = timestamp_ms (varint)
    data = _encode_field_bytes(1, vin.encode("utf-8"))
    # Wire type 1 (fixed64) for doubles: tag = (field_number << 3) | 1
    data += struct.pack("<B", (2 << 3) | 1) + struct.pack("<d", longitude)
    data += struct.pack("<B", (3 << 3) | 1) + struct.pack("<d", latitude)
    data += _encode_field_varint(4, timestamp_ms)
    return data


LOCATION_PAYLOAD = _build_location_payload(
    TEST_VIN, longitude=18.068581, latitude=59.329323, timestamp_ms=1772990058845
)


class TestBuildLocationRequest:
    def test_produces_field_1_string(self):
        result = _build_location_request(TEST_VIN)
        fields = _decode_message(result)
        assert 1 in fields
        assert fields[1][0] == TEST_VIN.encode("utf-8")
        assert 2 not in fields

    def test_roundtrip_vin(self):
        result = _build_location_request(TEST_VIN)
        expected = _encode_field_bytes(1, TEST_VIN.encode("utf-8"))
        assert result == expected


class TestParseLocationResponse:
    def test_parsed_response(self):
        result = _parse_location_response(LOCATION_PAYLOAD)
        assert result["latitude"] == pytest.approx(59.329323)
        assert result["longitude"] == pytest.approx(18.068581)
        assert result["timestamp_ms"] == 1772990058845

    def test_empty_response(self):
        result = _parse_location_response(b"")
        assert result["latitude"] is None
        assert result["longitude"] is None
        assert result["timestamp_ms"] is None

    def test_missing_coordinates(self):
        """Response with only VIN returns None values."""
        data = _encode_field_bytes(1, TEST_VIN.encode("utf-8"))
        result = _parse_location_response(data)
        assert result["latitude"] is None
        assert result["longitude"] is None
        assert result["timestamp_ms"] is None

    def test_top_level_decode(self):
        """Verify location fields are at top level (no envelope nesting)."""
        fields = _decode_message(LOCATION_PAYLOAD)
        assert fields[1][0] == TEST_VIN.encode("utf-8")
        # Fields 2 and 3 are fixed64 (doubles stored as uint64)
        assert 2 in fields
        assert 3 in fields
        assert 4 in fields


def _build_exterior_state(field_values: dict[int, int]) -> bytes:
    """Build an ExteriorState sub-message from {field_number: varint_value}."""
    data = b""
    for field_num in sorted(field_values):
        data += _encode_field_varint(field_num, field_values[field_num])
    return data


def _build_exterior_payload(vin: str, field_values: dict[int, int]) -> bytes:
    """Build a synthetic GetLatestExterior response with two-level envelope."""
    state_bytes = _build_exterior_state(field_values)
    # Outer envelope: field 2 = VIN, field 3 = ExteriorState sub-message
    data = _encode_field_bytes(2, vin.encode("utf-8"))
    data += _encode_field_bytes(3, state_bytes)
    return data


# All locked/closed, alarm idle
EXTERIOR_ALL_LOCKED = _build_exterior_payload(
    TEST_VIN,
    {
        2: 2,  # central_lock: LOCKED
        3: 2,  # front_left_door: CLOSED
        4: 2,  # front_right_door: CLOSED
        5: 2,  # rear_left_door: CLOSED
        6: 2,  # rear_right_door: CLOSED
        7: 2,  # front_left_window: CLOSED
        8: 2,  # front_right_window: CLOSED
        9: 2,  # rear_left_window: CLOSED
        10: 2,  # rear_right_window: CLOSED
        11: 2,  # hood: CLOSED
        12: 2,  # tailgate: CLOSED
        13: 2,  # tank_lid: CLOSED
        15: 1,  # alarm: IDLE
    },
)

# Mixed state: unlocked, some doors open/ajar, alarm triggered
EXTERIOR_MIXED = _build_exterior_payload(
    TEST_VIN,
    {
        2: 1,  # central_lock: UNLOCKED
        3: 1,  # front_left_door: OPEN
        4: 3,  # front_right_door: AJAR
        5: 2,  # rear_left_door: CLOSED
        6: 2,  # rear_right_door: CLOSED
        7: 1,  # front_left_window: OPEN
        8: 2,  # front_right_window: CLOSED
        9: 2,  # rear_left_window: CLOSED
        10: 2,  # rear_right_window: CLOSED
        11: 2,  # hood: CLOSED
        12: 1,  # tailgate: OPEN
        13: 2,  # tank_lid: CLOSED
        14: 0,  # sunroof: UNSPECIFIED
        15: 2,  # alarm: TRIGGERED
    },
)


class TestParseExteriorResponse:
    def test_all_locked_closed(self):
        result = _parse_exterior_response(EXTERIOR_ALL_LOCKED)
        assert result["central_lock"] == 2  # LOCKED
        assert result["front_left_door"] == 2  # CLOSED
        assert result["front_right_door"] == 2  # CLOSED
        assert result["rear_left_door"] == 2  # CLOSED
        assert result["rear_right_door"] == 2  # CLOSED
        assert result["front_left_window"] == 2  # CLOSED
        assert result["front_right_window"] == 2  # CLOSED
        assert result["rear_left_window"] == 2  # CLOSED
        assert result["rear_right_window"] == 2  # CLOSED
        assert result["hood"] == 2  # CLOSED
        assert result["tailgate"] == 2  # CLOSED
        assert result["tank_lid"] == 2  # CLOSED
        assert result["sunroof"] is None  # not in payload → UNSPECIFIED → None
        assert result["alarm"] == 1  # IDLE

    def test_mixed_open_ajar(self):
        result = _parse_exterior_response(EXTERIOR_MIXED)
        assert result["central_lock"] == 1  # UNLOCKED
        assert result["front_left_door"] == 1  # OPEN
        assert result["front_right_door"] == 3  # AJAR
        assert result["rear_left_door"] == 2  # CLOSED
        assert result["front_left_window"] == 1  # OPEN
        assert result["front_right_window"] == 2  # CLOSED
        assert result["tailgate"] == 1  # OPEN
        assert result["sunroof"] is None  # UNSPECIFIED(0) → None
        assert result["alarm"] == 2  # TRIGGERED

    def test_empty_response(self):
        result = _parse_exterior_response(b"")
        assert result["central_lock"] is None
        assert result["front_left_door"] is None
        assert result["alarm"] is None
        assert len(result) == 14  # all 14 fields present

    def test_missing_state_submessage(self):
        """Response with VIN but no field 3 returns all None."""
        data = _encode_field_bytes(2, TEST_VIN.encode("utf-8"))
        result = _parse_exterior_response(data)
        assert result["central_lock"] is None
        assert result["front_left_door"] is None
        assert result["alarm"] is None

    def test_two_level_envelope(self):
        """Verify outer envelope has field 2=VIN and field 3=ExteriorState."""
        outer = _decode_message(EXTERIOR_ALL_LOCKED)
        assert outer[2][0] == TEST_VIN.encode("utf-8")
        assert isinstance(outer[3][0], (bytes, bytearray))
        state = _get_submessage(outer, 3)
        assert state is not None
        assert 2 in state  # central_lock field


# ---------------------------------------------------------------------------
# Availability tests
# ---------------------------------------------------------------------------


def _build_availability_state(field_values: dict[int, int]) -> bytes:
    """Build an Availability state sub-message from {field_number: varint_value}."""
    data = b""
    for field_num in sorted(field_values):
        data += _encode_field_varint(field_num, field_values[field_num])
    return data


def _build_availability_payload(vin: str, field_values: dict[int, int]) -> bytes:
    """Build a synthetic GetLatestAvailability response with two-level envelope."""
    state_bytes = _build_availability_state(field_values)
    data = _encode_field_bytes(2, vin.encode("utf-8"))
    data += _encode_field_bytes(3, state_bytes)
    return data


AVAILABILITY_AVAILABLE = _build_availability_payload(
    TEST_VIN,
    {
        3: 1,  # availability_status: AVAILABLE
        5: 2,  # usage_mode: INACTIVE
    },
)

AVAILABILITY_UNAVAILABLE = _build_availability_payload(
    TEST_VIN,
    {
        3: 2,  # availability_status: UNAVAILABLE
        4: 2,  # unavailable_reason: POWER_SAVING_MODE
        5: 1,  # usage_mode: ABANDONED
    },
)


class TestParseAvailabilityResponse:
    def test_available_vehicle(self):
        result = _parse_availability_response(AVAILABILITY_AVAILABLE)
        assert result["availability_status"] == 1  # AVAILABLE
        assert result["unavailable_reason"] is None  # not set
        assert result["usage_mode"] == 2  # INACTIVE

    def test_unavailable_vehicle(self):
        result = _parse_availability_response(AVAILABILITY_UNAVAILABLE)
        assert result["availability_status"] == 2  # UNAVAILABLE
        assert result["unavailable_reason"] == 2  # POWER_SAVING_MODE
        assert result["usage_mode"] == 1  # ABANDONED

    def test_unspecified_values(self):
        """Value 0 for any field returns None."""
        payload = _build_availability_payload(
            TEST_VIN,
            {3: 0, 4: 0, 5: 0},
        )
        result = _parse_availability_response(payload)
        assert result["availability_status"] is None
        assert result["unavailable_reason"] is None
        assert result["usage_mode"] is None

    def test_empty_response(self):
        result = _parse_availability_response(b"")
        assert result["availability_status"] is None
        assert result["unavailable_reason"] is None
        assert result["usage_mode"] is None

    def test_missing_state_submessage(self):
        """Response with VIN but no field 3 returns all None."""
        data = _encode_field_bytes(2, TEST_VIN.encode("utf-8"))
        result = _parse_availability_response(data)
        assert result["availability_status"] is None
        assert result["unavailable_reason"] is None
        assert result["usage_mode"] is None

    def test_two_level_envelope(self):
        """Verify outer envelope has field 2=VIN and field 3=Availability state."""
        outer = _decode_message(AVAILABILITY_AVAILABLE)
        assert outer[2][0] == TEST_VIN.encode("utf-8")
        assert isinstance(outer[3][0], (bytes, bytearray))
        state = _get_submessage(outer, 3)
        assert state is not None
        assert 3 in state  # availability_status field


# ---------------------------------------------------------------------------
# Health tests
# ---------------------------------------------------------------------------


def _build_health_state(varint_fields: dict[int, int], float_fields: dict[int, float]) -> bytes:
    """Build a Health sub-message from varint and float (fixed32) fields."""
    data = b""
    for field_num in sorted(varint_fields):
        data += _encode_field_varint(field_num, varint_fields[field_num])
    for field_num in sorted(float_fields):
        data += _encode_field_fixed32(field_num, float_fields[field_num])
    return data


def _build_health_payload(
    vin: str, varint_fields: dict[int, int], float_fields: dict[int, float]
) -> bytes:
    """Build a synthetic GetHealth response with two-level envelope."""
    state_bytes = _build_health_state(varint_fields, float_fields)
    data = _encode_field_bytes(2, vin.encode("utf-8"))
    data += _encode_field_bytes(3, state_bytes)
    return data


# Full health response: service info, tyre warnings, fluid warnings, pressures
HEALTH_FULL = _build_health_payload(
    TEST_VIN,
    varint_fields={
        3: 180,  # days_to_service
        4: 12000,  # distance_to_service_km
        5: 1,  # service_warning: NO_WARNING
        6: 1,  # brake_fluid_level_warning: NO_WARNING
        7: 1,  # engine_coolant_level_warning: NO_WARNING
        8: 1,  # oil_level_warning: NO_WARNING
        9: 1,  # front_left_tyre_pressure_warning: NO_WARNING
        10: 3,  # front_right_tyre_pressure_warning: LOW
        11: 1,  # rear_left_tyre_pressure_warning: NO_WARNING
        12: 2,  # rear_right_tyre_pressure_warning: VERY_LOW
        13: 1,  # washer_fluid_level_warning: NO_WARNING
        14: 1,  # brake_light_left_warning: NO_WARNING
        15: 2,  # brake_light_center_warning: FAILURE
        38: 1,  # low_voltage_battery_warning: NO_WARNING
    },
    float_fields={
        39: 240.0,  # front_left_tyre_pressure_kpa
        40: 210.5,  # front_right_tyre_pressure_kpa
        41: 245.0,  # rear_left_tyre_pressure_kpa
        42: 190.3,  # rear_right_tyre_pressure_kpa
        43: 240.0,  # front_tyres_reference_pressure_kpa
        44: 250.0,  # rear_tyres_reference_pressure_kpa
    },
)

# Minimal: only tyre pressures, no warnings set
HEALTH_MINIMAL = _build_health_payload(
    TEST_VIN,
    varint_fields={},
    float_fields={
        39: 235.0,
        40: 235.0,
        41: 250.0,
        42: 250.0,
    },
)


class TestParseHealthResponse:
    def test_full_response(self):
        result = _parse_health_response(HEALTH_FULL)
        # Service info
        assert result["days_to_service"] == 180
        assert result["distance_to_service_km"] == 12000
        assert result["service_warning"] == 1  # NO_WARNING
        # Fluid warnings
        assert result["brake_fluid_level_warning"] == 1  # NO_WARNING
        assert result["engine_coolant_level_warning"] == 1  # NO_WARNING
        assert result["oil_level_warning"] == 1  # NO_WARNING
        assert result["washer_fluid_level_warning"] == 1  # NO_WARNING
        # Tyre pressure warnings
        assert result["front_left_tyre_pressure_warning"] == 1  # NO_WARNING
        assert result["front_right_tyre_pressure_warning"] == 3  # LOW
        assert result["rear_left_tyre_pressure_warning"] == 1  # NO_WARNING
        assert result["rear_right_tyre_pressure_warning"] == 2  # VERY_LOW
        # 12V battery
        assert result["low_voltage_battery_warning"] == 1  # NO_WARNING
        # Light warnings
        assert result["brake_light_left_warning"] == 1  # NO_WARNING
        assert result["brake_light_center_warning"] == 2  # FAILURE

    def test_tyre_pressure_floats(self):
        result = _parse_health_response(HEALTH_FULL)
        assert result["front_left_tyre_pressure_kpa"] == pytest.approx(240.0, abs=0.2)
        assert result["front_right_tyre_pressure_kpa"] == pytest.approx(210.5, abs=0.2)
        assert result["rear_left_tyre_pressure_kpa"] == pytest.approx(245.0, abs=0.2)
        assert result["rear_right_tyre_pressure_kpa"] == pytest.approx(190.3, abs=0.2)
        assert result["front_tyres_reference_pressure_kpa"] == pytest.approx(240.0, abs=0.2)
        assert result["rear_tyres_reference_pressure_kpa"] == pytest.approx(250.0, abs=0.2)

    def test_minimal_response_missing_warnings(self):
        """Response with only float fields returns None for varint warnings."""
        result = _parse_health_response(HEALTH_MINIMAL)
        assert result["front_left_tyre_pressure_kpa"] == pytest.approx(235.0, abs=0.2)
        assert result["days_to_service"] is None
        assert result["service_warning"] is None
        assert result["front_left_tyre_pressure_warning"] is None
        assert result["washer_fluid_level_warning"] is None
        assert result["low_voltage_battery_warning"] is None

    def test_empty_response(self):
        result = _parse_health_response(b"")
        assert result["front_left_tyre_pressure_kpa"] is None
        assert result["rear_right_tyre_pressure_kpa"] is None
        assert result["days_to_service"] is None
        assert result["service_warning"] is None
        assert result["front_left_tyre_pressure_warning"] is None
        assert result["washer_fluid_level_warning"] is None
        assert result["low_voltage_battery_warning"] is None
        assert result["brake_light_left_warning"] is None
        assert result["front_tyres_reference_pressure_kpa"] is None

    def test_missing_state_submessage(self):
        """Response with VIN but no field 3 returns all None."""
        data = _encode_field_bytes(2, TEST_VIN.encode("utf-8"))
        result = _parse_health_response(data)
        assert result["front_left_tyre_pressure_kpa"] is None
        assert result["days_to_service"] is None
        assert result["front_left_tyre_pressure_warning"] is None

    def test_two_level_envelope(self):
        """Verify outer envelope has field 2=VIN and field 3=Health state."""
        outer = _decode_message(HEALTH_FULL)
        assert outer[2][0] == TEST_VIN.encode("utf-8")
        assert isinstance(outer[3][0], (bytes, bytearray))
        state = _get_submessage(outer, 3)
        assert state is not None
        assert 3 in state  # days_to_service field

    def test_unspecified_enum_returns_none(self):
        """Enum value 0 (UNSPECIFIED) returns None."""
        payload = _build_health_payload(
            TEST_VIN,
            varint_fields={5: 0, 9: 0, 13: 0, 38: 0},
            float_fields={},
        )
        result = _parse_health_response(payload)
        assert result["service_warning"] is None
        assert result["front_left_tyre_pressure_warning"] is None
        assert result["washer_fluid_level_warning"] is None
        assert result["low_voltage_battery_warning"] is None

    def test_zero_days_to_service_is_not_none(self):
        """days_to_service=0 should return 0, not None."""
        payload = _build_health_payload(
            TEST_VIN,
            varint_fields={3: 0, 4: 0},
            float_fields={},
        )
        result = _parse_health_response(payload)
        assert result["days_to_service"] == 0
        assert result["distance_to_service_km"] == 0

    def test_pressure_rounding(self):
        """Verify tyre pressure values are rounded to 1 decimal."""
        payload = _build_health_payload(
            TEST_VIN,
            varint_fields={},
            float_fields={39: 240.123},
        )
        result = _parse_health_response(payload)
        # IEEE 754 single-precision may not store 240.123 exactly,
        # but the result should be rounded to 1 decimal place
        val = result["front_left_tyre_pressure_kpa"]
        assert val is not None
        assert val == round(val, 1)
