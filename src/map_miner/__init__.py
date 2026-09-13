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
    DEFAULT_SPA_PREVIEW_TIMEOUT,
    create_browser_context,
    extract_coordinates_from_url,
    scrape_google_maps,
)

__version__ = "0.3.0"

__all__ = [
    "DEFAULT_CAPTCHA_TIMEOUT",
    "DEFAULT_PROXY_BYPASS",
    "DEFAULT_SPA_PREVIEW_TIMEOUT",
    "DEFAULT_TOR_RENEW_COOLDOWN",
    "ProxyRotator",
    "RecaptchaBlockedError",
    "RecaptchaError",
    "RecaptchaSolveError",
    "RecaptchaSolver",
    "__version__",
    "create_browser_context",
    "extract_coordinates_from_url",
    "extract_place_data",
    "get_tor_rotating_proxy",
    "renew_tor_circuit_control",
    "scrape_google_maps",
]
