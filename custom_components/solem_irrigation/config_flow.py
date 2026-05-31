"""Config flow for SOLEM irrigation."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import SolemApiClient, SolemAuthError, SolemConnectionError
from .const import (
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_REGION,
    DOMAIN,
    REGION_EUROPE,
    REGIONS,
)


class SolemConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for SOLEM irrigation."""

    VERSION = 1

    async def _validate(self, data: Mapping[str, Any]) -> tuple[str | None, dict]:
        """Try to log in. Return (user_id, errors)."""
        errors: dict[str, str] = {}
        session = async_create_clientsession(self.hass, cookie_jar=aiohttp.CookieJar())
        client = SolemApiClient(
            session,
            email=data[CONF_EMAIL],
            password=data[CONF_PASSWORD],
            region=data.get(CONF_REGION, REGION_EUROPE),
        )
        try:
            user_id = await client.async_login()
        except SolemAuthError:
            errors["base"] = "invalid_auth"
        except SolemConnectionError:
            errors["base"] = "cannot_connect"
        else:
            return user_id, errors
        return None, errors

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            user_id, errors = await self._validate(user_input)
            if user_id is not None:
                await self.async_set_unique_id(user_id)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=user_input[CONF_EMAIL], data=user_input
                )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_EMAIL): selector.TextSelector(
                        selector.TextSelectorConfig(
                            type=selector.TextSelectorType.EMAIL
                        )
                    ),
                    vol.Required(CONF_PASSWORD): selector.TextSelector(
                        selector.TextSelectorConfig(
                            type=selector.TextSelectorType.PASSWORD
                        )
                    ),
                    vol.Required(
                        CONF_REGION, default=REGION_EUROPE
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=REGIONS,
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Handle re-authentication when the session/credentials fail."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm re-authentication with a fresh password."""
        errors: dict[str, str] = {}
        reauth_entry = self._get_reauth_entry()
        if user_input is not None:
            data = {**reauth_entry.data, **user_input}
            user_id, errors = await self._validate(data)
            if user_id is not None:
                await self.async_set_unique_id(user_id)
                self._abort_if_unique_id_mismatch(reason="wrong_account")
                return self.async_update_reload_and_abort(
                    reauth_entry, data_updates=user_input
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PASSWORD): selector.TextSelector(
                        selector.TextSelectorConfig(
                            type=selector.TextSelectorType.PASSWORD
                        )
                    ),
                }
            ),
            description_placeholders={CONF_EMAIL: reauth_entry.data[CONF_EMAIL]},
            errors=errors,
        )
