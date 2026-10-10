# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.9.0b3] - 2026-10-10

Third beta of 0.9.0, from two more reports in
[#8](https://github.com/antitoine/ha-solem-irrigation/issues/8): a wired on/off
rain sensor, and an LR-MS rain gauge watched through two threshold trips and
the OFF they triggered running out. To install it, enable **Show beta
versions** in HACS.

### Added

- **On/off rain sensors.** A rain sensor wired to a controller's sensor input
  (capteur de pluie, e.g. a dry-contact Rain Bird RSD on an LR-IP-ECO) now gets
  a binary sensor named after it in MySOLEM: *wet* while the sensor reports
  rain, *dry* once it has dried out, read from the 0/1 that MySOLEM plots for
  it. Identified by SOLEM's own input type (2), which the integration used to
  ignore. ([#8](https://github.com/antitoine/ha-solem-irrigation/issues/8))

### Changed

- **Two more rain threshold actions are named**: *Off for 2 days* and *Off for
  3 days* (codes 7 and 8, matched against the MySOLEM screen by the #8
  reporter).
- **The *rain threshold* binary sensor is documented for what it is.** On a
  real LR-MS it stayed on for days after the OFF it triggered had expired, so
  it records that the threshold was crossed, not that watering is held; the
  controllers' *Irrigation enabled* and *Rain delay* say that.

## [0.9.0b2] - 2026-10-07

Second beta of 0.9.0, shaped by the first real LR-MS rain gauge report in
[#8](https://github.com/antitoine/ha-solem-irrigation/issues/8). To install it,
enable **Show beta versions** in HACS.

### Changed

- **The *Irrigation enabled* switch and the *Rain delay* number now show what
  the controller reports.** They used to be assumed state, so an OFF set
  outside Home Assistant never showed: when a rain gauge crossed its threshold
  and MySOLEM set every linked controller to *OFF 1 day*, each switch still read
  on and each delay 0. The live state carries the status after all — read the
  way SOLEM's own web app reads it — so both now follow it, the delay showing
  the days left that the controller reports. A controller whose state carries
  no status keeps the previous behaviour: the last value set from Home
  Assistant, restored across restarts.
- ⚠️ **Breaking: Bluetooth-only controllers (BL-IP …) no longer get entities,
  and the device and entities earlier versions created are removed.** MySOLEM
  has no live link to them, so those entities could neither read nor command
  the controller — they only ever looked like they did. Any dashboard card or
  automation still referring to them must be updated. The startup warning
  naming them stays.
  ([#11](https://github.com/antitoine/ha-solem-irrigation/issues/11))
- **The rain threshold's action is named.** Its *threshold_action* attribute
  now reads *No action* or *Off for 1 day* instead of SOLEM's raw code (0 and 6,
  as matched against the MySOLEM screen by the #8 reporter); any other code is
  still shown raw. The configured threshold and action are also no longer lost
  when the polled record omits them.

### Added

- **When the rain threshold last fired.** A *last_threshold_alert* attribute on
  the *rain threshold* binary sensor, from SOLEM's own record.
- **Diagnostics keep the first reading of each sensor's 24-hour window**, which
  the 60-reading cap could drop — the only way to check a cumulative sensor's
  daily total against MySOLEM's.

## [0.9.0b1] - 2026-10-04

First beta of 0.9.0. It is aimed at the people who reported
[#8](https://github.com/antitoine/ha-solem-irrigation/issues/8) and
[#12](https://github.com/antitoine/ha-solem-irrigation/issues/12): the rain
gauge is built from a real LR-MS payload but has never run against one, and the
new diagnostics readings are what the LR-IP-ECO's turbine flow meter is waiting
on. To install it, enable **Show beta versions** in HACS.

### Added

- **Rain gauge support.** A SOLEM tipping-bucket rain gauge (pluviomètre — e.g.
  the one on an LR-MS sensor module) now gets its own device, with a
  *&lt;gauge&gt; rainfall* sensor — SOLEM's lifetime total in mm, ready for
  long-term statistics and a `utility_meter` — and a *&lt;gauge&gt; rain
  threshold* binary sensor carrying SOLEM's own "beyond thresholds" flag as-is,
  with the threshold set in MySOLEM as an attribute. Identified by SOLEM's own
  input type (14), as listed in its web app; the scaling is the per-gauge
  expression SOLEM ships.
  ([#8](https://github.com/antitoine/ha-solem-irrigation/issues/8))
- **Leftover devices can be deleted.** A replaced gateway or controller is a
  new module, so its old device used to linger forever with every entity
  *unavailable*, and Home Assistant offered no way to remove it. Any device the
  integration no longer exposes can now be deleted, even if the old module is
  still listed in MySOLEM; one still exposed is refused, as it would only come
  back.
- **Diagnostics now include each sensor's recent readings.** For every
  configured input — modelled or not — the dump carries its newest raw tick and
  up to 60 scaled ticks from the last 24 hours, plus any flag that changed since
  setup. The module page holds no readings at all, so for a sensor the
  integration does not model yet (such as the LR-IP-ECO's turbine flow meter,
  [#12](https://github.com/antitoine/ha-solem-irrigation/issues/12)), this is
  what reveals its scale and behaviour.

## [0.8.1] - 2026-10-04

### Fixed

- **An account holding only Bluetooth controllers (BL-IP …) no longer fails
  to set up.** MySOLEM has no live link to a Bluetooth-only module, so its state
  endpoint answers `503` on every request — by design, and SOLEM's own web app
  never asks it. The integration polled it anyway, and with no other module to
  succeed, every cycle failed and setup retried forever. Bluetooth-only modules
  (SOLEM's `isBluetoothOnly` flag, or a `bl-*` type) are no longer polled, and
  a warning at startup names them. A real cloud outage still fails the cycle
  as before.

  On an account that mixes Bluetooth-only controllers with LoRa or WiFi ones,
  nothing visible changes beyond that warning: those modules were already
  failing every poll, just silently. Their entities are still created, but
  since MySOLEM cannot see these controllers, they cannot reflect what the
  controllers are doing.
  ([#11](https://github.com/antitoine/ha-solem-irrigation/issues/11))

## [0.8.0] - 2026-09-23

### Added

- **Diagnostics.** The integration page now offers **⋮ → Download diagnostics**,
  which dumps, for every module on the account (including those the integration
  ignores): the raw module record, **every** sensor input — not only the flow
  meters that currently become entities — and the live state. Credentials,
  serial numbers, hardware identifiers and location are redacted.

  SOLEM's API is private and undocumented and returns different fields for
  different hardware, so this is what makes a report about hardware the
  maintainer does not own actionable. The bug-report and feature-request
  templates now ask for it.

### Fixed

- ***Last communication* no longer stays `unknown` on modules without a LoRa
  radio.** It only ever read `lastRadioCommunication`, which is the gateway ↔
  controller radio contact and therefore absent on anything that reaches the
  cloud directly. It now falls back to the module's own `seenAt`.

  This affected two cases: WiFi controllers such as the SMART-IS (as reported),
  and — found while investigating — **the LR-MB gateway itself**, whose sensor
  had been permanently `unknown` on every installation since it first got a
  device. LoRa controllers are unchanged.
  ([#7](https://github.com/antitoine/ha-solem-irrigation/issues/7))

- **The *Battery* level and the gateway's *Last communication* now actually
  update.** Both are read from a module's record, which came from the module
  page — over a megabyte per module, so fetched once at setup. Everything taken
  from it was therefore frozen until the next reload: measured on a real
  LR-MB-10, its timestamp changed 3 times in 24 hours (once per reload) against
  286 for the LoRa controller beside it. A battery level that can never fall is
  the worse half: it would never have warned anyone.

  Each poll now also re-reads just those few fields through the module
  endpoint's field projection — **under a kilobyte for an entire account, in one
  request** — and merges them in. A failure leaves the previous values in place
  rather than blanking them.

### Changed

- The discovery debug log now reports each module's input count and input
  types, so a sensor the integration does not model yet is visible from the log
  alone.
- README: documented that discovery is transport-agnostic — WiFi modules
  (SMART-IS) work exactly like LoRa ones, which was not stated anywhere.
  ([#7](https://github.com/antitoine/ha-solem-irrigation/issues/7))

### Note for anyone who tested `0.8.0b1`

That pre-release's diagnostics file under-redacted four keys — `uuid` (which rebuilds
the MAC and serial), `snapshotBy` (the account's user id), `locationKey` (an
AccuWeather id resolving to the town) and `defaultName`. Each sat beside a key
that *was* redacted, carrying the same identity in another encoding. Fixed in
`0.8.0b2`, and the test suite now walks the whole payload
rejecting any MAC- or UUID-shaped value. **If you downloaded a diagnostics file
on `0.8.0b1`, delete it rather than attaching it anywhere.**

## [0.7.0] - 2026-09-20

### Changed

- **Home Assistant 2026.9 or newer is now required** (previously 2024.12). The
  controller → gateway device link was migrated from the deprecated `via_device`
  to `via_device_id`, which only exists from 2026.9 — Home Assistant removes
  `via_device` in 2027.8. The link is now applied when the devices are
  registered at setup rather than from each entity's device info, because
  `via_device_id` takes a device-registry id. Installations on an older Home
  Assistant simply will not be offered the update by HACS.

### Fixed

- A module with no serial number no longer ends up with an empty-string serial
  on its device instead of none.

## [0.6.0] - 2026-07-30

### Added

- **Flow meter (débitmètre) support.** A controller with a flow meter now gets two
  sensors per meter:
  - ***&lt;meter&gt; water used*** — SOLEM's lifetime water counter, as a
    `total_increasing` volume with the `water` device class, so it can be added
    to the Home Assistant **Water** dashboard. Because the counter is SOLEM's own
    and not accumulated locally, restarting Home Assistant never double-counts.
  - ***&lt;meter&gt; flow rate*** — how fast water is flowing right now, in
    L/min (or gal/min). Non-zero outside a watering run is the signal a leak
    would produce. The meter only records while water flows, so the rate is
    derived from its most recent readings: it reads `0` when idle and may be
    briefly unknown in the first minute of a run.

  The meter's configuration (measurement interval, thresholds, leak-alert volume,
  each station's nominal flow) and SOLEM's own probe flags are exposed as
  attributes of the *water used* sensor. Meters on pool modules are ignored, as
  those modules already are.

### Fixed

- The *Battery* sensor is now created only for modules SOLEM flags as battery
  powered (`isBattery`), and for those it appears even when the level reads `0`.
  Previously it was gated on a truthy level, which had two consequences: a
  genuinely flat battery produced no entity, and (briefly, in `0.6.0b1`)
  mains-powered modules such as an AC-powered LR-IS gained a meaningless
  *Battery* showing `0` — SOLEM reports `0` there to mean "no battery". Any such
  entity created by `0.6.0b1` is removed automatically on upgrade.

## [0.5.0] - 2026-05-31

Quality and maintainability release. No functional changes to the integration —
the entities, services and behaviour are identical to 0.4.1.

### Added

- **Automated test suite** (`pytest` + `pytest-homeassistant-custom-component`)
  covering the MySOLEM API client, the coordinator, the config flow,
  setup / legacy-entity migration, and every entity platform.
- **Continuous integration**: a *Test* workflow (ruff lint + format check +
  pytest with coverage) and a *Release* workflow that builds the installable
  zip, alongside the existing hassfest / HACS validation.
- **Contributor tooling**: pre-commit hooks, a `uv`-managed dev environment
  (`pyproject.toml`), a Docker Compose dev setup, GitHub issue templates, and
  `AGENTS.md` / `CLAUDE.md` / `CONTRIBUTING.md` guides.

## [0.4.1] - 2026-05-31

### Fixed

- **Could not stop a running program.** Starting a program doesn't open a
  specific valve, so there was no obvious way to stop it from the device page.
  Added a global **Stop** button (*Arrêter l'arrosage*) per controller that stops
  anything running — a program or a manual station run. (Closing a station valve
  still stops that station; `solem_irrigation.run` with `mode: stop` also works.)

## [0.4.0] - 2026-05-31

Surface the two action-only capabilities as device-page controls, so running a
program and setting a rain delay no longer require the Actions form.

### Added

- **Number** *Run duration* per station (under *Configuration*): how long opening
  that station's valve runs it. Each station keeps its own duration; an explicit
  `duration` on the `run` service updates the same value.
- **Select** *Run program*: pick a stored program to start it now. It resets to
  a neutral placeholder after each run, so the same program can be picked again
  (a select never re-fires the option already shown).
- **Number** *Rain delay* (days): set N to disable irrigation for N days, 0 to
  re-enable — the visible form of `set_enabled`'s `days`, mirroring SOLEM's
  "Report de pluie". Assumed-state, restored across restarts.

### Changed

- The remembered manual-run duration is now tracked **per station** (it was a
  single per-controller value). Opening a station's valve uses that station's
  *Run duration*, defaulting to 5 min.

The equivalent `solem_irrigation.run` (`mode: program`, `duration`) and
`solem_irrigation.set_enabled` (`days`) actions remain for automations.

## [0.3.0] - 2026-05-31

Adopt the standard Home Assistant irrigation model (as used by Hunter Hydrawise):
each station is now a **valve** instead of options in a dropdown.

### Added

- **Valve** per station: open it to water that station for the remembered
  duration (the controller stops it automatically); close it to stop. Because
  every valve's state is derived from the single running-station reading, only
  one valve is ever open — faithfully modelling SOLEM's one-station-at-a-time
  hardware. Works with the standard tile/valve cards, voice, and the Schedule
  helper.

### Changed

- **BREAKING:** the *Manual run* `select` (added in 0.2.0) has been removed and
  replaced by the per-station valves. It is auto-removed from the entity
  registry on upgrade.
- The `solem_irrigation.run` service is unchanged but now targets the
  *Irrigation enabled* switch (the controller entity) instead of the select. It
  remains the way to run a **program**, to **stop**, or to run a station for a
  **specific** (non-default) duration.

### Fixed

- The dropdown could not stop a running **program** (it showed `Stop` when idle,
  and a select never re-fires the already-selected option). Removing the select
  in favour of valves resolves this — closing the open valve, or
  `solem_irrigation.run` with `mode: stop`, always stops.

## [0.2.0] - 2026-05-31

Simplified control surface, aligned with the SOLEM app: two conceptual commands
instead of ~11 per-station/per-program controls.

### Added

- **Select** *Manual run* per controller: a single dropdown to **Stop**, run any
  program, or run any station. Stations use the remembered run duration. It also
  reflects the running station.
- Service **`solem_irrigation.run`** — the same manual command for automations:
  `mode` (`stop` / `program` / `station`) plus `program`, `station` and
  `duration` (minutes). An explicit `duration` is remembered for next time.
- Service **`solem_irrigation.set_enabled`** — global on/off command: `enabled`
  plus an optional `days` to disable for a period (rain delay).
- The manual-run duration is now persisted and reused across runs/restarts (a
  default of 5 min is used until you set one).

### Changed

- **BREAKING:** The per-station switches, per-program *Run* buttons, the global
  *Stop watering* button, and the *Run duration* / *Rain delay* number entities
  have been removed. Their functions are now covered by the *Manual run* select,
  the enable switch, and the two services above. These obsolete entities are
  removed from the entity registry automatically on upgrade.
- Turning the *Irrigation enabled* switch off now disables permanently; use
  `solem_irrigation.set_enabled` with `days` for a timed (rain-delay) disable.

## [0.1.2] - 2026-05-31

### Fixed

- Resolved a `via_device` warning ("referencing a non existing via_device")
  that fired because the controller's device was created before its gateway
  device existed. Devices (controllers and their gateway) are now pre-registered
  during setup, so the parent/child link always resolves.

## [0.1.1] - 2026-05-31

### Fixed

- No devices/entities were created after setup. The module-list endpoint only
  returns module IDs (no `type`/`name`/`serialNumber` unless a field projection
  is sent), so controller detection always failed. Each module is now read in
  full from its module page, and irrigation controllers are identified via
  SOLEM's `typeIsWatering` flag — which also correctly excludes pool
  controllers that expose stations of their own.
- Added discovery debug logging and a warning when modules are found but none
  are irrigation controllers.

## [0.1.0] - 2026-05-30

### Added

- Initial release.
- Config flow (email / password / region) with re-authentication support.
- Cloud-polling client for the MySOLEM web API (session-cookie authentication),
  reverse-engineered from the mysolem.com web app.
- Automatic discovery of irrigation controllers and their stations and programs.
  Pool modules and the gateway are excluded from station/program entities.
- One switch per station — sequential watering (only one runs at a time); turning
  a station off sends a global stop.
- Master "Irrigation enabled" switch (assumed state).
- "Run duration" (minutes) and "Rain delay" (days) number entities.
- "Watering station" (with `time_remaining`), "Last communication" and "Battery"
  sensors.
- One button per stored program, plus a global "Stop watering" button.
- Optimistic state updates with a delayed reconcile to cope with LoRa latency.
- English and French translations.

[Unreleased]: https://github.com/antitoine/ha-solem-irrigation/compare/v0.9.0b3...HEAD
[0.9.0b3]: https://github.com/antitoine/ha-solem-irrigation/compare/v0.9.0b2...v0.9.0b3
[0.9.0b2]: https://github.com/antitoine/ha-solem-irrigation/compare/v0.9.0b1...v0.9.0b2
[0.9.0b1]: https://github.com/antitoine/ha-solem-irrigation/compare/v0.8.1...v0.9.0b1
[0.8.1]: https://github.com/antitoine/ha-solem-irrigation/compare/v0.8.0...v0.8.1
[0.8.0]: https://github.com/antitoine/ha-solem-irrigation/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/antitoine/ha-solem-irrigation/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/antitoine/ha-solem-irrigation/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/antitoine/ha-solem-irrigation/compare/v0.4.1...v0.5.0
[0.4.1]: https://github.com/antitoine/ha-solem-irrigation/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/antitoine/ha-solem-irrigation/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/antitoine/ha-solem-irrigation/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/antitoine/ha-solem-irrigation/compare/v0.1.2...v0.2.0
[0.1.2]: https://github.com/antitoine/ha-solem-irrigation/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/antitoine/ha-solem-irrigation/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/antitoine/ha-solem-irrigation/releases/tag/v0.1.0
