# SOLEM Irrigation for Home Assistant

[![Validate](https://github.com/antitoine/ha-solem-irrigation/actions/workflows/validate.yml/badge.svg)](https://github.com/antitoine/ha-solem-irrigation/actions/workflows/validate.yml)
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

For each irrigation controller on your account:

| Entity | What it does |
| --- | --- |
| **Switch** per station | Turn on → run that station for the configured duration. Turn off → stop. Only one station runs at a time. Named after the station's name in MySOLEM. |
| **Switch** *Irrigation enabled* | Enable / disable the controller (assumed state). |
| **Number** *Run duration* | Minutes used when a station switch is turned on. |
| **Number** *Rain delay* | Disable for N days (0 = enabled). |
| **Button** *Run &lt;program&gt;* | Start a stored program (one button per program defined in MySOLEM). |
| **Button** *Stop watering* | Global stop. |
| **Sensor** *Watering station* | Name of the station currently watering (idle = none). |
| **Sensor** *Last communication* | Last radio contact with the module. |
| **Sensor** *Battery* | Battery indicator (battery-powered modules). |

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
  is duty-cycle limited, so a switch you toggle in Home Assistant can take a
  few seconds to a minute to actually act. The integration updates the UI
  optimistically and then reconciles with the controller on the next poll.
- State is polled every 5 minutes (polling faster does not give fresher data).
- The *Irrigation enabled* switch and *Rain delay* number are **assumed state**
  (the cloud exposes no reliable read-back); their values are restored across
  restarts.

## Credits

Architecture and patterns inspired by
[FunFR/ha-indygo-pool](https://github.com/FunFR/ha-indygo-pool) — Indygo pools
and SOLEM irrigation share the same cloud backend (Indygo is *"Indygo by
SOLEM"*).

## Contributing

Bug reports and pull requests are welcome on the
[issue tracker](https://github.com/antitoine/ha-solem-irrigation/issues).
See [CHANGELOG.md](CHANGELOG.md) for release history.

## Support

If this integration is useful to you, you can support its development:

<a href="https://buymeacoffee.com/antitoine" target="_blank"><img src="https://cdn.buymeacoffee.com/buttons/v2/default-yellow.png" alt="Buy Me A Coffee" height="50"></a>
