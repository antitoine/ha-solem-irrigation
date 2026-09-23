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

# Sensor inputs ---------------------------------------------------------------
# A module's sensors are "inputs". SOLEM's own web app identifies a flow meter
# by type (its bundle declares ``flowMeterSensors = [1]``).
INPUT_TYPE_FLOW_METER = 1

# Reading unit, as reported by an input's ``unit`` field. SOLEM stores volumes
# in whichever of these the meter was configured with and converts for display.
INPUT_UNIT_LITRE = 1
INPUT_UNIT_GALLON = 3

# "Last communication" ---------------------------------------------------------
# SOLEM reports a module's last contact under a different key depending on how
# that module reaches the cloud, and a module only ever carries one of them:
#
#   lastRadioCommunication  the LoRa gateway <-> controller radio contact. Only
#                           on LoRa children (lr-is, lr-ip, lr-mas...).
#   seenAt                  the module's own contact with the cloud. On modules
#                           that talk to it directly -- the LR-MB gateway
#                           (verified), and (unverified, no hardware to check)
#                           WiFi controllers such as the SMART-IS, which have no
#                           LoRa radio and so never report
#                           `lastRadioCommunication`. Kept fresh by
#                           `LIVE_MODULE_FIELDS` below.
#
# Tried in order, so a LoRa module keeps the radio timestamp it has always had.
LAST_COMMUNICATION_KEYS = ("lastRadioCommunication", "seenAt")

# Fields re-read on every poll from the cheap projection endpoint, and merged
# back into each module's record.
#
# The module page that first populates that record is well over a megabyte, so
# it is fetched once at setup -- which used to leave everything read from it
# frozen until the next reload: the gateway's `seenAt` (its only timestamp,
# since its state endpoint answers 503) and every battery reading. The
# projection returns these for a whole account in a few hundred bytes.
#
# Keep this list short and made only of *stored* columns; SOLEM's computed
# getters (`isBattery`, `isOnline`, `typeIs*`) cannot be projected and are
# simply absent from the reply. They are read at setup only, so that is fine.
LIVE_MODULE_FIELDS = (
    "seenAt",
    "lastRadioCommunication",
    "battery",
    "batteryVoltage",
    "batteryLow",
    # Not surfaced yet: the likely wired rain-sensor contact (issue #8). Free to
    # carry here, and it keeps a diagnostics dump honest about the live value.
    "sensorState",
)

# Minutes between ticks when an input does not declare its own ``interval``.
DEFAULT_INPUT_INTERVAL = 1

# How far back to ask for ticks when deriving the current flow rate. Only needs
# to cover a couple of ticks; a wider window is just a bigger response.
FLOW_WINDOW = timedelta(minutes=15)

# Treat the meter as idle once its newest tick is older than this. Must stay at
# or above the poll interval: ticks reach the cloud with ~80s of upload lag, so
# a tighter bound would intermittently report 0 during active watering.
FLOW_IDLE_AFTER = DEFAULT_SCAN_INTERVAL
