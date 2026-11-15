"""The library's readers are tested on httpx itself, as `microgrid[sources]` installs it
outside Home Assistant: scripts/ci_local.sh runs the library without Home Assistant's
harness, whose Home Assistant makes `import httpx` resolve to httpx2."""

import httpx
import pytest


def test_the_readers_run_on_httpx_itself(pytestconfig: pytest.Config) -> None:
    if pytestconfig.pluginmanager.has_plugin("pytest_homeassistant"):
        pytest.skip("a session with Home Assistant's harness")
    assert httpx.__name__ == "httpx"
