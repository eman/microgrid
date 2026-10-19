"""The Home Assistant integration's tests: Home Assistant's own test harness
(``pytest-homeassistant-custom-component``). A module that loads the integration asks
for ``enable_custom_integrations``."""

from collections.abc import Iterator
from contextlib import suppress

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")


@pytest.fixture(autouse=True, scope="session")
def recorder_migration_annotations() -> Iterator[None]:
    """Make the recorder's deferred type names resolvable at runtime.

    The harness patches ``migration._find_schema_errors`` with ``autospec=True``, which
    evaluates its annotations; that module imports ``Recorder`` only under
    ``TYPE_CHECKING``, and on Python 3.14 the evaluation fails before any test runs.
    Binding the real classes is for the harness, not the integration.
    """
    from homeassistant.components.recorder import migration
    from homeassistant.components.recorder.core import Recorder
    from homeassistant.helpers import recorder as recorder_helper
    from sqlalchemy.orm.session import Session

    patched = (
        (migration, "Recorder", Recorder),
        (migration, "Session", Session),
        (recorder_helper, "Session", Session),
    )
    for module, name, value in patched:
        setattr(module, name, value)
    yield
    for module, name, _ in patched:
        with suppress(AttributeError):
            delattr(module, name)
