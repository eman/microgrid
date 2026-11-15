"""The Home Assistant integration's tests, on Home Assistant's own test harness
(``pytest-homeassistant``), which enables custom integrations."""

import pytest

pytest.importorskip("pytest_homeassistant")
