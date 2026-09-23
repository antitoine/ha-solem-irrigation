# SOLEM Irrigation for Home Assistant

[![Validate](https://github.com/antitoine/ha-solem-irrigation/actions/workflows/validate.yml/badge.svg)](https://github.com/antitoine/ha-solem-irrigation/actions/workflows/validate.yml)
[![Test](https://github.com/antitoine/ha-solem-irrigation/actions/workflows/test.yml/badge.svg)](https://github.com/antitoine/ha-solem-irrigation/actions/workflows/test.yml)
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Buy Me A Coffee](https://img.shields.io/badge/Buy%20Me%20A%20Coffee-donate-FFDD00?logo=buymeacoffee&logoColor=black)](https://buymeacoffee.com/antitoine)

A custom [Home Assistant](https://www.home-assistant.io/) integration for
**SOLEM** connected irrigation controllers reached through the **MySOLEM**
cloud.

It talks to the same cloud backend as the [mysolem.com](https://mysolem.com)
web app (a session-cookie API), so it reaches controllers that the
Bluetooth-only community integrations cannot.

**Which hardware works:** anything MySOLEM itself lists as a watering device
with at least one station. Discovery does not care how a controller reaches the
cloud, so LoRa modules behind an **LR-MB** gateway (LR-IS / LR-IP …) and
**WiFi** modules (SMART-IS …) are both picked up, with no configuration
difference. Development happens against an LR-IS behind an LR-MB-10; other
models are reported working by their owners. If yours is not detected, please
[open an issue](https://github.com/antitoine/ha-solem-irrigation/issues) with a
diagnostics file — see [Reporting a problem](#reporting-a-problem).

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
| **Sensor** *Last communication* | When the module last talked to the cloud — the LoRa radio contact for modules behind a gateway, the module's own connection for the gateway and for WiFi controllers. |
| **Sensor** *Battery* | Battery indicator (battery-powered modules). |
| **Sensor** *&lt;meter&gt; water used* | SOLEM's lifetime water counter, for controllers with a flow meter (débitmètre). Add it to the Home Assistant **Water** dashboard. |
| **Sensor** *&lt;meter&gt; flow rate* | How fast water is flowing right now — non-zero outside a watering run means a leak. |

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
- A **flow meter** is a sensor wired to the controller, not a device of its own,
  so its entities live on the controller. *Water used* is SOLEM's own lifetime
  counter, which means restarting Home Assistant never double-counts it. The
  meter only records a reading while water actually flows, so *flow rate* is
  derived from the last minutes of readings; it reads `0` when idle and can be
  briefly unknown in the first minute of a run.

## Reporting a problem

MySOLEM's API is private, undocumented, and returns different fields for
different hardware — and this integration is developed against exactly one
setup. So for anything hardware-specific, a **diagnostics file** is far more
useful than a description, and usually decides whether something is fixable at
all.

**Settings → Devices & Services → SOLEM Irrigation → ⋮ → Download
diagnostics**, then attach the file to your issue.

It contains, for every module on your account (including the ones the
integration ignores): the raw module record, **every** sensor input — not just
the ones that currently become entities — and the live state. Credentials,
serial numbers and your location are redacted automatically; module **names**
are kept, since they are what an issue refers to, so rename them in MySOLEM
first if any of yours is personal.

If a sensor you own is missing, debug logs also help: they list every module
with its input types.

```yaml
logger:
  default: info
  logs:
    custom_components.solem_irrigation: debug
```

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
