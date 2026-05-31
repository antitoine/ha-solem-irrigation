# SOLEM Irrigation for Home Assistant

[![Validate](https://github.com/antitoine/ha-solem-irrigation/actions/workflows/validate.yml/badge.svg)](https://github.com/antitoine/ha-solem-irrigation/actions/workflows/validate.yml)
[![Test](https://github.com/antitoine/ha-solem-irrigation/actions/workflows/test.yml/badge.svg)](https://github.com/antitoine/ha-solem-irrigation/actions/workflows/test.yml)
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Buy Me A Coffee](https://img.shields.io/badge/Buy%20Me%20A%20Coffee-donate-FFDD00?logo=buymeacoffee&logoColor=black)](https://buymeacoffee.com/antitoine)

A custom [Home Assistant](https://www.home-assistant.io/) integration for
**SOLEM** connected irrigation controllers (LR-IS / LR-IP and similar LoRa
modules) reached through a SOLEM **LR-MB** WiFi gateway and the **MySOLEM**
cloud.

It talks to the same cloud backend as the [mysolem.com](https://mysolem.com)
web app (a session-cookie API), so it works for LoRa controllers that the
Bluetooth-only community integrations cannot reach.

> ⚠️ **Unofficial.** This integration is not affiliated with or endorsed by
> SOLEM. It uses a private API reverse-engineered from the MySOLEM web app,
> which may change at any time. Use at your own risk.

## Features

The integration follows the standard Home Assistant irrigation model (the same
one the Hunter Hydrawise integration uses). For each irrigation controller on
your account:

| Entity | What it does |
| --- | --- |
| **Valve** per station | Open it to water that station for its *Run duration* (the controller stops it automatically); close it to stop. Named after the station in MySOLEM. Only one is ever open — the controller waters one station at a time. |
| **Number** *Run duration* per station | How long opening that station's valve runs it (minutes). Each station keeps its own. Shown under *Configuration*. |
| **Select** *Run program* | Pick a stored program to start it now. |
| **Button** *Stop watering* | Stop anything running — a manual station run or a program. |
| **Switch** *Irrigation enabled* | Turn the controller on, or off permanently (assumed state). |
| **Number** *Rain delay* | Disable for N days (0 = enabled) — SOLEM's "Report de pluie". |
| **Sensor** *Watering station* | Name of the station currently watering (idle = none). |
| **Sensor** *Last communication* | Last radio contact with the module. |
| **Sensor** *Battery* | Battery indicator (battery-powered modules). |

### Actions (services)

| Action | What it does |
| --- | --- |
| `solem_irrigation.run` | Stop, run a **program**, or run a station for a **specific** duration: `mode` (`stop` / `program` / `station`) plus `program`, `station`, and `duration` (minutes). An explicit `duration` is remembered for the next run. Programs are run through this action (no per-program entity). |
| `solem_irrigation.set_enabled` | The on/off command: `enabled` (true/false) plus an optional `days` to disable for a period (rain delay; `0`/omitted = permanent). |

Opening a station valve runs it for the remembered duration (default 5 min until
you set one); use `solem_irrigation.run` to run for a specific duration, which
then becomes the new remembered default.

Every module (including the gateway) appears as a Home Assistant **device**;
controllers are linked to the gateway they communicate through.

## Installation

### HACS (recommended)

This integration is not in the HACS default store, so add it as a **custom
repository**:

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=antitoine&repository=ha-solem-irrigation&category=integration)

1. In HACS, open the **⋮** menu (top right) → **Custom repositories**.
2. Repository: `https://github.com/antitoine/ha-solem-irrigation` — Category:
   **Integration** — click **Add**.
3. Search for **SOLEM Irrigation**, open it, and click **Download**.
4. **Restart Home Assistant.**

### Manual

1. Download the [latest release](https://github.com/antitoine/ha-solem-irrigation/releases)
   (or clone this repository).
2. Copy the `custom_components/solem_irrigation` folder into your Home Assistant
   `config/custom_components/` directory (create `custom_components` if it does
   not exist).
3. **Restart Home Assistant.**

## Configuration

[![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=solem_irrigation)

Or go to **Settings → Devices & Services → Add Integration → SOLEM Irrigation**,
then enter your MySOLEM **email**, **password**, and **region** (Europe or
America — the same region you use to log in on mysolem.com).

Your controllers, stations, programs, and gateway are then discovered
automatically. (Pool modules on the same account are ignored — they belong to a
pool integration, not this one.)

## How it works / limitations

- Commands travel **cloud → LR-MB gateway → LoRa downlink → controller**. LoRa
  is duty-cycle limited, so a command you trigger in Home Assistant can take a
  few seconds to a minute to actually act. The integration updates the UI
  optimistically and then reconciles with the controller on the next poll.
- State is polled every 5 minutes (polling faster does not give fresher data).
- The *Irrigation enabled* switch and the *Rain delay* number are **assumed
  state** (the cloud exposes no reliable read-back); their values are restored
  across restarts. They both drive the controller's on/off, so they can show
  slightly out of sync with each other.

## Credits

Architecture and patterns inspired by
[FunFR/ha-indygo-pool](https://github.com/FunFR/ha-indygo-pool) — Indygo pools
and SOLEM irrigation share the same cloud backend (Indygo is *"Indygo by
SOLEM"*).

## Contributing

Bug reports and pull requests are welcome on the
[issue tracker](https://github.com/antitoine/ha-solem-irrigation/issues).
See [CONTRIBUTING.md](CONTRIBUTING.md) for the development setup (tests, linting,
Docker) and [CHANGELOG.md](CHANGELOG.md) for release history.

## Support

If this integration is useful to you, you can support its development:

<a href="https://buymeacoffee.com/antitoine" target="_blank"><img src="https://cdn.buymeacoffee.com/buttons/v2/default-yellow.png" alt="Buy Me A Coffee" height="50"></a>
