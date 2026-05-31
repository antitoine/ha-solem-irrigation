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

# Services (Actions) -----------------------------------------------------------
# Global on/off command, folding the rain-delay ("off for N days") in.
SERVICE_SET_ENABLED = "set_enabled"
ATTR_ENABLED = "enabled"
ATTR_DAYS = "days"

# Unified manual command: stop / run a program / run a station for a duration.
SERVICE_RUN = "run"
ATTR_MODE = "mode"
ATTR_PROGRAM = "program"
ATTR_STATION = "station"
ATTR_DURATION = "duration"

MODE_STOP = "stop"
MODE_PROGRAM = "program"
MODE_STATION = "station"
RUN_MODES = [MODE_STOP, MODE_PROGRAM, MODE_STATION]

# Storage key for the remembered manual-run duration (replaces the old
# "Run duration" number entity, which persisted via RestoreNumber).
STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}.run_minutes"
