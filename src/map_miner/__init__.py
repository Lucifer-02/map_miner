"""
map-miner: High-performance asynchronous Google Maps scraper using Playwright.
"""

from .extractor import extract_place_data
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
    DEFAULT_CAPTCHA_TIMEOUT,
    DEFAULT_FLATTEN_COLUMNS,
    DEFAULT_RANGE_LIMIT,
    DEFAULT_SPA_PREVIEW_TIMEOUT,
    DEFAULT_STATIC_CACHE_DIR,
    REQUIRED_COLUMNS,
    create_browser_context,
    extract_coordinates_from_url,
    format_places_dataframe,
    scrape_google_maps,
)

__version__ = "0.3.1"

__all__ = [
    "DEFAULT_CAPTCHA_TIMEOUT",
    "DEFAULT_FLATTEN_COLUMNS",
    "DEFAULT_PROXY_BYPASS",
    "DEFAULT_RANGE_LIMIT",
    "DEFAULT_SPA_PREVIEW_TIMEOUT",
    "DEFAULT_STATIC_CACHE_DIR",
    "DEFAULT_TOR_RENEW_COOLDOWN",
    "REQUIRED_COLUMNS",
    "ProxyRotator",
    "RecaptchaBlockedError",
    "RecaptchaError",
    "RecaptchaSolveError",
    "RecaptchaSolver",
    "__version__",
    "create_browser_context",
    "extract_coordinates_from_url",
    "extract_place_data",
    "format_places_dataframe",
    "get_tor_rotating_proxy",
    "renew_tor_circuit_control",
    "scrape_google_maps",
]
