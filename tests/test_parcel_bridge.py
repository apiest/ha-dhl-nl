"""Tests for the DHL NL → Parcel bridge."""

from __future__ import annotations

from datetime import UTC, datetime


from custom_components.dhl_nl.const import (
    STATUS_AT_SERVICE_POINT,
    STATUS_COLLECTED_AT_SERVICE_POINT,
)
from custom_components.dhl_nl.parcel_bridge import (
    _extract_delivery_window,
    _map_status,
    _parse_iso,
    transform_to_shipment,
)
from custom_components.parcel.models import (
    ShipmentDirection,
    ShipmentStatus,
)


# ── _parse_iso ──


class TestParseIso:
    """Tests for the _parse_iso helper."""

    def test_none_returns_none(self) -> None:
        assert _parse_iso(None) is None

    def test_empty_string_returns_none(self) -> None:
        assert _parse_iso("") is None

    def test_invalid_string_returns_none(self) -> None:
        assert _parse_iso("not-a-date") is None

    def test_aware_datetime_preserved(self) -> None:
        result = _parse_iso("2026-05-29T14:00:00+02:00")
        assert result is not None
        assert result.tzinfo is not None

    def test_naive_datetime_gets_utc(self) -> None:
        """Offset-naive strings must be promoted to UTC to prevent comparison errors."""
        result = _parse_iso("2026-05-29T14:00:00")
        assert result is not None
        assert result.tzinfo is UTC
        assert result == datetime(2026, 5, 29, 14, 0, 0, tzinfo=UTC)

    def test_utc_z_suffix(self) -> None:
        result = _parse_iso("2026-05-29T14:00:00Z")
        assert result is not None
        assert result.tzinfo is not None

    def test_all_parsed_datetimes_are_aware(self) -> None:
        """Regression: ensure no _parse_iso output can be offset-naive."""
        samples = [
            "2026-05-29T14:00:00",
            "2026-05-29T14:00:00Z",
            "2026-05-29T14:00:00+00:00",
            "2026-05-29T14:00:00+02:00",
            "2026-01-01",
        ]
        for sample in samples:
            result = _parse_iso(sample)
            if result is not None:
                assert result.tzinfo is not None, (
                    f"_parse_iso({sample!r}) returned offset-naive datetime"
                )


# ── _map_status ──


class TestMapStatus:
    """Tests for the _map_status helper."""

    def test_delivered_category(self) -> None:
        assert (
            _map_status({"category": "DELIVERED", "status": ""})
            == ShipmentStatus.DELIVERED
        )

    def test_delivered_in_status_text(self) -> None:
        assert (
            _map_status({"category": "IN_DELIVERY", "status": "DELIVERED_IN_MAILBOX"})
            == ShipmentStatus.DELIVERED
        )

    def test_service_point_pickup_ready(self) -> None:
        assert (
            _map_status({"category": "IN_DELIVERY", "status": STATUS_AT_SERVICE_POINT})
            == ShipmentStatus.PICKUP_READY
        )

    def test_service_point_collected(self) -> None:
        assert (
            _map_status(
                {"category": "IN_DELIVERY", "status": STATUS_COLLECTED_AT_SERVICE_POINT}
            )
            == ShipmentStatus.DELIVERED
        )

    def test_data_received(self) -> None:
        assert (
            _map_status({"category": "DATA_RECEIVED", "status": ""})
            == ShipmentStatus.ANNOUNCED
        )

    def test_in_delivery(self) -> None:
        assert (
            _map_status({"category": "IN_DELIVERY", "status": "SOME_STATUS"})
            == ShipmentStatus.IN_TRANSIT
        )

    def test_underway(self) -> None:
        assert (
            _map_status({"category": "UNDERWAY", "status": ""})
            == ShipmentStatus.IN_TRANSIT
        )

    def test_customs(self) -> None:
        assert (
            _map_status({"category": "CUSTOMS", "status": ""})
            == ShipmentStatus.IN_TRANSIT
        )

    def test_exception(self) -> None:
        assert (
            _map_status({"category": "EXCEPTION", "status": ""})
            == ShipmentStatus.EXCEPTION
        )

    def test_problem(self) -> None:
        assert (
            _map_status({"category": "PROBLEM", "status": ""})
            == ShipmentStatus.EXCEPTION
        )

    def test_unknown_category(self) -> None:
        assert (
            _map_status({"category": "UNKNOWN", "status": ""}) == ShipmentStatus.UNKNOWN
        )

    def test_missing_fields(self) -> None:
        assert _map_status({}) == ShipmentStatus.UNKNOWN


# ── _extract_delivery_window ──


class TestExtractDeliveryWindow:
    """Tests for the _extract_delivery_window helper."""

    def test_no_indication(self) -> None:
        assert _extract_delivery_window({}) == (None, None, None)

    def test_none_indication(self) -> None:
        assert _extract_delivery_window({"receivingTimeIndication": None}) == (
            None,
            None,
            None,
        )

    def test_moment_indication(self) -> None:
        parcel = {
            "receivingTimeIndication": {
                "indicationType": "MomentIndication",
                "moment": "2026-05-29T14:00:00Z",
            }
        }
        expected_at, expected_from, expected_to = _extract_delivery_window(parcel)
        assert expected_at is not None
        assert expected_from is None
        assert expected_to is None

    def test_interval_indication(self) -> None:
        parcel = {
            "receivingTimeIndication": {
                "indicationType": "IntervalIndication",
                "start": "2026-05-29T08:00:00Z",
                "end": "2026-05-29T16:00:00Z",
            }
        }
        expected_at, expected_from, expected_to = _extract_delivery_window(parcel)
        assert expected_at is None
        assert expected_from is not None
        assert expected_to is not None

    def test_unknown_indication_type(self) -> None:
        parcel = {
            "receivingTimeIndication": {
                "indicationType": "SomeNewType",
            }
        }
        assert _extract_delivery_window(parcel) == (None, None, None)


# ── transform_to_shipment ──


class TestTransformToShipment:
    """Tests for the transform_to_shipment function."""

    @staticmethod
    def _make_parcel(**overrides) -> dict:
        defaults = {
            "barcode": "JVGL12345678901234567890",
            "status": "SOME_STATUS",
            "category": "IN_DELIVERY",
            "sender": {"name": "Test Sender BV"},
            "receiver": {
                "name": "J. Doe",
                "address": {"countryCode": "NL"},
            },
            "destination": {"locationType": "ADDRESS"},
            "receivingTimeIndication": None,
        }
        defaults.update(overrides)
        return defaults

    def test_basic_inbound(self) -> None:
        parcel = self._make_parcel()
        shipment = transform_to_shipment(parcel, ShipmentDirection.INBOUND)
        assert shipment.shipment_id == "dhl_nl:JVGL12345678901234567890"
        assert shipment.provider == "dhl_nl"
        assert shipment.carrier == "dhl"
        assert shipment.direction == ShipmentDirection.INBOUND
        assert shipment.title == "Test Sender BV"

    def test_outbound_uses_receiver_name(self) -> None:
        parcel = self._make_parcel()
        shipment = transform_to_shipment(parcel, ShipmentDirection.OUTBOUND)
        assert shipment.title == "J. Doe"

    def test_all_datetime_fields_are_aware(self) -> None:
        """Regression: all datetime fields on the Shipment must be timezone-aware."""
        parcel = self._make_parcel(
            receivingTimeIndication={
                "indicationType": "IntervalIndication",
                "start": "2026-05-29T08:00:00",
                "end": "2026-05-29T16:00:00",
            },
        )
        shipment = transform_to_shipment(parcel, ShipmentDirection.INBOUND)

        for field_name in (
            "expected_from",
            "expected_to",
            "expected_at",
            "delivered_at",
            "last_updated",
        ):
            value = getattr(shipment, field_name)
            if value is not None:
                assert value.tzinfo is not None, (
                    f"Shipment.{field_name} is offset-naive — will crash on comparison"
                )

    def test_moment_indication_populates_expected_at(self) -> None:
        parcel = self._make_parcel(
            receivingTimeIndication={
                "indicationType": "MomentIndication",
                "moment": "2026-05-29T14:00:00Z",
            },
        )
        shipment = transform_to_shipment(parcel, ShipmentDirection.INBOUND)
        assert shipment.expected_at is not None
        assert shipment.expected_at.tzinfo is not None

    def test_service_point_maps_to_pickup_ready(self) -> None:
        parcel = self._make_parcel(status=STATUS_AT_SERVICE_POINT)
        shipment = transform_to_shipment(parcel, ShipmentDirection.INBOUND)
        assert shipment.status == ShipmentStatus.PICKUP_READY

    def test_tracking_url(self) -> None:
        parcel = self._make_parcel()
        shipment = transform_to_shipment(parcel, ShipmentDirection.INBOUND)
        assert "JVGL12345678901234567890" in shipment.tracking_url

    def test_delivered_sets_delivered_at(self) -> None:
        parcel = self._make_parcel(category="DELIVERED", status="DELIVERED")
        shipment = transform_to_shipment(parcel, ShipmentDirection.INBOUND)
        assert shipment.status == ShipmentStatus.DELIVERED
        assert shipment.delivered_at is not None
        assert shipment.delivered_at.tzinfo is not None
