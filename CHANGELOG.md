# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/antitoine/ha-solem-irrigation/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/antitoine/ha-solem-irrigation/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/antitoine/ha-solem-irrigation/releases/tag/v0.1.0
