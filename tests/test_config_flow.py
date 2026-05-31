"""Tests for the SOLEM irrigation config flow."""

from unittest.mock import patch

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.solem_irrigation.api import SolemAuthError, SolemConnectionError
from custom_components.solem_irrigation.const import (
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_REGION,
    DOMAIN,
)

USER_ID = "a1b2c3d4e5f6a1b2c3d4e5f6"
OTHER_ID = "ffffffffffffffffffffffff"
USER_INPUT = {CONF_EMAIL: "e@x.com", CONF_PASSWORD: "pw", CONF_REGION: "Europe"}

_LOGIN = "custom_components.solem_irrigation.config_flow.SolemApiClient.async_login"
_SETUP = "custom_components.solem_irrigation.async_setup_entry"


async def test_form_success(hass: HomeAssistant) -> None:
    """A valid login creates a config entry titled with the email."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {}

    with (
        patch(_LOGIN, return_value=USER_ID),
        patch(_SETUP, return_value=True),
    ):
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )
        await hass.async_block_till_done()

    assert result2["type"] == FlowResultType.CREATE_ENTRY
    assert result2["title"] == "e@x.com"
    assert result2["data"] == USER_INPUT
    assert result2["result"].unique_id == USER_ID


@pytest.mark.parametrize(
    ("exception", "expected_error"),
    [
        (SolemAuthError, "invalid_auth"),
        (SolemConnectionError, "cannot_connect"),
    ],
)
async def test_form_errors(
    hass: HomeAssistant, exception: type[Exception], expected_error: str
) -> None:
    """Login failures map to the expected base error."""
    with patch(_LOGIN, side_effect=exception("boom")):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_USER},
            data=USER_INPUT,
        )
        await hass.async_block_till_done()

    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": expected_error}


async def test_form_already_configured(hass: HomeAssistant) -> None:
    """A second entry for the same MySOLEM account aborts."""
    MockConfigEntry(domain=DOMAIN, unique_id=USER_ID, data=USER_INPUT).add_to_hass(hass)

    with patch(_LOGIN, return_value=USER_ID):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_USER},
            data=USER_INPUT,
        )

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth_success(hass: HomeAssistant) -> None:
    """A successful re-auth updates the stored password and reloads."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id=USER_ID, data=USER_INPUT)
    entry.add_to_hass(hass)

    result = await entry.start_reauth_flow(hass)
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    with (
        patch(_LOGIN, return_value=USER_ID),
        patch(_SETUP, return_value=True),
    ):
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PASSWORD: "new-password"}
        )
        await hass.async_block_till_done()

    assert result2["type"] == FlowResultType.ABORT
    assert result2["reason"] == "reauth_successful"
    assert entry.data[CONF_PASSWORD] == "new-password"


async def test_reauth_wrong_account(hass: HomeAssistant) -> None:
    """Re-authenticating into a different account aborts with wrong_account."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id=USER_ID, data=USER_INPUT)
    entry.add_to_hass(hass)

    result = await entry.start_reauth_flow(hass)
    with patch(_LOGIN, return_value=OTHER_ID):
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PASSWORD: "whatever"}
        )

    assert result2["type"] == FlowResultType.ABORT
    assert result2["reason"] == "wrong_account"
