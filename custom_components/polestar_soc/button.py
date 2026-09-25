"""Button platform for Polestar remote vehicle commands."""

from __future__ import annotations

from abc import abstractmethod

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import PolestarCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Polestar remote command buttons."""
    coordinator: PolestarCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[ButtonEntity] = []
    for vehicle in coordinator.data.get("vehicles", []):
        vin = vehicle["vin"]
        entities.extend(
            (
                PolestarWarmCarButton(coordinator, vehicle, vin),
                PolestarStartChargingButton(coordinator, vehicle, vin),
                PolestarStopChargingButton(coordinator, vehicle, vin),
                PolestarFlashLightsButton(coordinator, vehicle, vin),
                PolestarHonkFlashButton(coordinator, vehicle, vin),
                PolestarUnlockTrunkButton(coordinator, vehicle, vin),
            )
        )
    async_add_entities(entities)


class PolestarRemoteButton(CoordinatorEntity[PolestarCoordinator], ButtonEntity):
    """Base class for one-shot Polestar remote commands."""

    _attr_has_entity_name = True
    command_label = "Remote"

    def __init__(
        self,
        coordinator: PolestarCoordinator,
        vehicle: dict,
        vin: str,
    ) -> None:
        super().__init__(coordinator)
        self._vin = vin
        self._attr_unique_id = f"{vin}_{self.translation_key}"

        model_name = vehicle.get("modelName") or "Polestar"
        year = vehicle.get("modelYear", "")
        device_name = f"{model_name} ({year})" if year else model_name
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, vin)},
            name=device_name,
            manufacturer="Polestar",
            model=model_name,
            sw_version=str(year) if year else None,
        )

    @abstractmethod
    def _command(self) -> object:
        """Execute the blocking backend command."""

    async def async_press(self) -> None:
        """Execute the command and refresh vehicle state."""
        try:
            await self.hass.async_add_executor_job(self._command)
        except Exception:
            raise HomeAssistantError(f"{self.command_label} command failed") from None
        await self.coordinator.async_request_refresh()


class PolestarWarmCarButton(PolestarRemoteButton):
    """Start cabin pre-conditioning at 22°C."""

    _attr_translation_key = "warm_car"
    _attr_icon = "mdi:car-seat-heater"
    command_label = "Warm car"

    def _command(self) -> object:
        return self.coordinator.pccs.climatization_start(self._vin, 22.0)


class PolestarStartChargingButton(PolestarRemoteButton):
    """Start charging by overriding an active schedule."""

    _attr_translation_key = "start_charging"
    _attr_icon = "mdi:ev-station"
    command_label = "Start charging"

    def _command(self) -> object:
        return self.coordinator.cep.start_charging(self._vin)


class PolestarStopChargingButton(PolestarRemoteButton):
    """Stop the active charge-schedule override."""

    _attr_translation_key = "stop_charging"
    _attr_icon = "mdi:ev-station-off"
    command_label = "Stop charging"

    def _command(self) -> object:
        return self.coordinator.cep.stop_charging(self._vin)


class PolestarFlashLightsButton(PolestarRemoteButton):
    """Flash the vehicle lights."""

    _attr_translation_key = "flash_lights"
    _attr_icon = "mdi:car-light-high"
    command_label = "Flash lights"

    def _command(self) -> object:
        return self.coordinator.cep.honk_flash(self._vin, 2)


class PolestarHonkFlashButton(PolestarRemoteButton):
    """Honk and flash the vehicle lights."""

    _attr_translation_key = "honk_and_flash"
    _attr_icon = "mdi:bullhorn"
    command_label = "Honk and flash"

    def _command(self) -> object:
        return self.coordinator.cep.honk_flash(self._vin, 0)


class PolestarUnlockTrunkButton(PolestarRemoteButton):
    """Unlock the vehicle trunk only."""

    _attr_translation_key = "unlock_trunk"
    _attr_icon = "mdi:car-back"
    command_label = "Unlock trunk"

    def _command(self) -> object:
        return self.coordinator.cep.unlock_trunk(self._vin)
