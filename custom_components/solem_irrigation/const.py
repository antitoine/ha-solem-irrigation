"""Constants for the SOLEM irrigation integration."""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "solem_irrigation"
MANUFACTURER = "SOLEM"

# Config keys
CONF_EMAIL = "email"
CONF_PASSWORD = "password"  # noqa: S105
CONF_REGION = "region"

# Regions -> base URL + the value posted in the login form's "country-select".
REGION_EUROPE = "Europe"
REGION_AMERICA = "America"
REGIONS = [REGION_EUROPE, REGION_AMERICA]

BASE_URLS = {
    REGION_EUROPE: "https://mysolem.com",
    REGION_AMERICA: "https://us.mysolem.com",
}

# Polling. LoRa is eventually-consistent (cloud -> gateway -> duty-cycled
# downlink), so polling faster than this buys nothing and just hammers a cloud
# we do not own.
DEFAULT_SCAN_INTERVAL = timedelta(minutes=5)

# Manual run duration (minutes) used when a station switch is turned on.
DEFAULT_RUN_MINUTES = 5
MIN_RUN_MINUTES = 1
MAX_RUN_MINUTES = 12 * 60  # the controller takes hours + minutes (max 12h)

# Rain-delay (a.k.a. "off for N days"); 0 days == enabled.
MAX_RAIN_DELAY_DAYS = 30

# Command vocabulary for POST /module/sendManualModuleCommand
CMD_STOP = "manualStop"
CMD_STATION = "station"
CMD_PROGRAM = "program"
CMD_STATUS = "status"
STATUS_ON = "on"
STATUS_OFF = "off"
