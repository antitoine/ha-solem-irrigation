# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/antitoine/ha-solem-irrigation/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/antitoine/ha-solem-irrigation/compare/v0.4.1...v0.5.0
[0.4.1]: https://github.com/antitoine/ha-solem-irrigation/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/antitoine/ha-solem-irrigation/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/antitoine/ha-solem-irrigation/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/antitoine/ha-solem-irrigation/compare/v0.1.2...v0.2.0
[0.1.2]: https://github.com/antitoine/ha-solem-irrigation/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/antitoine/ha-solem-irrigation/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/antitoine/ha-solem-irrigation/releases/tag/v0.1.0
