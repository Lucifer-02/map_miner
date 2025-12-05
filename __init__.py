# map_miner/__init__.py
"""Top-level package for map_miner."""

# Package version (simple)
__version__ = "0.0.1"

# Re-export commonly used objects so users can do:
#   from map_miner import Scraper, extract_map
from .extractor import extract_place_data
from .RecaptchaSolver import RecaptchaSolver

__all__ = ["__version__", "extract_place_data", "RecaptchaSolver"]
