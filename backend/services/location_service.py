
import logging
from functools import lru_cache

from geopy.geocoders import Nominatim
from geopy.extra.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

_geolocator = Nominatim(
    user_agent="OverWatchAI-VehicleDetection/1.0",
    timeout=8,
)

_reverse = RateLimiter(
    _geolocator.reverse,
    min_delay_seconds=1,
    max_retries=1,
    swallow_exceptions=True,
)


@lru_cache(maxsize=1000)
def get_location_name(latitude: float, longitude: float) -> str:
    """Convert GPS coordinates into a readable location."""
    try:
        location = _reverse(
            (latitude, longitude),
            exactly_one=True,
            language="en",
            addressdetails=True,
        )

        if not location:
            return f"GPS: {latitude:.6f}, {longitude:.6f}"

        address = location.raw.get("address", {})

        road = address.get("road")
        suburb = (
            address.get("suburb")
            or address.get("neighbourhood")
            or address.get("quarter")
        )
        city = (
            address.get("city")
            or address.get("town")
            or address.get("village")
            or address.get("municipality")
            or address.get("county")
        )
        province = address.get("state")

        parts = list(dict.fromkeys(
            part for part in [road, suburb, city, province]
            if part
        ))

        return ", ".join(parts) or location.address

    except Exception:
        logger.exception("Reverse geocoding failed")
        return f"GPS: {latitude:.6f}, {longitude:.6f}"
