"""Coordinator for the DHL Package Tracker integration."""

from __future__ import annotations

import logging
from datetime import timedelta

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import DhlApiClient, DhlApiError
from .const import ACTIVE_CATEGORIES, DOMAIN, POLL_INTERVAL

_LOGGER = logging.getLogger(__name__)


def filter_active_parcels(parcels: list[dict]) -> list[dict]:
    """Return only active incoming parcels (not returns, in an active category)."""
    return [
        p
        for p in parcels
        if not p.get("isReturn", True) and p.get("category") in ACTIVE_CATEGORIES
    ]


def filter_active_sent_shipments(shipments: list[dict]) -> list[dict]:
    """Return only outgoing shipments that are still in transit (not yet delivered)."""
    return [
        s
        for s in shipments
        if s.get("type") == "outgoing" and s.get("category") in ACTIVE_CATEGORIES
    ]


class DhlCoordinator(DataUpdateCoordinator[list[dict]]):
    """Coordinator that polls the DHL parcels API on a fixed schedule."""

    def __init__(self, hass: HomeAssistant, client: DhlApiClient) -> None:
        """Initialise the coordinator.

        Args:
            hass: The Home Assistant instance.
            client: An authenticated :class:`DhlApiClient` instance.
        """
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=POLL_INTERVAL),
        )
        self._client = client

    async def _async_update_data(self) -> list[dict]:
        try:
            raw = await self._client.async_get_parcels()
        except (DhlApiError, aiohttp.ClientError) as err:
            raise UpdateFailed(f"DHL error: {err}") from err

        active = filter_active_parcels(raw)
        _LOGGER.debug("DHL parcels fetched: %d total, %d active", len(raw), len(active))

        # Push to the Parcel integration (no-op when Parcel is not loaded).
        await self._push_to_parcel(active)

        return active

    async def _push_to_parcel(self, incoming: list[dict]) -> None:
        """Push incoming + outgoing shipments to the Parcel integration."""
        try:
            from .parcel_bridge import push_to_parcel  # noqa: PLC0415
        except ImportError:
            return

        # Grab outgoing data from the sent coordinator if available.
        entry_data = self.hass.data.get(DOMAIN, {})
        outgoing: list[dict] = []
        for data in entry_data.values():
            if isinstance(data, dict) and "sent_coordinator" in data:
                sent_coord = data["sent_coordinator"]
                if sent_coord.data:
                    outgoing = sent_coord.data
                break

        await push_to_parcel(self.hass, incoming, outgoing)


class DhlSentShipmentsCoordinator(DataUpdateCoordinator[list[dict]]):
    """Coordinator that polls the DHL sent shipments API on a fixed schedule."""

    def __init__(self, hass: HomeAssistant, client: DhlApiClient) -> None:
        """Initialise the coordinator.

        Args:
            hass: The Home Assistant instance.
            client: An authenticated :class:`DhlApiClient` instance.
        """
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_sent",
            update_interval=timedelta(seconds=POLL_INTERVAL),
        )
        self._client = client

    async def _async_update_data(self) -> list[dict]:
        try:
            raw = await self._client.async_get_sent_shipments()
        except (DhlApiError, aiohttp.ClientError) as err:
            raise UpdateFailed(f"DHL error (sent): {err}") from err

        active = filter_active_sent_shipments(raw)
        _LOGGER.debug(
            "DHL sent shipments fetched: %d total, %d active", len(raw), len(active)
        )
        return active
