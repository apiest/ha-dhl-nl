"""Coordinator for the DHL Package Tracker integration."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import DhlApiClient, DhlApiError
from .const import (
    ACTIVE_CATEGORIES,
    DELIVERED_CATEGORY,
    DELIVERED_RETENTION_DAYS,
    DOMAIN,
    POLL_INTERVAL,
)

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


def _parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO 8601 timestamp, returning None when absent or malformed."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _days_since_delivery(shipment: dict) -> float | None:
    """Return days since delivery, or None when it cannot be determined."""
    received_days_ago = shipment.get("receivedDaysAgo")
    if isinstance(received_days_ago, (int, float)):
        return float(received_days_ago)

    indication = shipment.get("receivingTimeIndication") or {}
    moment = _parse_iso(indication.get("moment")) or _parse_iso(
        shipment.get("timeCreated")
    )
    if moment is None:
        return None
    return (datetime.now(UTC) - moment).total_seconds() / 86400


def _is_recently_delivered(shipment: dict) -> bool:
    """Return True for delivered shipments still inside the retention window.

    Shipments whose delivery date cannot be determined are included, so a
    missing timestamp never causes Parcel to mis-expire a delivered parcel.
    """
    if shipment.get("category") != DELIVERED_CATEGORY:
        return False
    days = _days_since_delivery(shipment)
    return days is None or days <= DELIVERED_RETENTION_DAYS


def filter_recently_delivered_parcels(parcels: list[dict]) -> list[dict]:
    """Return incoming parcels delivered within the retention window."""
    return [
        p for p in parcels if not p.get("isReturn", True) and _is_recently_delivered(p)
    ]


def filter_recently_delivered_sent_shipments(shipments: list[dict]) -> list[dict]:
    """Return outgoing shipments delivered within the retention window."""
    return [
        s
        for s in shipments
        if s.get("type") == "outgoing" and _is_recently_delivered(s)
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
        # Delivered parcels are included so Parcel records the terminal status
        # instead of expiring them once DHL drops them from the active set.
        await self._push_to_parcel(active + filter_recently_delivered_parcels(raw))

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
                outgoing = sent_coord.shipments_for_push
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
        self._raw: list[dict] = []

    @property
    def shipments_for_push(self) -> list[dict]:
        """Return active + recently delivered outgoing shipments for Parcel."""
        return filter_active_sent_shipments(
            self._raw
        ) + filter_recently_delivered_sent_shipments(self._raw)

    async def _async_update_data(self) -> list[dict]:
        try:
            raw = await self._client.async_get_sent_shipments()
        except (DhlApiError, aiohttp.ClientError) as err:
            raise UpdateFailed(f"DHL error (sent): {err}") from err

        self._raw = raw
        active = filter_active_sent_shipments(raw)
        _LOGGER.debug(
            "DHL sent shipments fetched: %d total, %d active", len(raw), len(active)
        )
        return active
