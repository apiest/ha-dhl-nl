"""Tests for coordinator filter functions and error handling."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.dhl_nl.api import DhlApiError
from custom_components.dhl_nl.coordinator import (
    DhlCoordinator,
    filter_active_parcels,
    filter_active_sent_shipments,
    filter_recently_delivered_parcels,
    filter_recently_delivered_sent_shipments,
)

# ---------------------------------------------------------------------------
# filter_active_parcels
# ---------------------------------------------------------------------------


def _parcel(category: str, is_return: bool = False) -> dict:
    return {"barcode": "TEST123", "category": category, "isReturn": is_return}


def test_active_parcel_is_included():
    assert filter_active_parcels([_parcel("IN_DELIVERY")]) != []


def test_delivered_parcel_is_excluded():
    assert filter_active_parcels([_parcel("DELIVERED")]) == []


def test_return_parcel_is_excluded():
    assert filter_active_parcels([_parcel("IN_DELIVERY", is_return=True)]) == []


def test_all_active_categories_pass():
    from custom_components.dhl_nl.const import ACTIVE_CATEGORIES

    parcels = [_parcel(cat) for cat in ACTIVE_CATEGORIES]
    assert len(filter_active_parcels(parcels)) == len(ACTIVE_CATEGORIES)


def test_mixed_parcels_filtered_correctly():
    parcels = [
        _parcel("IN_DELIVERY"),
        _parcel("DELIVERED"),
        _parcel("IN_DELIVERY", is_return=True),
        _parcel("UNDERWAY"),
    ]
    result = filter_active_parcels(parcels)
    assert len(result) == 2


def test_empty_list_returns_empty():
    assert filter_active_parcels([]) == []


# ---------------------------------------------------------------------------
# filter_active_sent_shipments
# ---------------------------------------------------------------------------


def _shipment(category: str, shipment_type: str = "outgoing") -> dict:
    return {"barcode": "SENT123", "category": category, "type": shipment_type}


def test_active_outgoing_shipment_is_included():
    assert filter_active_sent_shipments([_shipment("IN_DELIVERY")]) != []


def test_delivered_shipment_is_excluded():
    assert filter_active_sent_shipments([_shipment("DELIVERED")]) == []


def test_non_outgoing_type_is_excluded():
    assert (
        filter_active_sent_shipments(
            [_shipment("IN_DELIVERY", shipment_type="incoming")]
        )
        == []
    )


# ---------------------------------------------------------------------------
# DhlCoordinator error handling
# ---------------------------------------------------------------------------


async def test_coordinator_raises_update_failed_on_api_error(hass):
    from homeassistant.helpers.update_coordinator import UpdateFailed

    client = MagicMock()
    client.async_get_parcels = AsyncMock(side_effect=DhlApiError("401"))

    coordinator = DhlCoordinator(hass, client)

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_coordinator_returns_only_active_parcels(hass):
    client = MagicMock()
    client.async_get_parcels = AsyncMock(
        return_value=[
            _parcel("IN_DELIVERY"),
            _parcel("DELIVERED"),
            _parcel("IN_DELIVERY", is_return=True),
        ]
    )

    coordinator = DhlCoordinator(hass, client)
    result = await coordinator._async_update_data()

    assert len(result) == 1
    assert result[0]["category"] == "IN_DELIVERY"


# ---------------------------------------------------------------------------
# Recently-delivered retention (keeps Parcel from mis-expiring delivered items)
# ---------------------------------------------------------------------------


def _delivered_parcel(days_ago: float | None = 0, **extra) -> dict:
    parcel = {"barcode": "DEL123", "category": "DELIVERED", "isReturn": False}
    if days_ago is not None:
        parcel["receivedDaysAgo"] = days_ago
    parcel.update(extra)
    return parcel


def test_recently_delivered_parcel_is_included():
    assert filter_recently_delivered_parcels([_delivered_parcel(1)]) != []


def test_delivered_parcel_outside_window_is_excluded():
    assert filter_recently_delivered_parcels([_delivered_parcel(30)]) == []


def test_delivered_parcel_on_window_boundary_is_included():
    assert filter_recently_delivered_parcels([_delivered_parcel(7)]) != []


def test_delivered_parcel_without_timestamp_is_included():
    """An undeterminable delivery date must never cause a mis-expiry."""
    assert filter_recently_delivered_parcels([_delivered_parcel(None)]) != []


def test_active_parcel_is_not_recently_delivered():
    assert filter_recently_delivered_parcels([_parcel("IN_DELIVERY")]) == []


def test_delivered_return_parcel_is_excluded():
    assert (
        filter_recently_delivered_parcels([_delivered_parcel(1, isReturn=True)]) == []
    )


def test_delivered_parcel_uses_receiving_time_indication():
    recent = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    parcel = _delivered_parcel(None, receivingTimeIndication={"moment": recent})
    assert filter_recently_delivered_parcels([parcel]) != []

    old = (datetime.now(UTC) - timedelta(days=20)).isoformat()
    parcel = _delivered_parcel(None, receivingTimeIndication={"moment": old})
    assert filter_recently_delivered_parcels([parcel]) == []


def test_delivered_parcel_falls_back_to_time_created():
    old = (datetime.now(UTC) - timedelta(days=20)).isoformat()
    parcel = _delivered_parcel(None, timeCreated=old)
    assert filter_recently_delivered_parcels([parcel]) == []


def test_recently_delivered_sent_shipment_is_included():
    shipment = {"barcode": "S1", "category": "DELIVERED", "type": "outgoing"}
    assert filter_recently_delivered_sent_shipments([shipment]) != []


def test_delivered_sent_shipment_wrong_type_is_excluded():
    shipment = {"barcode": "S1", "category": "DELIVERED", "type": "incoming"}
    assert filter_recently_delivered_sent_shipments([shipment]) == []


async def test_coordinator_pushes_delivered_parcels_to_parcel(hass):
    """coordinator.data stays active-only, but the push includes delivered."""
    client = MagicMock()
    client.async_get_parcels = AsyncMock(
        return_value=[_parcel("IN_DELIVERY"), _delivered_parcel(1)]
    )

    coordinator = DhlCoordinator(hass, client)
    pushed: list[list[dict]] = []

    async def _capture(parcels):
        pushed.append(parcels)

    coordinator._push_to_parcel = _capture

    result = await coordinator._async_update_data()

    assert [p["category"] for p in result] == ["IN_DELIVERY"]
    assert {p["category"] for p in pushed[0]} == {"IN_DELIVERY", "DELIVERED"}
