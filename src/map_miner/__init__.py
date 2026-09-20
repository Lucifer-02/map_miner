"""
map-miner: High-performance asynchronous Google Maps scraper using Playwright.
"""

from .extractor import (
    DEFAULT_FLATTEN_COLUMNS,
    REQUIRED_COLUMNS,
    calculate_distance,
    extract_coordinates_from_url,
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
    DEFAULT_CAPTCHA_TIMEOUT,
    DEFAULT_CONFIG,
    DEFAULT_RANGE_LIMIT,
    DEFAULT_SPA_PREVIEW_TIMEOUT,
    DEFAULT_STATIC_CACHE_DIR,
    ScraperConfig,
    create_browser_context,
    is_no_results_page,
    scrape_google_maps,
)

__version__ = "0.3.3"

__all__ = [
    "DEFAULT_CAPTCHA_TIMEOUT",
    "DEFAULT_CONFIG",
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
    "ScraperConfig",
    "__version__",
    "calculate_distance",
    "create_browser_context",
    "extract_coordinates_from_url",
    "extract_place_data",
    "format_places_dataframe",
    "get_tor_rotating_proxy",
    "is_no_results_page",
    "is_preview_response_for_link",
    "is_within_range",
    "make_place_url",
    "renew_tor_circuit_control",
    "scrape_google_maps",
]
