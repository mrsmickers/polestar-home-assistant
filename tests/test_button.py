"""Tests for Polestar remote command buttons."""

from unittest.mock import AsyncMock, MagicMock

import grpc
import pytest
from homeassistant.const import Platform
from homeassistant.exceptions import HomeAssistantError

from custom_components.polestar_soc import PLATFORMS
from custom_components.polestar_soc.button import (
    PolestarFlashLightsButton,
    PolestarHonkFlashButton,
    PolestarStartChargingButton,
    PolestarStopChargingButton,
    PolestarUnlockTrunkButton,
    PolestarWarmCarButton,
)
from custom_components.polestar_soc.cep import CepError
from custom_components.polestar_soc.const import DOMAIN
from custom_components.polestar_soc.pccs import PccsError

VIN = "YSMYKEAE1RB000001"


def test_button_platform_is_forwarded():
    assert Platform.BUTTON in PLATFORMS


def _make_button(button_class, sample_vehicle):
    coordinator = MagicMock()
    coordinator.data = {"vehicles": [sample_vehicle]}
    coordinator.async_request_refresh = AsyncMock()
    button = button_class(coordinator, sample_vehicle, VIN)
    button.hass = MagicMock()
    button.hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *args: fn(*args))
    return button


@pytest.mark.parametrize(
    ("button_class", "translation_key"),
    (
        (PolestarWarmCarButton, "warm_car"),
        (PolestarStartChargingButton, "start_charging"),
        (PolestarStopChargingButton, "stop_charging"),
        (PolestarFlashLightsButton, "flash_lights"),
        (PolestarHonkFlashButton, "honk_and_flash"),
        (PolestarUnlockTrunkButton, "unlock_trunk"),
    ),
)
def test_button_identity(button_class, translation_key, sample_vehicle):
    button = _make_button(button_class, sample_vehicle)
    assert button.unique_id == f"{VIN}_{translation_key}"
    assert button.translation_key == translation_key
    assert button.device_info["identifiers"] == {(DOMAIN, VIN)}


@pytest.mark.asyncio
async def test_warm_car_starts_climate_at_22_degrees(sample_vehicle):
    button = _make_button(PolestarWarmCarButton, sample_vehicle)

    await button.async_press()

    button.coordinator.pccs.climatization_start.assert_called_once_with(VIN, 22.0)
    button.coordinator.async_request_refresh.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("button_class", "method_name", "args"),
    (
        (PolestarStartChargingButton, "start_charging", (VIN,)),
        (PolestarStopChargingButton, "stop_charging", (VIN,)),
        (PolestarFlashLightsButton, "honk_flash", (VIN, 2)),
        (PolestarHonkFlashButton, "honk_flash", (VIN, 0)),
        (PolestarUnlockTrunkButton, "unlock_trunk", (VIN,)),
    ),
)
async def test_cep_button_calls_expected_command(button_class, method_name, args, sample_vehicle):
    button = _make_button(button_class, sample_vehicle)

    await button.async_press()

    getattr(button.coordinator.cep, method_name).assert_called_once_with(*args)
    button.coordinator.async_request_refresh.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    (
        grpc.RpcError(),
        CepError("backend detail must not leak"),
        PccsError("backend detail must not leak"),
        RuntimeError("unexpected detail must not leak"),
    ),
)
async def test_button_errors_are_sanitised(error, sample_vehicle):
    button = _make_button(PolestarWarmCarButton, sample_vehicle)
    button.hass.async_add_executor_job = AsyncMock(side_effect=error)

    with pytest.raises(HomeAssistantError, match="Warm car command failed") as exc_info:
        await button.async_press()

    assert exc_info.value.__cause__ is None
    assert "backend detail" not in str(exc_info.value)
    assert "unexpected detail" not in str(exc_info.value)
    button.coordinator.async_request_refresh.assert_not_called()
