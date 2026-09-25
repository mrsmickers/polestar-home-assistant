"""Tests for device_tracker platform — vehicle GPS location."""

from unittest.mock import MagicMock

from homeassistant.components.device_tracker import SourceType

from custom_components.polestar_soc.const import DOMAIN
from custom_components.polestar_soc.device_tracker import (
    PolestarDeviceTracker,
    PolestarParkedDeviceTracker,
)

VIN = "YSMYKEAE1RB000001"


def _make_tracker(coordinator_data: dict, vehicle: dict, vin: str) -> PolestarDeviceTracker:
    """Create a PolestarDeviceTracker with a mock coordinator."""
    coordinator = MagicMock()
    coordinator.data = coordinator_data
    return PolestarDeviceTracker(coordinator, vehicle, vin)


class TestPolestarDeviceTracker:
    def test_latitude(self, sample_coordinator_data, sample_vehicle):
        tracker = _make_tracker(sample_coordinator_data, sample_vehicle, VIN)
        assert tracker.latitude == 59.329323

    def test_longitude(self, sample_coordinator_data, sample_vehicle):
        tracker = _make_tracker(sample_coordinator_data, sample_vehicle, VIN)
        assert tracker.longitude == 18.068581

    def test_source_type(self, sample_coordinator_data, sample_vehicle):
        tracker = _make_tracker(sample_coordinator_data, sample_vehicle, VIN)
        assert tracker.source_type == SourceType.GPS

    def test_unique_id(self, sample_coordinator_data, sample_vehicle):
        tracker = _make_tracker(sample_coordinator_data, sample_vehicle, VIN)
        assert tracker.unique_id == f"{VIN}_location"

    def test_device_info(self, sample_coordinator_data, sample_vehicle):
        tracker = _make_tracker(sample_coordinator_data, sample_vehicle, VIN)
        assert tracker.device_info["identifiers"] == {(DOMAIN, VIN)}
        assert tracker.device_info["manufacturer"] == "Polestar"

    def test_extra_state_attributes(self, sample_coordinator_data, sample_vehicle):
        tracker = _make_tracker(sample_coordinator_data, sample_vehicle, VIN)
        attrs = tracker.extra_state_attributes
        assert attrs is not None
        assert "location_timestamp" in attrs
        assert attrs["location_timestamp"] == "2026-03-08T17:14:18.845000+00:00"

    def test_none_when_no_location_data(self, sample_coordinator_data, sample_vehicle):
        sample_coordinator_data["location"] = {}
        tracker = _make_tracker(sample_coordinator_data, sample_vehicle, VIN)
        assert tracker.latitude is None
        assert tracker.longitude is None
        assert tracker.extra_state_attributes is None

    def test_none_when_no_coordinator_data(self, sample_vehicle):
        coordinator_data = None
        coordinator = MagicMock()
        coordinator.data = coordinator_data
        tracker = PolestarDeviceTracker(coordinator, sample_vehicle, VIN)
        assert tracker.latitude is None
        assert tracker.longitude is None
        assert tracker.extra_state_attributes is None

    def test_none_when_location_values_none(self, sample_coordinator_data, sample_vehicle):
        sample_coordinator_data["location"][VIN] = {
            "latitude": None,
            "longitude": None,
            "timestamp_ms": None,
        }
        tracker = _make_tracker(sample_coordinator_data, sample_vehicle, VIN)
        assert tracker.latitude is None
        assert tracker.longitude is None
        assert tracker.extra_state_attributes is None


class TestPolestarParkedDeviceTracker:
    def test_uses_parked_location_data(self, sample_coordinator_data, sample_vehicle):
        sample_coordinator_data["parked_location"] = {
            VIN: {"latitude": 50.8, "longitude": -0.1, "timestamp_ms": 1772990058845}
        }
        coordinator = MagicMock()
        coordinator.data = sample_coordinator_data
        tracker = PolestarParkedDeviceTracker(coordinator, sample_vehicle, VIN)

        assert tracker.latitude == 50.8
        assert tracker.longitude == -0.1
        assert tracker.unique_id == f"{VIN}_parked_location"
        assert tracker.translation_key == "parked_location"
        assert tracker.extra_state_attributes == {
            "location_timestamp": "2026-03-08T17:14:18.845000+00:00"
        }

    def test_unavailable_without_parked_location(self, sample_coordinator_data, sample_vehicle):
        coordinator = MagicMock()
        coordinator.data = sample_coordinator_data
        tracker = PolestarParkedDeviceTracker(coordinator, sample_vehicle, VIN)

        assert tracker.latitude is None
        assert tracker.longitude is None
        assert tracker.extra_state_attributes is None
