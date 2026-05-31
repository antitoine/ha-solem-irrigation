"""Shared pytest fixtures for the SOLEM irrigation tests."""

import pathlib

import pytest

# When the integration is installed in editable mode, Home Assistant's
# integration discovery walks a synthetic ``__editable__`` finder path that does
# not exist on disk, raising FileNotFoundError mid-config-flow. Swallow that one
# case so config-flow tests can enumerate integrations. (Same shim as upstream
# HACS integration test suites.)
_original_iterdir = pathlib.Path.iterdir


def _safe_iterdir(self):
    try:
        return _original_iterdir(self)
    except FileNotFoundError:
        return iter([])


pathlib.Path.iterdir = _safe_iterdir

try:
    from pytest_socket import enable_socket, socket_allow_hosts
except ImportError:  # pragma: no cover - pytest-socket always present via PHCC

    def enable_socket():
        """No-op when pytest-socket is unavailable."""

    def socket_allow_hosts(*args, **kwargs):
        """No-op when pytest-socket is unavailable."""


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable loading of the custom integration in every test."""
    yield


@pytest.fixture(autouse=True)
def allow_socket_fixture(request):
    """Allow real sockets only for tests explicitly marked ``integration``."""
    if request.node.get_closest_marker("integration"):
        enable_socket()
        socket_allow_hosts(
            ["127.0.0.1", "localhost", "::1", "mysolem.com", "us.mysolem.com"],
            allow_unix_socket=True,
        )


pytest_plugins = "pytest_homeassistant_custom_component"
