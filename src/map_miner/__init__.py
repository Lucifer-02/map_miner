"""
map-miner: High-performance asynchronous Google Maps scraper using Playwright.
"""

from .extractor import (
    DEFAULT_FLATTEN_COLUMNS,
    REQUIRED_COLUMNS,
    calculate_distance,
    extract_coordinates_from_url,
    extract_feed_item_dom,
    extract_place_data,
    format_places_dataframe,
    is_preview_response_for_link,
    is_within_range,
    make_place_url,
)
from .proxy import (
    DEFAULT_PROXY_BYPASS,
    DEFAULT_TOR_RENEW_COOLDOWN,
    ProxyRotator,
    get_tor_rotating_proxy,
    renew_tor_circuit_control,
)
from .recaptcha_solver import (
    RecaptchaBlockedError,
    RecaptchaError,
    RecaptchaSolveError,
    RecaptchaSolver,
)
from .scraper import (
    scrape_google_maps,
)

__version__ = "0.3.5"

__all__ = [
    "DEFAULT_FLATTEN_COLUMNS",
    "DEFAULT_PROXY_BYPASS",
    "DEFAULT_TOR_RENEW_COOLDOWN",
    "REQUIRED_COLUMNS",
    "ProxyRotator",
    "RecaptchaBlockedError",
    "RecaptchaError",
    "RecaptchaSolveError",
    "RecaptchaSolver",
    "__version__",
    "calculate_distance",
    "extract_coordinates_from_url",
    "extract_feed_item_dom",
    "extract_place_data",
    "format_places_dataframe",
    "get_tor_rotating_proxy",
    "is_preview_response_for_link",
    "is_within_range",
    "make_place_url",
    "renew_tor_circuit_control",
    "scrape_google_maps",
]
