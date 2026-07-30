"""Structural and (optional) live conformity tests for the integration.

The structural tests need no credentials and guard the metadata that HACS, the
Home Assistant frontend and the manifest validators rely on. The single
``integration``-marked test talks to the real MySOLEM cloud and is skipped
unless credentials are provided in a ``.env`` file (see ``.env.example``).
"""

import json
import os
import re
from pathlib import Path

import aiohttp
import pytest
import yaml
from dotenv import load_dotenv
from homeassistant.util import dt as dt_util

from custom_components.solem_irrigation.api import SolemApiClient, apply_expression
from custom_components.solem_irrigation.const import (
    FLOW_WINDOW,
    INPUT_TYPE_FLOW_METER,
)

ROOT = Path(__file__).resolve().parent.parent
COMPONENT = ROOT / "custom_components" / "solem_irrigation"


def _load_json(relative: str) -> dict:
    return json.loads((COMPONENT / relative).read_text(encoding="utf-8"))


def test_manifest_is_well_formed():
    manifest = _load_json("manifest.json")
    assert manifest["domain"] == "solem_irrigation"
    assert manifest["config_flow"] is True
    assert manifest["integration_type"] == "hub"
    assert manifest["iot_class"] == "cloud_polling"
    # Version is semver-shaped (survives future bumps; not hardcoded here).
    assert re.fullmatch(r"\d+\.\d+\.\d+", manifest["version"])
    assert manifest["documentation"].startswith("https://")
    assert manifest["issue_tracker"].startswith("https://")
    assert manifest["codeowners"]


def test_hacs_manifest_present():
    hacs = json.loads((ROOT / "hacs.json").read_text(encoding="utf-8"))
    assert hacs["name"]
    assert "homeassistant" in hacs


def test_translations_mirror_strings_entities():
    """Every entity declared in strings.json is translated in each language."""
    strings = _load_json("strings.json")
    for language in ("en", "fr"):
        translation = _load_json(f"translations/{language}.json")
        assert set(translation["entity"]) == set(strings["entity"]), language
        for platform, entities in strings["entity"].items():
            assert set(translation["entity"][platform]) == set(entities), (
                language,
                platform,
            )


def test_services_yaml_matches_strings():
    """services.yaml and strings.json agree on the exposed services and fields."""
    strings = _load_json("strings.json")
    services = yaml.safe_load((COMPONENT / "services.yaml").read_text("utf-8"))
    assert set(services) == set(strings["services"])
    for service, spec in services.items():
        documented = set(strings["services"][service]["fields"])
        declared = set(spec.get("fields", {}))
        assert declared == documented, service


@pytest.mark.integration
async def test_live_login_and_discovery():
    """Smoke-test the real API: log in and list modules."""
    load_dotenv()
    email = os.getenv("email")
    password = os.getenv("password")
    region = os.getenv("region", "Europe")
    if not email or not password:
        pytest.skip("Missing MySOLEM credentials in .env")

    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar()) as session:
        client = SolemApiClient(session, email=email, password=password, region=region)
        user_id = await client.async_login()
        assert user_id
        module_ids = await client.async_get_module_ids()
        assert isinstance(module_ids, list)


@pytest.mark.integration
async def test_live_flow_meter_reading():
    """Read a real flow meter end to end, if the account has one.

    Guards the two things a mocked test cannot: that the module page still
    embeds ``var inputs``, and that the newest tick scales into a plausible
    volume via the meter's own expression.
    """
    load_dotenv()
    email = os.getenv("email")
    password = os.getenv("password")
    region = os.getenv("region", "Europe")
    if not email or not password:
        pytest.skip("Missing MySOLEM credentials in .env")

    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar()) as session:
        client = SolemApiClient(session, email=email, password=password, region=region)
        await client.async_login()
        meters: list[tuple[dict, dict]] = []
        for module_id in await client.async_get_module_ids():
            module, inputs = await client.async_get_module_page(module_id)
            # A module declaring inputs must yield them, or the scrape has rotted.
            if int(module.get("numberOfInputs", 0) or 0) > 0:
                assert inputs, f"module {module_id} declares inputs but none parsed"
            meters += [
                (module, record)
                for record in inputs
                if record.get("type") == INPUT_TYPE_FLOW_METER
            ]

        if not meters:
            pytest.skip("No flow meter on this account")

        for module, record in meters:
            tick = await client.async_get_last_input_tick(record["id"])
            assert tick, f"flow meter {record['id']} has never reported"
            volume = apply_expression(record.get("expression"), float(tick["value"]))
            assert volume is not None and volume >= 0
            assert dt_util.parse_datetime(tick["timestamp"]) is not None
            # The windowed series must agree with the record we scraped.
            now = dt_util.utcnow()
            series = await client.async_get_module_sensor_data(
                module["id"],
                (now - FLOW_WINDOW).isoformat().replace("+00:00", "Z"),
                now.isoformat().replace("+00:00", "Z"),
            )
            assert any(r.get("id") == record["id"] for r in series)
