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

# On/off as the live state reports it, in ``status.watering``. SOLEM's web app
# reads ``state`` 0 (or the string "OFF") as off, and ``rainDelay`` as the days
# left before it turns back on by itself; anything else in ``state`` is on.
# ``rainDelay`` 255 is how the web app spells a permanent OFF on another family
# of modules, so it is read as permanent here too rather than as 255 days.
WATERING_STATES_OFF = (0, "OFF")
RAIN_DELAY_PERMANENT = 255

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
# A tipping-bucket rain gauge (``rainGaugeSensors = [14]`` in the same bundle):
# a cumulative counter of bucket tips, scaled to millimetres by its expression
# (``x*0.2794`` on an LR-MS). Not to be confused with type 2, the on/off rain
# *sensor* the bundle lists separately.
INPUT_TYPE_RAIN_GAUGE = 14

# What MySOLEM does to the controllers linked to a rain gauge once its daily
# threshold is crossed (``actionWhenHighDailyThresholdExceeded``). Only the codes
# a user has matched against the MySOLEM screen are named (#8); any other is
# passed through raw rather than guessed at.
THRESHOLD_ACTIONS = {0: "none", 6: "off_1_day"}

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

# Module types MySOLEM can only ever reach over a phone's Bluetooth, copied from
# the web app's own list. The cloud has no live link to them -- no gateway, no
# WiFi -- so their state endpoint answers 503 by design, and the web app never
# asks it. Only a fallback: SOLEM's own ``isBluetoothOnly`` flag on the module
# record is read first, and this covers a record that lacks it.
BLUETOOTH_ONLY_TYPES = frozenset(
    {
        "bl-ag",
        "bl-ip",
        "bl-ip-v2",
        "bl-is",
        "bl-nr-v2",
        "bl-ol",
        "bl-pc",
        "joro",
        "joro-v2",
    }
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
