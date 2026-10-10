"""Courier map coordinates come from the order's delivery location.

Preprod defect (2026-10-06): dispatched orders delivering to Pune 411001
showed the Bengaluru stub 12.9715987,77.5945627 on Track / Open courier map.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.ondc_routes import _tracking_from_order
from config import settings

BENGALURU_STUB = "12.9715987,77.5945627"

PUNE_ADDRESS = {
    "name": "Preprod Buyer",
    "line1": "12 Preprod Test Lane",
    "city": "Pune",
    "state": "Maharashtra",
    "country": "IND",
    "postalCode": "411001",
}


def _shipped_order(address: dict | None, **fulfilment: object) -> dict:
    body: dict = {"status": "shipped", "tracking_id": "TRK-1", **fulfilment}
    if address is not None:
        body["delivery_address"] = address
    return {"order_id": "ORDER-GPS-1", "status": "shipped", "fulfilment": body}


def _coords(gps: str) -> tuple[float, float]:
    lat, lng = (float(part) for part in gps.split(","))
    return lat, lng


def _assert_pune(gps: str | None) -> None:
    assert gps, "Pune 411001 order must carry coordinates"
    lat, lng = _coords(gps)
    assert 18.40 <= lat <= 18.65, gps
    assert 73.70 <= lng <= 74.00, gps


def test_pune_411001_order_gets_pune_coordinates_not_bengaluru() -> None:
    tracked = _tracking_from_order(_shipped_order(PUNE_ADDRESS))
    location = tracked["tracking"]["location"]
    _assert_pune(location["gps"])
    assert location["gps"] != BENGALURU_STUB
    assert BENGALURU_STUB not in (tracked["tracking"]["url"] or "")
    assert tracked["tracking"]["url"] == (
        f"https://www.google.com/maps/search/?api=1&query={location['gps']}"
    )
    assert location["address"]["city"] == "Pune"
    assert location["address"]["area_code"] == "411001"


def test_city_fallback_when_pincode_unknown() -> None:
    address = {**PUNE_ADDRESS, "postalCode": "411999"}
    _assert_pune(_tracking_from_order(_shipped_order(address))["tracking"]["location"]["gps"])
    lower = {**PUNE_ADDRESS, "postalCode": "", "city": "  pune "}
    _assert_pune(_tracking_from_order(_shipped_order(lower))["tracking"]["location"]["gps"])


def test_explicit_address_coordinates_win_over_centroid() -> None:
    address = {**PUNE_ADDRESS, "lat": 18.5310, "lng": 73.8446}
    gps = _tracking_from_order(_shipped_order(address))["tracking"]["location"]["gps"]
    assert _coords(gps) == pytest.approx((18.5310, 73.8446))
    address = {**PUNE_ADDRESS, "gps": "18.5000,73.9000"}
    gps = _tracking_from_order(_shipped_order(address))["tracking"]["location"]["gps"]
    assert _coords(gps) == pytest.approx((18.5, 73.9))


def test_invalid_explicit_coordinates_are_ignored() -> None:
    address = {**PUNE_ADDRESS, "gps": "not-a-gps", "lat": 999, "lng": 73.8}
    _assert_pune(_tracking_from_order(_shipped_order(address))["tracking"]["location"]["gps"])


def test_unknown_delivery_location_omits_gps_and_fake_map() -> None:
    address = {"line1": "Somewhere", "city": "Atlantis", "postalCode": "000000"}
    for order in (_shipped_order(address), _shipped_order(None)):
        tracked = _tracking_from_order(order)
        assert tracked["tracking"]["location"]["gps"] is None
        assert tracked["tracking"]["url"] is None


def test_existing_tracking_location_and_url_are_preserved() -> None:
    order = _shipped_order(
        PUNE_ADDRESS,
        tracking_location={"gps": "18.6000,73.8000", "address": {"city": "Pune"}},
        tracking_url="https://courier.example/track/TRK-1",
    )
    tracked = _tracking_from_order(order)
    assert tracked["tracking"]["location"]["gps"] == "18.6000,73.8000"
    assert tracked["tracking"]["url"] == "https://courier.example/track/TRK-1"


def test_local_track_route_returns_pune_coordinates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "data_dir", str(tmp_path / "data"))
    monkeypatch.setattr(settings, "aadhaar_chain_env", "demo")
    from app.commerce_demo import create_item, create_order, publish_item, transition_order
    from main import app

    app.state.persistence_pool = None
    created = create_item(
        {
            "title": "Pune Atta 1kg",
            "price_inr": 89,
            "inventory": 2,
            "seller_id": "ondcseller.example",
            "seller_name": "Track Mart",
        }
    )
    publish_item(created["item"]["item_id"])
    order = create_order(
        {
            "item_id": created["item"]["item_id"],
            "quantity": 1,
            "buyer_id": "buyer-pune",
            "delivery_address": {**PUNE_ADDRESS, "phone": "9999999999"},
        }
    )["order"]
    order_id = order["order_id"]
    transition_order(order_id, "confirmed")
    transition_order(order_id, "preparing")
    transition_order(order_id, "shipped", payload={"tracking_id": "TRACK-PUNE-1"})

    tracked = TestClient(app).get(f"/api/ondc/track?order_id={order_id}")
    assert tracked.status_code == 200, tracked.text
    tracking = tracked.json()["data"]["tracking"]
    _assert_pune(tracking["location"]["gps"])
    assert BENGALURU_STUB not in tracked.text
