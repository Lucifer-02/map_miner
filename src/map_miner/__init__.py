"""
map-miner: High-performance asynchronous Google Maps scraper using Playwright.
"""

from .extractor import extract_place_data
from .recaptcha_solver import RecaptchaSolver
from .scraper import scrape_google_maps

__version__ = "0.1.1"

__all__ = [
    "RecaptchaSolver",
    "__version__",
    "extract_place_data",
    "scrape_google_maps",
]
