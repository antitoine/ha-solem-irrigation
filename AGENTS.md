# AGENTS.md

This file guides AI agents (and humans) working on the **SOLEM Irrigation**
Home Assistant integration. Follow it to keep the codebase consistent and
maintainable.

## 🧠 Philosophy & Principles

- **KISS / DRY**: keep solutions simple; factor shared logic (the coordinator's
  command helpers and the base entity exist for this reason).
- **Modern Home Assistant architecture**: follow the
  [HA Developer Docs](https://developers.home-assistant.io/). Use
  `DataUpdateCoordinator` for polling, a config flow for setup, and standard
  entity platforms.
  - **HTTP**: ALWAYS get a session from
    `homeassistant.helpers.aiohttp_client.async_create_clientsession` (the
    coordinator and config flow already do). Never instantiate
    `aiohttp.ClientSession()` directly in HA code.
  - **Strings & translations**: every user-facing string lives in
    `strings.json` and the `translations/` files; entities use
    `translation_key`, not hardcoded `name`s. Keep `en` and `fr` in sync.
  - **Native constants**: prefer HA constants (e.g. `UnitOfTime.MINUTES`) over
    raw strings.
- **Unofficial, reverse-engineered API**: this talks to the private MySOLEM web
  backend. It can change without notice. Treat `api.py` as the only place that
  knows the wire format.

## 📂 Project structure & responsibilities

- **`api.py`** — ALL MySOLEM cloud logic (login, module discovery, live state,
  sensor readings, manual commands). Session-cookie auth, HTML-embedded JSON
  parsing, retry on expired session. No Home Assistant imports here. Note the
  module page carries *two* embedded literals: `let module = {…}` and a sibling
  `var inputs = […]` (the module's sensors), so both come from one fetch.
- **`coordinator.py`** — `DataUpdateCoordinator` that logs in, discovers modules
  (classifying irrigation controllers via SOLEM's `typeIsWatering` flag),
  polls live state, and owns the **optimistic update + delayed reconcile** used
  to mask slow LoRa downlinks. Also persists the per-station run duration and
  polls each flow meter (its lifetime counter plus a derived flow rate).
- **`entity.py`** — base `CoordinatorEntity` sharing `device_info` (and the
  controller → gateway `via_device` link) across platforms.
- **Platforms** — thin wrappers over coordinator data:
  - `valve.py` — one valve per station (only one is ever open: the hardware
    waters one station at a time).
  - `switch.py` — the assumed-state *Irrigation enabled* switch; also registers
    the `run` and `set_enabled` services.
  - `select.py` — *Run program* (momentary; resets to a neutral option).
  - `number.py` — per-station *Run duration* and assumed-state *Rain delay*.
  - `button.py` — global *Stop watering*.
  - `sensor.py` — *Watering station*, *Last communication*, *Battery*, and per
    flow meter *water used* (cumulative) + *flow rate*.
- **`config_flow.py`** — setup (email / password / region) with re-auth.
- **`const.py`** — domain, regions/base URLs, command vocabulary, service names.
- **`tests/`** — `pytest` suite mirroring the source. Pure logic and entity
  behaviour are unit-tested; an optional `@pytest.mark.integration` test hits the
  real API when a `.env` is present.

## 🔁 Development workflow

1. **Understand** the request and the existing code.
2. **Implement** following the structure above. Keep assumed-state entities
   (`enabled`, `rain_delay`) restoring across restarts, and route every manual
   command through the coordinator's command helpers so the optimistic update
   and reconcile stay consistent.
3. **Verify & commit**: you MUST pass the QA checklist in
   [CONTRIBUTING.md](CONTRIBUTING.md).
   - **Conventional Commits**: `feat:`, `fix:`, `refactor:`, `chore:`, `docs:`,
     `test:`, `ci:` … Keep messages concise.
   - **Breaking changes** (`!`): only when it breaks a user's HA config
     (removing an entity, renaming the domain, changing required setup). Note
     any auto-migration in `__init__.py`'s legacy-cleanup and the CHANGELOG.

## 📚 References

- [`CONTRIBUTING.md`](CONTRIBUTING.md) — tech stack, dev environment, QA commands.
- [`README.md`](README.md) — features, installation, how it works.
- [`CHANGELOG.md`](CHANGELOG.md) — release history (Keep a Changelog + SemVer).
