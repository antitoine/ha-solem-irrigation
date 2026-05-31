# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/antitoine/ha-solem-irrigation/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/antitoine/ha-solem-irrigation/compare/v0.1.2...v0.2.0
[0.1.2]: https://github.com/antitoine/ha-solem-irrigation/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/antitoine/ha-solem-irrigation/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/antitoine/ha-solem-irrigation/releases/tag/v0.1.0
