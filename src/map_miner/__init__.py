"""
map-miner: High-performance asynchronous Google Maps scraper using Playwright.
"""

from .extractor import extract_place_data
from .recaptcha_solver import RecaptchaSolver
from .scraper import (
    DEFAULT_PROXY_BYPASS,
    ProxyRotator,
    create_browser_context,
    extract_coordinates_from_url,
    scrape_google_maps,
)

__version__ = "0.2.2"

__all__ = [
    "DEFAULT_PROXY_BYPASS",
    "ProxyRotator",
    "RecaptchaSolver",
    "__version__",
    "create_browser_context",
    "extract_coordinates_from_url",
    "extract_place_data",
    "scrape_google_maps",
]
