"""Home Assistant's REST API: an entity's state (with its attributes), a service that
returns a response, an integration's config entries, and an entity's history.

Authentication is a long-lived access token, read from the environment
(``HomeAssistant.token_env``), never from a file.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Self

import httpx
from pydantic import SecretStr

from microgrid.cli.sources import SourceError
from microgrid.cli.sources.config import HomeAssistant

__all__ = [
    "DEFAULT_TIMEOUT",
    "HaClient",
    "HaError",
]

DEFAULT_TIMEOUT = httpx.Timeout(20.0, connect=5.0)


class HaError(SourceError):
    """Home Assistant could not be reached or refused the request."""


class HaClient:
    """Async HA REST client. Use as an async context manager, or pass ``transport`` in tests."""

    def __init__(
        self,
        base_url: str,
        token: SecretStr,
        *,
        timeout: httpx.Timeout = DEFAULT_TIMEOUT,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token.get_secret_value()}"},
            timeout=timeout,
            transport=transport,
        )

    @classmethod
    def connect(cls, config: HomeAssistant) -> HaClient:
        """A client for ``config``, its token from the environment variable it names."""
        token = os.environ.get(config.token_env)
        if not token:
            raise HaError(f"no Home Assistant token: set {config.token_env}")
        return cls(config.url, SecretStr(token))

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying HTTP connection pool."""
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = await self._client.request(method, path, **kwargs)
        except (httpx.HTTPError, httpx.InvalidURL) as err:
            # InvalidURL is not an HTTPError: a malformed entity id in a configuration.
            raise HaError(f"{method} {path}: {type(err).__name__}: {err}") from err
        if response.status_code >= 400:
            # HA does not relay a service's reason over REST: a schema error is a bare
            # "400: Bad Request" and a ServiceValidationError a generic 500. The reason is
            # only in HA's log.
            hint = " (see the Home Assistant log)" if path.startswith("/api/services/") else ""
            raise HaError(
                f"{method} {path}: HTTP {response.status_code}: {response.text[:200]}{hint}"
            )
        try:
            return response.json()
        except ValueError as err:
            raise HaError(f"{method} {path}: response is not JSON") from err

    async def get_state(self, entity_id: str) -> dict[str, Any]:
        """``GET /api/states/<entity_id>``: state, attributes and timestamps."""
        body = await self._request("GET", f"/api/states/{entity_id}")
        if not isinstance(body, dict):
            raise HaError(f"state of {entity_id} is not an object")
        return body

    async def call_service(self, domain: str, service: str, data: dict[str, Any]) -> Any:
        """Call a service with ``return_response`` and return its ``service_response``."""
        body = await self._request(
            "POST", f"/api/services/{domain}/{service}", params="return_response", json=data
        )
        if not isinstance(body, dict) or "service_response" not in body:
            raise HaError(f"{domain}.{service} returned no service_response")
        return body["service_response"]

    async def config_entries(self, domain: str) -> list[dict[str, Any]]:
        """Config entries of one integration (``/api/config/config_entries/entry``)."""
        body = await self._request(
            "GET", "/api/config/config_entries/entry", params={"domain": domain}
        )
        if not isinstance(body, list):
            raise HaError(f"config entries of {domain} are not a list")
        return [entry for entry in body if isinstance(entry, dict)]

    async def history(
        self, entity_ids: Sequence[str], start: datetime, end: datetime
    ) -> dict[str, list[tuple[datetime, str]]]:
        """Each entity's states over ``[start, end)``, from the one in force at ``start``,
        as ``(time it changed, state)`` (``GET /api/history/period``; the recorder keeps
        what its settings keep, ten days by default)."""
        body = await self._request(
            "GET",
            f"/api/history/period/{start.astimezone(UTC).isoformat()}",
            params={
                "filter_entity_id": ",".join(entity_ids),
                "end_time": end.astimezone(UTC).isoformat(),
                "minimal_response": "",
                "no_attributes": "",
            },
        )
        if not isinstance(body, list):
            raise HaError("history is not a list")
        found: dict[str, list[tuple[datetime, str]]] = {e: [] for e in entity_ids}
        for rows in body:
            if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
                continue
            entity = rows[0].get("entity_id")
            if entity not in found:
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                try:
                    changed = datetime.fromisoformat(str(row["last_changed"]))
                except KeyError, ValueError:
                    continue
                found[entity].append((changed.astimezone(UTC), str(row.get("state"))))
        return found
