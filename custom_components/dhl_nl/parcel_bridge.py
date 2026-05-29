"""Bridge between DHL NL and the Parcel integration.

Transforms DHL raw parcel dicts into normalized ``Shipment`` instances and
pushes them to the Parcel coordinator.  The bridge is conditional: when the
Parcel integration is not loaded, all push calls are silently skipped so that
DHL NL continues to work standalone.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from custom_components.parcel.const import (
    EVENT_STATUS_CHANGED,
    ProviderCapability,
)
from custom_components.parcel.coordinator import ParcelCoordinator
from custom_components.parcel.models import (
    Shipment,
    ShipmentDirection,
    ShipmentKind,
    ShipmentStatus,
    ShipmentUpdate,
)
from custom_components.parcel.provider import (
    ProviderAdapter,
    ProviderInfo,
    TrackingRequest,
    async_register_provider,
    async_unregister_provider,
    compile_tracking_patterns,
)

from .const import STATUS_AT_SERVICE_POINT, STATUS_COLLECTED_AT_SERVICE_POINT

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

PROVIDER_NAME = "dhl_nl"

# DHL NL barcode formats:
#  - JVGL + 20 digits: DHL eCommerce / DHL Parcel NL
#  - JJD + 18-21 digits: DHL Express (international waybills)
_DHL_TRACKING_PATTERNS = compile_tracking_patterns(
    r"JVGL\d{20}",
    r"JJD\d{18,21}",
)

_DHL_TRACKING_URL = "https://www.dhl.com/nl-nl/home/traceren.html?tracking-id={code}"

# ── Status mapping ──

# Category-level mapping (coarse).
_CATEGORY_STATUS_MAP: dict[str, ShipmentStatus] = {
    "DELIVERED": ShipmentStatus.DELIVERED,
    "DATA_RECEIVED": ShipmentStatus.ANNOUNCED,
    "IN_DELIVERY": ShipmentStatus.IN_TRANSIT,
    "UNDERWAY": ShipmentStatus.IN_TRANSIT,
    "LEG": ShipmentStatus.IN_TRANSIT,
    "CUSTOMS": ShipmentStatus.IN_TRANSIT,
    "INTERVENTION": ShipmentStatus.IN_TRANSIT,
    "EXCEPTION": ShipmentStatus.EXCEPTION,
    "PROBLEM": ShipmentStatus.EXCEPTION,
    "UNKNOWN": ShipmentStatus.UNKNOWN,
}


def _map_status(parcel: dict) -> ShipmentStatus:
    """Map a DHL parcel dict to a ShipmentStatus.

    Uses the granular ``status`` field for ServicePoint detection, then
    falls back to the coarse ``category`` field.
    """
    status = parcel.get("status", "")
    category = parcel.get("category", "")

    # ServicePoint: ready for pickup.
    if status == STATUS_AT_SERVICE_POINT:
        return ShipmentStatus.PICKUP_READY

    # ServicePoint: collected (delivered).
    if status == STATUS_COLLECTED_AT_SERVICE_POINT:
        return ShipmentStatus.DELIVERED

    # Granular "delivered" in status text.
    if "DELIVERED" in status.upper():
        return ShipmentStatus.DELIVERED

    return _CATEGORY_STATUS_MAP.get(category, ShipmentStatus.UNKNOWN)


def _parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO datetime string, returning None on failure.

    Ensures the result is always timezone-aware (defaults to UTC when
    the source string lacks timezone info).
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _extract_delivery_window(
    parcel: dict,
) -> tuple[datetime | None, datetime | None, datetime | None]:
    """Extract expected_at, expected_from, expected_to from receivingTimeIndication.

    Returns:
        (expected_at, expected_from, expected_to)
    """
    rti = parcel.get("receivingTimeIndication")
    if not rti:
        return None, None, None

    indication_type = rti.get("indicationType", "")
    if indication_type == "MomentIndication":
        return _parse_iso(rti.get("moment")), None, None
    if indication_type == "IntervalIndication":
        return None, _parse_iso(rti.get("start")), _parse_iso(rti.get("end"))
    return None, None, None


def transform_to_shipment(
    parcel: dict,
    direction: ShipmentDirection,
) -> Shipment:
    """Convert a DHL raw parcel dict to a normalized Parcel Shipment."""
    barcode = parcel.get("barcode", "")
    now = datetime.now(UTC)
    expected_at, expected_from, expected_to = _extract_delivery_window(parcel)

    # Title: sender name for inbound, receiver name for outbound.
    if direction == ShipmentDirection.INBOUND:
        title = parcel.get("sender", {}).get("name") or barcode
    else:
        title = parcel.get("receiver", {}).get("name") or barcode

    # Destination country from address data.
    dest_address = parcel.get("receiver", {}).get("address") or {}
    dest_country = dest_address.get("countryCode") or dest_address.get("country")

    status = _map_status(parcel)

    # Derive delivered_at for terminal statuses.
    delivered_at: datetime | None = None
    if status == ShipmentStatus.DELIVERED:
        delivered_at = expected_at or now

    return Shipment(
        shipment_id=f"{PROVIDER_NAME}:{barcode}",
        provider=PROVIDER_NAME,
        carrier="dhl",
        shipment_kind=ShipmentKind.PACKAGE,
        direction=direction,
        tracking_code=barcode,
        title=title,
        status=status,
        status_message=parcel.get("status"),
        expected_at=expected_at,
        expected_from=expected_from,
        expected_to=expected_to,
        delivered_at=delivered_at,
        tracking_url=_DHL_TRACKING_URL.format(code=barcode),
        origin_country="NL",
        destination_country=dest_country,
        last_updated=now,
        provider_metadata={
            k: v
            for k, v in {
                "category": parcel.get("category"),
                "destination_type": parcel.get("destination", {}).get("locationType"),
            }.items()
            if v is not None
        },
    )


def _get_parcel_coordinator(hass: HomeAssistant) -> ParcelCoordinator | None:
    """Retrieve the Parcel coordinator from the first loaded parcel config entry."""
    entries = hass.config_entries.async_entries("parcel")
    for entry in entries:
        if hasattr(entry, "runtime_data") and entry.runtime_data is not None:
            return entry.runtime_data  # type: ignore[return-value]
    return None


async def push_to_parcel(
    hass: HomeAssistant,
    incoming: list[dict],
    outgoing: list[dict],
) -> None:
    """Transform DHL parcels and push them to the Parcel coordinator."""
    coordinator = _get_parcel_coordinator(hass)
    if coordinator is None:
        return

    updates: list[ShipmentUpdate] = []
    known_ids: set[str] = set()

    for parcel in incoming:
        shipment = transform_to_shipment(parcel, ShipmentDirection.INBOUND)
        known_ids.add(shipment.shipment_id)
        updates.append(
            ShipmentUpdate(shipment=shipment, event_type=EVENT_STATUS_CHANGED)
        )

    for parcel in outgoing:
        shipment = transform_to_shipment(parcel, ShipmentDirection.OUTBOUND)
        known_ids.add(shipment.shipment_id)
        updates.append(
            ShipmentUpdate(shipment=shipment, event_type=EVENT_STATUS_CHANGED)
        )

    if updates:
        await coordinator.async_push_updates(updates)

    await coordinator.async_reconcile_provider(PROVIDER_NAME, known_ids)

    _LOGGER.debug(
        "Pushed %d DHL shipments to Parcel (%d incoming, %d outgoing)",
        len(updates),
        len(incoming),
        len(outgoing),
    )


class DhlProviderAdapter(ProviderAdapter):
    """Parcel provider adapter for DHL NL."""

    provider = PROVIDER_NAME
    capabilities = ProviderCapability.ADD_TRACKING | ProviderCapability.REMOVE_TRACKING

    async def async_add_tracking(self, request: TrackingRequest) -> Shipment:
        """Create an announced shipment for a DHL tracking code.

        The shipment will be updated with real data on the next DHL
        coordinator poll if the barcode matches the user's account.
        """
        now = datetime.now(UTC)
        return Shipment(
            shipment_id=f"{PROVIDER_NAME}:{request.tracking_code}",
            provider=PROVIDER_NAME,
            carrier="dhl",
            shipment_kind=ShipmentKind.PACKAGE,
            tracking_code=request.tracking_code,
            title=request.title or request.tracking_code,
            status=ShipmentStatus.ANNOUNCED,
            tracking_url=_DHL_TRACKING_URL.format(code=request.tracking_code),
            origin_country="NL",
            destination_country="NL",
            last_updated=now,
        )

    async def async_remove_tracking(self, shipment_id: str) -> None:
        """Remove tracking (no-op — Parcel handles expiration)."""


def register_provider(hass: HomeAssistant) -> None:
    """Register DHL NL as a Parcel provider."""
    adapter = DhlProviderAdapter()
    async_register_provider(
        hass,
        ProviderInfo(
            name=PROVIDER_NAME,
            capabilities=adapter.capabilities,
            adapter=adapter,
            tracking_patterns=_DHL_TRACKING_PATTERNS,
        ),
    )


def unregister_provider(hass: HomeAssistant) -> None:
    """Unregister DHL NL from the Parcel provider registry."""
    async_unregister_provider(hass, PROVIDER_NAME)
