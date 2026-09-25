"""Tests for sensor value extraction functions."""

from custom_components.polestar_soc.sensor import (
    _battery_soc,
    _cep_is_explicitly_not_charging,
    _charge_location_attributes,
    _charge_location_name,
    _charging_power,
    _charging_status,
    _charging_time_remaining,
    _charging_type,
    _climate_heating,
    _climate_status,
    _estimated_range,
    _estimated_range_miles,
    _odometer_km,
)

VIN = "YSMYKEAE1RB000001"


# ---------------------------------------------------------------------------
# Existing sensors (updated signature: data, vin)
# ---------------------------------------------------------------------------


class TestBatterySoc:
    def test_returns_percentage(self, sample_coordinator_data):
        assert _battery_soc(sample_coordinator_data, VIN) == 72

    def test_falls_back_to_cep_when_graphql_battery_missing(self):
        data = {
            "battery": {},
            "cep_battery": {VIN: {"soc": 76.0}},
        }
        assert _battery_soc(data, VIN) == 76.0

    def test_none_when_no_battery_source(self, sample_coordinator_data):
        sample_coordinator_data["battery"] = {}
        sample_coordinator_data["cep_battery"] = {}
        assert _battery_soc(sample_coordinator_data, VIN) is None

    def test_none_when_missing_key(self):
        data = {"battery": {VIN: {"vin": "X"}}}
        assert _battery_soc(data, VIN) is None

    def test_zero_percent(self):
        data = {"battery": {VIN: {"batteryChargeLevelPercentage": 0}}}
        assert _battery_soc(data, VIN) == 0

    def test_full_charge(self):
        data = {"battery": {VIN: {"batteryChargeLevelPercentage": 100}}}
        assert _battery_soc(data, VIN) == 100


class TestChargingStatus:
    def test_known_status(self, sample_coordinator_data):
        assert _charging_status(sample_coordinator_data, VIN) == "Charging"

    def test_none_battery_returns_unknown(self):
        data = {"battery": {}}
        assert _charging_status(data, VIN) == "Unknown"

    def test_falls_back_to_cep_idle_when_graphql_missing(self):
        data = {
            "battery": {},
            "cep_battery": {VIN: {"charging_status": 2}},
        }
        assert _charging_status(data, VIN) == "Idle"

    def test_non_null_graphql_unspecified_remains_authoritative(self):
        data = {
            "battery": {VIN: {"chargingStatus": "CHARGING_STATUS_UNSPECIFIED"}},
            "cep_battery": {VIN: {"charging_status": 2}},
        }
        assert _charging_status(data, VIN) == "Unknown"

    def test_graphql_null_falls_back_to_cep(self):
        data = {
            "battery": {VIN: {"chargingStatus": None}},
            "cep_battery": {VIN: {"charging_status": 3}},
        }
        assert _charging_status(data, VIN) == "Scheduled"

    def test_unknown_cep_status_remains_unknown(self):
        data = {
            "battery": {},
            "cep_battery": {VIN: {"charging_status": 99}},
        }
        assert _charging_status(data, VIN) == "Unknown"

    def test_wrong_vin_cep_data_is_not_used(self):
        data = {
            "battery": {},
            "cep_battery": {"OTHER": {"charging_status": 2}},
        }
        assert _charging_status(data, VIN) == "Unknown"

    def test_idle(self):
        data = {"battery": {VIN: {"chargingStatus": "CHARGING_STATUS_IDLE"}}}
        assert _charging_status(data, VIN) == "Idle"

    def test_missing_status_key(self):
        data = {"battery": {VIN: {"vin": "X"}}}
        result = _charging_status(data, VIN)
        assert result == "Unknown"


class TestChargingTimeRemaining:
    def test_returns_minutes(self, sample_coordinator_data):
        assert _charging_time_remaining(sample_coordinator_data, VIN) == 95

    def test_none_when_no_battery(self):
        data = {"battery": {}}
        assert _charging_time_remaining(data, VIN) is None

    def test_falls_back_to_cep_minutes_when_graphql_missing(self):
        data = {
            "battery": {},
            "cep_battery": {VIN: {"estimated_charging_time_minutes": 45}},
        }
        assert _charging_time_remaining(data, VIN) == 45

    def test_returns_zero_when_cep_reports_idle(self):
        data = {
            "battery": {},
            "cep_battery": {
                VIN: {
                    "charging_status": 2,
                    "estimated_charging_time_minutes": None,
                }
            },
        }
        assert _charging_time_remaining(data, VIN) == 0

    def test_contradictory_cep_flags_do_not_infer_zero(self):
        data = {
            "battery": {},
            "cep_battery": {
                VIN: {
                    "estimated_charging_time_minutes": None,
                    "charging_status": 1,
                    "charging_type": 1,
                }
            },
        }
        assert _charging_time_remaining(data, VIN) is None

    def test_zero_minutes(self):
        data = {"battery": {VIN: {"estimatedChargingTimeToFullMinutes": 0}}}
        assert _charging_time_remaining(data, VIN) == 0


class TestOdometerKm:
    def test_converts_meters_to_km(self, sample_coordinator_data):
        result = _odometer_km(sample_coordinator_data, VIN)
        assert result == 12345.7  # 12345678 / 1000, rounded to 1 decimal

    def test_none_when_no_odometer(self):
        data = {"odometer": {}}
        assert _odometer_km(data, VIN) is None

    def test_none_when_missing_key(self):
        data = {"odometer": {VIN: {"vin": "X"}}}
        assert _odometer_km(data, VIN) is None

    def test_zero_meters(self):
        data = {"odometer": {VIN: {"odometerMeters": 0}}}
        assert _odometer_km(data, VIN) == 0.0

    def test_small_value(self):
        data = {"odometer": {VIN: {"odometerMeters": 500}}}
        assert _odometer_km(data, VIN) == 0.5


# ---------------------------------------------------------------------------
# New climate sensors
# ---------------------------------------------------------------------------


class TestClimateStatus:
    def test_returns_status(self, sample_coordinator_data):
        assert _climate_status(sample_coordinator_data, VIN) == "Off"

    def test_none_when_no_climate(self):
        data = {"climate": {}}
        assert _climate_status(data, VIN) is None

    def test_none_when_empty_data(self):
        assert _climate_status({}, VIN) is None

    def test_active_status(self):
        data = {"climate": {VIN: {"status": "Pre-conditioning"}}}
        assert _climate_status(data, VIN) == "Pre-conditioning"


class TestDriverSeatHeating:
    def test_returns_level(self, sample_coordinator_data):
        fn = _climate_heating("driver_seat_heating")
        assert fn(sample_coordinator_data, VIN) == "Off"

    def test_none_when_no_climate(self):
        fn = _climate_heating("driver_seat_heating")
        data = {"climate": {}}
        assert fn(data, VIN) is None

    def test_heating_active(self):
        fn = _climate_heating("driver_seat_heating")
        data = {"climate": {VIN: {"driver_seat_heating": "High"}}}
        assert fn(data, VIN) == "High"


class TestPassengerSeatHeating:
    def test_returns_level(self, sample_coordinator_data):
        fn = _climate_heating("passenger_seat_heating")
        assert fn(sample_coordinator_data, VIN) == "Off"


class TestRearLeftSeatHeating:
    def test_returns_level(self, sample_coordinator_data):
        fn = _climate_heating("rear_left_seat_heating")
        assert fn(sample_coordinator_data, VIN) == "Off"


class TestRearRightSeatHeating:
    def test_returns_level(self, sample_coordinator_data):
        fn = _climate_heating("rear_right_seat_heating")
        assert fn(sample_coordinator_data, VIN) == "Off"


class TestSteeringWheelHeating:
    def test_returns_level(self, sample_coordinator_data):
        fn = _climate_heating("steering_wheel_heating")
        assert fn(sample_coordinator_data, VIN) == "Off"

    def test_none_when_no_climate(self):
        fn = _climate_heating("steering_wheel_heating")
        assert fn({}, VIN) is None


class TestEstimatedRange:
    def test_returns_km(self, sample_coordinator_data):
        assert _estimated_range(sample_coordinator_data, VIN) == 230

    def test_none_when_no_cep_battery(self):
        data = {"cep_battery": {}}
        assert _estimated_range(data, VIN) is None

    def test_none_when_empty_data(self):
        assert _estimated_range({}, VIN) is None

    def test_none_when_range_missing(self):
        data = {"cep_battery": {VIN: {"soc": 76.0}}}
        assert _estimated_range(data, VIN) is None


# ---------------------------------------------------------------------------
# Battery/charging sensors (CEP)
# ---------------------------------------------------------------------------


class TestChargingPower:
    def test_returns_watts(self):
        data = {"cep_battery": {VIN: {"charging_power_watts": 11000}}}
        assert _charging_power(data, VIN) == 11000

    def test_graphql_charging_and_cep_not_charging_disagreement_is_unknown(
        self, sample_coordinator_data
    ):
        # GraphQL says charging while CEP says not charging; do not fabricate zero power.
        assert _charging_power(sample_coordinator_data, VIN) is None

    def test_contradictory_cep_flags_do_not_infer_zero(self):
        data = {
            "cep_battery": {
                VIN: {
                    "charging_power_watts": None,
                    "charging_status": 1,
                    "charging_type": 1,
                }
            }
        }
        assert _charging_power(data, VIN) is None

    def test_none_when_no_cep_battery(self):
        assert _charging_power({}, VIN) is None

    def test_none_when_missing_key(self):
        data = {"cep_battery": {VIN: {"soc": 76.0}}}
        assert _charging_power(data, VIN) is None


class TestCepChargingMeasurementSafety:
    def test_non_charging_truth_table(self):
        cases = (
            ({}, False),
            ({"charging_status": 1}, False),
            ({"charging_status": 2}, True),
            ({"charging_status": 3}, True),
            ({"charging_status": 99}, False),
            ({"charging_type": 1}, True),
            ({"charging_type": 2}, False),
            ({"charging_type": 3}, False),
            ({"charging_type": 4}, False),
            ({"charging_type": 99}, False),
            ({"charging_status": 2, "charging_type": 1}, True),
            ({"charging_status": 3, "charging_type": 1}, True),
            ({"charging_status": 1, "charging_type": 1}, False),
            ({"charging_status": 2, "charging_type": 2}, False),
            ({"charging_status": 99, "charging_type": 1}, False),
            ({"charging_status": 2, "charging_type": 99}, False),
        )

        for telemetry, expected in cases:
            assert _cep_is_explicitly_not_charging(telemetry) is expected

    def test_explicit_zero_measurements_are_preserved_while_charging(self):
        data = {
            "battery": {},
            "cep_battery": {
                VIN: {
                    "charging_status": 1,
                    "charging_type": 2,
                    "estimated_charging_time_minutes": 0,
                    "charging_power_watts": 0,
                }
            },
        }
        assert _charging_time_remaining(data, VIN) == 0
        assert _charging_power(data, VIN) == 0

    def test_idle_status_with_active_type_does_not_infer_zero(self):
        data = {
            "battery": {},
            "cep_battery": {
                VIN: {
                    "charging_status": 2,
                    "charging_type": 2,
                    "estimated_charging_time_minutes": None,
                    "charging_power_watts": None,
                }
            },
        }
        assert _charging_time_remaining(data, VIN) is None
        assert _charging_power(data, VIN) is None

    def test_unknown_status_with_inactive_type_does_not_infer_zero(self):
        data = {
            "battery": {},
            "cep_battery": {
                VIN: {
                    "charging_status": 99,
                    "charging_type": 1,
                    "estimated_charging_time_minutes": None,
                    "charging_power_watts": None,
                }
            },
        }
        assert _charging_time_remaining(data, VIN) is None
        assert _charging_power(data, VIN) is None

    def test_consistent_active_indicators_keep_missing_measurements_unknown(self):
        data = {
            "battery": {},
            "cep_battery": {
                VIN: {
                    "charging_status": 1,
                    "charging_type": 3,
                    "estimated_charging_time_minutes": None,
                    "charging_power_watts": None,
                }
            },
        }
        assert _charging_time_remaining(data, VIN) is None
        assert _charging_power(data, VIN) is None

    def test_wrong_vin_does_not_supply_measurements(self):
        data = {
            "battery": {},
            "cep_battery": {
                "OTHER": {
                    "charging_status": 2,
                    "charging_type": 1,
                    "estimated_charging_time_minutes": 0,
                    "charging_power_watts": 0,
                }
            },
        }
        assert _charging_time_remaining(data, VIN) is None
        assert _charging_power(data, VIN) is None

    def test_authoritative_graphql_noninactive_status_blocks_inferred_zero(self):
        for graphql_status in (
            "CHARGING_STATUS_CHARGING",
            "CHARGING_STATUS_UNSPECIFIED",
            "CHARGING_STATUS_FAULT",
            "CHARGING_STATUS_BACKEND_FUTURE_VALUE",
        ):
            data = {
                "battery": {
                    VIN: {
                        "chargingStatus": graphql_status,
                        "estimatedChargingTimeToFullMinutes": None,
                    }
                },
                "cep_battery": {
                    VIN: {
                        "charging_status": 2,
                        "charging_type": 1,
                        "estimated_charging_time_minutes": None,
                        "charging_power_watts": None,
                    }
                },
            }
            assert _charging_time_remaining(data, VIN) is None
            assert _charging_power(data, VIN) is None

    def test_graphql_and_cep_inactive_agreement_allows_inferred_zero(self):
        for graphql_status in (
            "CHARGING_STATUS_IDLE",
            "CHARGING_STATUS_DONE",
            "CHARGING_STATUS_SCHEDULED",
        ):
            data = {
                "battery": {
                    VIN: {
                        "chargingStatus": graphql_status,
                        "estimatedChargingTimeToFullMinutes": None,
                    }
                },
                "cep_battery": {
                    VIN: {
                        "charging_status": 2,
                        "charging_type": 1,
                        "estimated_charging_time_minutes": None,
                        "charging_power_watts": None,
                    }
                },
            }
            assert _charging_time_remaining(data, VIN) == 0
            assert _charging_power(data, VIN) == 0

    def test_graphql_inactive_and_cep_active_disagreement_blocks_zero(self):
        data = {
            "battery": {
                VIN: {
                    "chargingStatus": "CHARGING_STATUS_IDLE",
                    "estimatedChargingTimeToFullMinutes": None,
                }
            },
            "cep_battery": {
                VIN: {
                    "charging_status": 1,
                    "charging_type": 2,
                    "estimated_charging_time_minutes": None,
                    "charging_power_watts": None,
                }
            },
        }
        assert _charging_time_remaining(data, VIN) is None
        assert _charging_power(data, VIN) is None

    def test_explicit_measurement_zero_remains_authoritative_over_status(self):
        data = {
            "battery": {
                VIN: {
                    "chargingStatus": "CHARGING_STATUS_CHARGING",
                    "estimatedChargingTimeToFullMinutes": None,
                }
            },
            "cep_battery": {
                VIN: {
                    "charging_status": 1,
                    "charging_type": 2,
                    "estimated_charging_time_minutes": 0,
                    "charging_power_watts": 0,
                }
            },
        }
        assert _charging_time_remaining(data, VIN) == 0
        assert _charging_power(data, VIN) == 0


class TestChargingType:
    def test_not_charging(self, sample_coordinator_data):
        # Fixture has charging_type=1 (NONE)
        assert _charging_type(sample_coordinator_data, VIN) == "Not charging"

    def test_ac(self):
        data = {"cep_battery": {VIN: {"charging_type": 2}}}
        assert _charging_type(data, VIN) == "AC"

    def test_dc(self):
        data = {"cep_battery": {VIN: {"charging_type": 3}}}
        assert _charging_type(data, VIN) == "DC"

    def test_wireless(self):
        data = {"cep_battery": {VIN: {"charging_type": 4}}}
        assert _charging_type(data, VIN) == "Wireless"

    def test_none_when_no_cep_battery(self):
        assert _charging_type({}, VIN) is None

    def test_none_when_missing_key(self):
        data = {"cep_battery": {VIN: {"soc": 76.0}}}
        assert _charging_type(data, VIN) is None

    def test_unknown_value_returns_none(self):
        data = {"cep_battery": {VIN: {"charging_type": 99}}}
        assert _charging_type(data, VIN) is None


class TestEstimatedRangeMiles:
    def test_returns_miles(self, sample_coordinator_data):
        assert _estimated_range_miles(sample_coordinator_data, VIN) == 140

    def test_none_when_no_cep_battery(self):
        assert _estimated_range_miles({}, VIN) is None

    def test_none_when_missing_key(self):
        data = {"cep_battery": {VIN: {"soc": 76.0}}}
        assert _estimated_range_miles(data, VIN) is None


class TestChargeLocation:
    @staticmethod
    def _data(current_id: str = "home-id") -> dict:
        return {
            "current_charge_location": {
                VIN: {"status": 1, "location_id": current_id, "arrived_at": 1234}
            },
            "charge_locations": {
                VIN: [
                    {
                        "location_id": "home-id",
                        "alias": "Home",
                        "amp_limit": 16,
                        "minimum_soc": 20,
                        "optimised_charging": True,
                        "bidirectional_charging": False,
                        "available_optimised_charging": 2,
                        "location_type": 1,
                    }
                ]
            },
        }

    def test_current_location_resolves_alias(self):
        assert _charge_location_name(self._data(), VIN) == "Home"

    def test_unknown_location_uses_identifier(self):
        assert _charge_location_name(self._data("other-id"), VIN) == "other-id"

    def test_no_current_location_returns_none(self):
        assert _charge_location_name(self._data(""), VIN) is None

    def test_attributes_expose_settings_without_coordinates(self):
        attrs = _charge_location_attributes(self._data(), VIN)
        assert attrs["saved_location_count"] == 1
        assert attrs["current_location_id"] == "home-id"
        assert attrs["saved_locations"] == self._data()["charge_locations"][VIN]
        assert "latitude" not in str(attrs)
        assert "longitude" not in str(attrs)
