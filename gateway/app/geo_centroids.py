"""Approximate delivery coordinates for courier-map links.

Used only when an order carries no explicit GPS. Values are approximate
public centroids (pincode head post-office area or city centre), good enough
to open a map near the buyer's delivery area. Unknown places return ``None``
so callers omit GPS rather than inventing a location.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Optional

# Exact 6-digit pincode -> (lat, lng). Head post-office / CBD areas.
PINCODE_CENTROIDS: dict[str, tuple[float, float]] = {
    "411001": (18.5167, 73.8762),  # Pune Camp / Pune GPO area
    "400001": (18.9388, 72.8354),  # Mumbai Fort
    "110001": (28.6315, 77.2167),  # New Delhi Connaught Place
    "560001": (12.9767, 77.5993),  # Bengaluru GPO / MG Road
    "600001": (13.0896, 80.2879),  # Chennai George Town
    "500001": (17.3891, 78.4747),  # Hyderabad Abids
    "700001": (22.5726, 88.3509),  # Kolkata BBD Bagh
    "380001": (23.0258, 72.5873),  # Ahmedabad Old City
}

# Lower-cased city name -> (lat, lng) city centre.
CITY_CENTROIDS: dict[str, tuple[float, float]] = {
    "pune": (18.5204, 73.8567),
    "mumbai": (19.0760, 72.8777),
    "bombay": (19.0760, 72.8777),
    "delhi": (28.6139, 77.2090),
    "new delhi": (28.6139, 77.2090),
    "bengaluru": (12.9716, 77.5946),
    "bangalore": (12.9716, 77.5946),
    "chennai": (13.0827, 80.2707),
    "hyderabad": (17.3850, 78.4867),
    "kolkata": (22.5726, 88.3639),
    "ahmedabad": (23.0225, 72.5714),
    "gurugram": (28.4595, 77.0266),
    "gurgaon": (28.4595, 77.0266),
    "noida": (28.5355, 77.3910),
    "jaipur": (26.9124, 75.7873),
    "lucknow": (26.8467, 80.9462),
}

_PIN_RE = re.compile(r"^\d{6}$")


def format_gps(lat: float, lng: float) -> str:
    return f"{lat:.6f},{lng:.6f}"


def _valid(lat: float, lng: float) -> bool:
    return -90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0 and (lat, lng) != (0.0, 0.0)


def parse_gps(value: Any) -> Optional[tuple[float, float]]:
    """Parse ``"lat,lng"`` into floats; ``None`` when malformed or out of range."""
    if not isinstance(value, str) or "," not in value:
        return None
    left, _, right = value.partition(",")
    try:
        lat, lng = float(left.strip()), float(right.strip())
    except ValueError:
        return None
    return (lat, lng) if _valid(lat, lng) else None


def _first(mapping: dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return value
    return None


def explicit_coordinates(location: Any) -> Optional[tuple[float, float]]:
    """Coordinates stored on an address/location dict (gps or lat+lng)."""
    if not isinstance(location, dict):
        return None
    parsed = parse_gps(location.get("gps"))
    if parsed:
        return parsed
    lat = _first(location, ("lat", "latitude"))
    lng = _first(location, ("lng", "lon", "long", "longitude"))
    if lat is None or lng is None:
        return None
    try:
        lat_f, lng_f = float(lat), float(lng)
    except (TypeError, ValueError):
        return None
    return (lat_f, lng_f) if _valid(lat_f, lng_f) else None


def centroid_for_address(address: Any) -> Optional[tuple[float, float]]:
    """Pincode centroid first, then city centre; ``None`` when unknown."""
    if not isinstance(address, dict):
        return None
    pin = str(
        _first(address, ("postalCode", "postal_code", "pincode", "pin", "area_code"))
        or ""
    ).strip()
    if _PIN_RE.match(pin) and pin in PINCODE_CENTROIDS:
        return PINCODE_CENTROIDS[pin]
    city = " ".join(str(address.get("city") or "").split()).lower()
    return CITY_CENTROIDS.get(city)


__all__ = [
    "CITY_CENTROIDS",
    "PINCODE_CENTROIDS",
    "centroid_for_address",
    "explicit_coordinates",
    "format_gps",
    "parse_gps",
]
