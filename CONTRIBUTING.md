# Contributing to SOLEM Irrigation

This document covers how to set up your development environment and contribute to
the **SOLEM Irrigation** integration.

For project architecture and design philosophy, see [AGENTS.md](AGENTS.md).
For features and installation, see [README.md](README.md).

## Contribution guidelines

Contributions are welcome — whether it's reporting a bug, discussing code, or
opening a pull request.

1. Fork the repo and create your branch from `main`.
2. If you changed behaviour, update the documentation (`README.md`,
   `strings.json` / translations) and add a `CHANGELOG.md` entry.
3. Make sure tests, linting and formatting pass (see the checklist below).
4. Open the pull request, using a [Conventional Commit](https://www.conventionalcommits.org/)
   style title (e.g. `fix: stop a running program`).

## 🛠️ Technology stack & environment

- **Language**: Python 3.13+ (type hints expected)
- **Dependency management**: [uv](https://docs.astral.sh/uv/)
- **Lint & format**: [ruff](https://docs.astral.sh/ruff/)
- **Testing**: [pytest](https://docs.pytest.org/) with
  [`pytest-homeassistant-custom-component`](https://github.com/MatthewFlamm/pytest-homeassistant-custom-component)
- **Manual testing**: Docker / Docker Compose

> **Why Python 3.13?** Recent Home Assistant releases (which the test
> dependencies pull in) require Python 3.13. The integration itself runs on
> whatever Python your Home Assistant uses; this only affects the dev/test env.
> A `.python-version` file pins it for `uv`.

### Getting started

```bash
git clone https://github.com/antitoine/ha-solem-irrigation.git
cd ha-solem-irrigation
uv sync --all-extras --all-groups   # create the env and install dev deps
uv run pre-commit install           # enable the git hooks
```

## ✅ Quality assurance checklist

Before telling the user you are done or opening a PR, you MUST ensure:

- [ ] **Tests pass**: `uv run pytest -m "not integration"`
- [ ] **Linting is clean**: `uv run ruff check .`
- [ ] **Code is formatted**: `uv run ruff format --check .`
- [ ] **Coverage holds**: the suite enforces a minimum (currently 90%).

A one-liner mirroring CI:

```bash
uv run ruff check . && uv run ruff format --check . && uv run pytest -m "not integration"
```

## 🧪 Testing

### Automated tests

```bash
uv run pytest -m "not integration"
```

The suite covers the API client, the coordinator, the config flow, setup /
legacy-entity migration, and every entity platform — without any network access.

### Live integration test (real credentials)

A single smoke test talks to the real MySOLEM cloud. It is skipped unless you
provide credentials. Copy `.env.example` to `.env` and fill it in:

```env
email=your_email@example.com
password=your_password
region=Europe
```

Then run:

```bash
uv run pytest -m integration
```

### Manual testing in Home Assistant (Docker)

```bash
docker compose up -d        # start HA at http://localhost:8123
docker compose logs -f      # follow logs
docker compose down         # stop
```

`docker-compose.yml` mounts `custom_components/solem_irrigation` into the
container. Restart the container to pick up code changes.

### Deploying to a real Home Assistant

```bash
scp -r custom_components/solem_irrigation root@<HA_IP>:/config/custom_components/
```

> Requires the **SSH & Web Terminal** add-on. Restart Home Assistant to apply.

## ✨ Code quality commands

- **Check**: `uv run ruff check .`
- **Fix**: `uv run ruff check --fix .`
- **Format**: `uv run ruff format .`
- **All hooks**: `uv run pre-commit run --all-files`
