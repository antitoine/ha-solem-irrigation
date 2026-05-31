"""Select platform for SOLEM irrigation.

A single "Manual run" select per controller is the one manual command: its
options are ``Stop`` plus every stored program and every station. Picking an
option triggers it (stations run for the remembered duration). The same action
is also exposed as the ``solem_irrigation.run`` service for automations, which
additionally accepts an explicit ``duration``.
"""

from __future__ import annotations

import voluptuous as vol

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv, entity_platform
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    ATTR_DURATION,
    ATTR_MODE,
    ATTR_PROGRAM,
    ATTR_STATION,
    MAX_RUN_MINUTES,
    MIN_RUN_MINUTES,
    MODE_PROGRAM,
    MODE_STATION,
    MODE_STOP,
    RUN_MODES,
    SERVICE_RUN,
)
from .coordinator import (
    SolemConfigEntry,
    SolemDataUpdateCoordinator,
    SolemModule,
    SolemProgram,
    SolemStation,
)
from .entity import SolemModuleEntity

# Option labels. Fixed (not translated) so the stored state stays stable across
# UI languages; programs/stations are user-named, prefixed to disambiguate.
OPTION_STOP = "Stop"
PROGRAM_PREFIX = "Program: "
STATION_PREFIX = "Station: "


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SolemConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the SOLEM "Manual run" select and the ``run`` service."""
    coordinator = entry.runtime_data
    async_add_entities(
        SolemManualRunSelect(coordinator, module)
        for module in coordinator.modules.values()
        if module.is_controller
    )

    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        SERVICE_RUN,
        {
            vol.Required(ATTR_MODE): vol.In(RUN_MODES),
            vol.Optional(ATTR_PROGRAM): cv.string,
            vol.Optional(ATTR_STATION): cv.string,
            vol.Optional(ATTR_DURATION): vol.All(
                vol.Coerce(int), vol.Range(min=MIN_RUN_MINUTES, max=MAX_RUN_MINUTES)
            ),
        },
        "async_handle_run",
    )


class SolemManualRunSelect(SolemModuleEntity, SelectEntity):
    """The single manual command: stop, run a program, or run a station."""

    _attr_translation_key = "manual_run"
    _attr_icon = "mdi:water"

    def __init__(
        self, coordinator: SolemDataUpdateCoordinator, module: SolemModule
    ) -> None:
        """Initialise the manual-run select."""
        super().__init__(coordinator, module)
        self._attr_unique_id = f"{module.id}_manual_run"

    @staticmethod
    def _program_option(program: SolemProgram) -> str:
        return f"{PROGRAM_PREFIX}{program.name}"

    @staticmethod
    def _station_option(station: SolemStation) -> str:
        return f"{STATION_PREFIX}{station.name}"

    @property
    def options(self) -> list[str]:
        """Stop, then every program, then every station."""
        module = self._module
        return [
            OPTION_STOP,
            *(self._program_option(p) for p in module.programs),
            *(self._station_option(s) for s in module.stations),
        ]

    @property
    def current_option(self) -> str:
        """Reflect the running station; ``Stop`` when idle.

        Running *programs* are not reliably reported, so a program run shows as
        its station once watering starts (and ``Stop`` until then).
        """
        index = self.coordinator.running_station_index(self._module_id)
        if index > 0:
            for station in self._module.stations:
                if station.index == index:
                    return self._station_option(station)
        return OPTION_STOP

    async def async_select_option(self, option: str) -> None:
        """Trigger the picked option (stations use the remembered duration)."""
        module = self._module
        if option == OPTION_STOP:
            await self._stop()
            return
        for program in module.programs:
            if self._program_option(program) == option:
                await self._run_program(program)
                return
        for station in module.stations:
            if self._station_option(station) == option:
                await self._run_station(
                    station, self.coordinator.get_run_minutes(self._module_id)
                )
                return
        raise ServiceValidationError(f"Unknown option: {option}")

    async def async_handle_run(
        self,
        mode: str,
        program: str | None = None,
        station: str | None = None,
        duration: int | None = None,
    ) -> None:
        """Handle the ``solem_irrigation.run`` service for this controller."""
        module = self._module
        if mode == MODE_STOP:
            await self._stop()
            return
        if mode == MODE_PROGRAM:
            if not program:
                raise ServiceValidationError(
                    "The 'program' field is required when mode is 'program'."
                )
            resolved = module.find_program(program)
            if resolved is None:
                raise ServiceValidationError(f"Unknown program: {program}")
            await self._run_program(resolved)
            return
        # mode == MODE_STATION
        if not station:
            raise ServiceValidationError(
                "The 'station' field is required when mode is 'station'."
            )
        resolved = module.find_station(station)
        if resolved is None:
            raise ServiceValidationError(f"Unknown station: {station}")
        if duration is not None:
            # Explicit duration becomes the remembered default for next time.
            await self.coordinator.async_set_run_minutes(self._module_id, duration)
            minutes = duration
        else:
            minutes = self.coordinator.get_run_minutes(self._module_id)
        await self._run_station(resolved, minutes)

    # -- shared command paths (optimistic + reconcile) ---------------------

    async def _stop(self) -> None:
        await self.coordinator.client.async_stop(self._serial)
        self.coordinator.apply_optimistic_running_station(self._module_id, 0)
        self.coordinator.async_schedule_refresh()

    async def _run_program(self, program: SolemProgram) -> None:
        await self.coordinator.client.async_run_program(self._serial, program.id)
        self.coordinator.async_schedule_refresh()

    async def _run_station(self, station: SolemStation, minutes: int) -> None:
        await self.coordinator.client.async_run_station(
            self._serial, station.id, minutes
        )
        self.coordinator.apply_optimistic_running_station(
            self._module_id, station.index
        )
        self.coordinator.async_schedule_refresh()
