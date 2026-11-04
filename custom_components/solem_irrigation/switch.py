"""Switch platform for SOLEM irrigation.

A single master switch per controller enables / disables it. Turning it off
disables permanently; the ``solem_irrigation.set_enabled`` service additionally
accepts ``days`` to disable for a period (the rain-delay path).

Both controller-level services are registered here (the enable switch is the
single entity that represents the controller):

* ``solem_irrigation.set_enabled`` — global on/off (+ optional rain-delay days).
* ``solem_irrigation.run`` — stop, run a program, or run a station for a
  specific duration (the stations are also exposed as valves for the common
  "run for the usual time" case).
* ``solem_irrigation.set_program_schedule`` — edit a stored program.
* ``solem_irrigation.push_programs`` — push the programs to the controller now.
* ``solem_irrigation.create_backup`` — back the programs up in the cloud.

Each stored program also gets a switch of its own that turns it on or off.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import voluptuous as vol
from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_platform
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.typing import VolDictType

from . import schedule
from .api import SolemError, SolemValidationError
from .const import (
    ATTR_DAYS,
    ATTR_DURATION,
    ATTR_DURATIONS,
    ATTR_ENABLED,
    ATTR_EVERY_DAYS,
    ATTR_MODE,
    ATTR_NAME,
    ATTR_OFFSET_DAYS,
    ATTR_PROGRAM,
    ATTR_PUSH,
    ATTR_SCHEDULE_TYPE,
    ATTR_START_TIMES,
    ATTR_STATION,
    ATTR_WATER_BUDGET,
    ATTR_WEEKDAYS,
    ATTR_WINDOWS,
    MAX_RAIN_DELAY_DAYS,
    MAX_RUN_MINUTES,
    MIN_RUN_MINUTES,
    MODE_PROGRAM,
    MODE_STOP,
    RUN_MODES,
    SERVICE_CREATE_BACKUP,
    SERVICE_PUSH_PROGRAMS,
    SERVICE_RUN,
    SERVICE_SET_ENABLED,
    SERVICE_SET_PROGRAM_SCHEDULE,
)
from .coordinator import (
    SolemConfigEntry,
    SolemDataUpdateCoordinator,
    SolemModule,
    SolemProgram,
)
from .entity import SolemModuleEntity

# Bounded, which also refuses inf and nan.
_SOAK_MINUTES = vol.All(
    vol.Coerce(float), vol.Range(min=0, max=schedule.MINUTES_PER_DAY)
)
_STATION_MINUTES = vol.All(
    vol.Coerce(float), vol.Range(min=0, max=schedule.MAX_STATION_MINUTES)
)

_WINDOW_SCHEMA = vol.Schema(
    {
        vol.Required("start"): cv.string,
        vol.Required("end"): cv.string,
        vol.Optional("soak_on_minutes"): _SOAK_MINUTES,
        vol.Optional("soak_off_minutes"): _SOAK_MINUTES,
    }
)

_SET_PROGRAM_SCHEDULE_SCHEMA: VolDictType = {
    vol.Required(ATTR_PROGRAM): cv.string,
    vol.Optional(ATTR_SCHEDULE_TYPE): vol.In(
        [
            schedule.SCHEDULE_TYPE_WEEKLY,
            schedule.SCHEDULE_TYPE_EVEN_DAYS,
            schedule.SCHEDULE_TYPE_ODD_DAYS,
            schedule.SCHEDULE_TYPE_ODD_DAYS_NOT_31,
            schedule.SCHEDULE_TYPE_INTERVAL,
        ]
    ),
    vol.Optional(ATTR_WEEKDAYS): vol.All(
        cv.ensure_list, [vol.In(schedule.WEEKDAY_KEYS)]
    ),
    vol.Optional(ATTR_EVERY_DAYS): vol.All(
        vol.Coerce(int), vol.Range(min=1, max=schedule.MAX_INTERVAL_DAYS)
    ),
    vol.Optional(ATTR_OFFSET_DAYS): vol.All(
        vol.Coerce(int), vol.Range(min=0, max=schedule.MAX_OFFSET_DAYS)
    ),
    vol.Optional(ATTR_WATER_BUDGET): vol.All(
        vol.Coerce(int), vol.Range(min=0, max=schedule.MAX_WATER_BUDGET)
    ),
    vol.Optional(ATTR_ENABLED): cv.boolean,
    vol.Optional(ATTR_START_TIMES): vol.All(cv.ensure_list, [cv.string]),
    vol.Optional(ATTR_DURATIONS): {cv.string: _STATION_MINUTES},
    vol.Optional(ATTR_WINDOWS): vol.All(cv.ensure_list, [_WINDOW_SCHEMA]),
    vol.Optional(ATTR_PUSH, default=True): cv.boolean,
}

# The controller-level actions are the Irrigation enabled switch's. A program
# switch carries a device class, so it is left out of an action aimed at the
# whole device (and refused when named), which would otherwise run once per
# program.
_CONTROLLER_SWITCH = [None]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SolemConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the SOLEM switches and the controller-level services."""
    coordinator = entry.runtime_data
    modules = coordinator.modules.values()
    entities: list[SwitchEntity] = [
        SolemEnableSwitch(coordinator, module)
        for module in modules
        if module.is_controller
    ]
    # A Bluetooth-only controller gets them too: the cloud edit works, only
    # the push to the controller is skipped.
    entities.extend(
        SolemProgramEnabledSwitch(coordinator, module, program)
        for module in modules
        if module.has_schedule
        for program in module.programs
    )
    async_add_entities(entities)

    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        SERVICE_SET_ENABLED,
        {
            vol.Required(ATTR_ENABLED): cv.boolean,
            vol.Optional(ATTR_DAYS): vol.All(
                vol.Coerce(int), vol.Range(min=0, max=MAX_RAIN_DELAY_DAYS)
            ),
        },
        "async_set_enabled",
        entity_device_classes=_CONTROLLER_SWITCH,
    )
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
        entity_device_classes=_CONTROLLER_SWITCH,
    )
    platform.async_register_entity_service(
        SERVICE_SET_PROGRAM_SCHEDULE,
        _SET_PROGRAM_SCHEDULE_SCHEMA,
        "async_set_program_schedule",
        entity_device_classes=_CONTROLLER_SWITCH,
    )
    platform.async_register_entity_service(
        SERVICE_PUSH_PROGRAMS,
        None,
        "async_push_programs",
        entity_device_classes=_CONTROLLER_SWITCH,
    )
    platform.async_register_entity_service(
        SERVICE_CREATE_BACKUP,
        {vol.Optional(ATTR_NAME): cv.string},
        "async_create_backup",
        entity_device_classes=_CONTROLLER_SWITCH,
    )


class SolemEnableSwitch(SolemModuleEntity, SwitchEntity, RestoreEntity):
    """Enable / disable a controller.

    Follows the ON/OFF status the cloud reports, so an OFF set from MySOLEM --
    by hand, or by a rain gauge crossing its threshold -- shows up here too.
    A controller whose state carries no status falls back to an assumed state,
    the last one set from Home Assistant, restored across restarts.
    """

    _attr_icon = "mdi:power"
    _attr_translation_key = "enabled"

    def __init__(
        self, coordinator: SolemDataUpdateCoordinator, module: SolemModule
    ) -> None:
        """Initialise the master enable switch."""
        super().__init__(coordinator, module)
        self._attr_unique_id = f"{module.id}_enabled"
        self._is_on = True

    async def async_added_to_hass(self) -> None:
        """Restore the last enabled state."""
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            self._is_on = last.state == "on"

    @property
    def assumed_state(self) -> bool:
        """True while the cloud reports no ON/OFF status for this controller."""
        return self.coordinator.watering_enabled(self._module.id) is None

    @property
    def is_on(self) -> bool:
        """Return the reported enabled state, else the assumed one."""
        if (enabled := self.coordinator.watering_enabled(self._module.id)) is None:
            return self._is_on
        return enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable the controller."""
        await self.async_set_enabled(enabled=True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable the controller permanently."""
        await self.async_set_enabled(enabled=False, days=0)

    async def async_set_enabled(self, enabled: bool, days: int = 0) -> None:
        """Apply the global on/off command.

        ``enabled=True`` turns the controller on. ``enabled=False`` disables it:
        ``days=0`` is permanent, ``days>0`` disables it for that many days (the
        rain-delay). Exposed both via the switch toggle and the
        ``solem_irrigation.set_enabled`` service.
        """
        await self.coordinator.async_command_set_status(
            self._module, enabled=enabled, days=days
        )
        self._is_on = enabled
        self.async_write_ha_state()

    async def async_handle_run(
        self,
        mode: str,
        program: str | None = None,
        station: str | None = None,
        duration: int | None = None,
    ) -> None:
        """Handle the ``solem_irrigation.run`` service for this controller."""
        module = self._module
        coordinator = self.coordinator
        if mode == MODE_STOP:
            await coordinator.async_command_stop(module)
            return
        if mode == MODE_PROGRAM:
            if not program:
                raise ServiceValidationError(
                    "The 'program' field is required when mode is 'program'."
                )
            resolved = module.find_program(program)
            if resolved is None:
                raise ServiceValidationError(f"Unknown program: {program}")
            await coordinator.async_command_run_program(module, resolved)
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
            # Explicit duration becomes this station's remembered default.
            await coordinator.async_set_run_minutes(resolved.id, duration)
            minutes = duration
        else:
            minutes = coordinator.get_run_minutes(resolved.id)
        await coordinator.async_command_run_station(module, resolved, minutes)

    async def async_set_program_schedule(self, **kwargs: Any) -> None:
        """Handle the ``solem_irrigation.set_program_schedule`` service.

        With both schedule fields and ``enabled``, the edit holds its push so
        the two writes share one.
        """
        program: str = kwargs.pop(ATTR_PROGRAM)
        push: bool = kwargs.pop(ATTR_PUSH, True)
        enabled: bool | None = kwargs.pop(ATTR_ENABLED, None)
        changes = {k: v for k, v in kwargs.items() if v is not None}
        if not changes and enabled is None:
            raise ServiceValidationError(
                "Nothing to change: give schedule fields and/or 'enabled'."
            )
        with _service_errors():
            if changes:
                await self.coordinator.async_command_update_program(
                    self._module, program, changes, push=push and enabled is None
                )
            if enabled is not None:
                await self.coordinator.async_command_set_program_enabled(
                    self._module, program, enabled, push=push
                )

    async def async_push_programs(self) -> None:
        """Handle the ``solem_irrigation.push_programs`` service."""
        with _service_errors():
            await self.coordinator.async_command_push_programs(self._module)

    async def async_create_backup(self, name: str | None = None) -> None:
        """Handle the ``solem_irrigation.create_backup`` service."""
        with _service_errors():
            await self.coordinator.async_command_create_backup(self._module, name)


class SolemProgramEnabledSwitch(SolemModuleEntity, SwitchEntity):
    """Turn one stored program on or off.

    There is no enabled flag on the wire: off parks the program the way the
    portal does (weekly, no day), after saving its recurrence, and on restores
    it. The state is read back from the program record, so an edit made in
    MySOLEM shows up here too.
    """

    _attr_translation_key = "prog_enabled"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_device_class = SwitchDeviceClass.SWITCH

    def __init__(
        self,
        coordinator: SolemDataUpdateCoordinator,
        module: SolemModule,
        program: SolemProgram,
    ) -> None:
        """Initialise the switch of one program."""
        super().__init__(coordinator, module)
        self._program_id = program.id
        self._attr_unique_id = f"{module.id}_prog_{program.id}"
        self._attr_translation_placeholders = {"program": program.name}

    def _program(self) -> SolemProgram | None:
        return next(
            (p for p in self._module.programs if p.id == self._program_id), None
        )

    @property
    def available(self) -> bool:
        """Unavailable once the program is gone from the module."""
        return super().available and self._program() is not None

    @property
    def is_on(self) -> bool:
        """True while the program can run."""
        if (program := self._program()) is None:
            return False
        try:
            return schedule.program_is_enabled(program.raw)
        except TypeError, ValueError:
            return False

    @property
    def icon(self) -> str:
        """A calendar, ticked while the program can run."""
        return "mdi:calendar-check" if self.is_on else "mdi:calendar-remove"

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Bring the program's schedule back."""
        await self._async_set_enabled(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Park the program: it stays stored but never runs."""
        await self._async_set_enabled(False)

    async def _async_set_enabled(self, enabled: bool) -> None:
        with _service_errors():
            await self.coordinator.async_command_set_program_enabled(
                self._module, self._program_id, enabled
            )
        self.async_write_ha_state()


@contextmanager
def _service_errors() -> Iterator[None]:
    """Map a program-editing failure to the error Home Assistant should show.

    Input the controller would refuse, or the cloud refused, is the user's to
    fix; anything else is a failure to reach MySOLEM.
    """
    try:
        yield
    except (ValueError, SolemValidationError) as err:
        # ScheduleError is a ValueError.
        raise ServiceValidationError(str(err)) from err
    except SolemError as err:
        raise HomeAssistantError(str(err)) from err
