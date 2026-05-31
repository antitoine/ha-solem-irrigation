"""Tests for the SOLEM global Stop button."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.solem_irrigation.button import (
    SolemStopButton,
    async_setup_entry,
)
from custom_components.solem_irrigation.coordinator import (
    SolemDataUpdateCoordinator,
    SolemModule,
    SolemStation,
)


@pytest.fixture
def module() -> SolemModule:
    return SolemModule(
        id="m1",
        name="Controller",
        serial="SER1",
        type="LR-IS",
        display_type="LR-IS",
        raw={"typeIsWatering": True},
        stations=[SolemStation(id="s1", name="Pelouse 1", index=1)],
        programs=[],
    )


@pytest.fixture
def coordinator(module) -> MagicMock:
    coord = MagicMock(spec=SolemDataUpdateCoordinator)
    coord.modules = {module.id: module}
    return coord


async def test_press_sends_stop(coordinator, module):
    coordinator.async_command_stop = AsyncMock()
    button = SolemStopButton(coordinator, module)
    assert button.unique_id == "m1_stop"
    await button.async_press()
    coordinator.async_command_stop.assert_awaited_once_with(module)


async def test_async_setup_entry_one_per_controller(coordinator, module):
    pool = SolemModule(
        id="pool",
        name="Pool",
        serial="SER2",
        type="LR-PC",
        display_type="LR-PC",
        raw={},
        stations=[SolemStation(id="x", name="x", index=1)],
        programs=[],
    )
    coordinator.modules = {module.id: module, pool.id: pool}
    entry = MagicMock()
    entry.runtime_data = coordinator
    added: list = []

    await async_setup_entry(MagicMock(), entry, added.extend)

    assert [b.unique_id for b in added] == ["m1_stop"]
