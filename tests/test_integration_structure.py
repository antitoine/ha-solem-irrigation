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

from custom_components.solem_irrigation.api import SolemApiClient

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
