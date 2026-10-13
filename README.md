# Map Miner 🗺️

High-performance asynchronous Google Maps scraper using Playwright and Polars.

## Installation

```bash
pip install map_miner
playwright install chromium
```

Playwright needs its Chromium browser installed in the environment used to run
the scraper. If the executable is missing, `scrape_google_maps` raises a
`RuntimeError` with installation instructions. For this repository, run
`uv run playwright install chromium`; with pip, run
`python -m playwright install chromium` using the same Python environment.

## Quick Start

```python
import asyncio
from geopy.point import Point
from map_miner import scrape_google_maps


async def main():
    df = await scrape_google_maps(
        queries={"cafe"},
        geo_coordinates=Point(20.985322, 105.781289),
        zoom=18,
        max_places=20,
        lang="en",
        headless=True,
    )
    print(df)
    df.write_excel("places.xlsx")


if __name__ == "__main__":
    asyncio.run(main())
```

By default, results are limited to 25 km from `geo_coordinates`. Set `range_limit` (in meters) to use a different radius.

Chromium disk cache flags are enabled by default using `.cache/chromium_cache/`.
Use `cache_dir=Path(...)` to select
another directory or `cache_dir=None` to omit Chromium disk cache flags.
Application static caching remains enabled in `.cache/static_assets/`,
configured separately through `static_cache_dir`. Playwright documents that
[routing disables HTTP cache](https://playwright.dev/python/docs/api/class-browsercontext#browser-context-route);
this scraper routes requests, so those Chromium flags do not guarantee HTTP
cache reuse. The application static cache persists independently.

In live checks with
Playwright 1.63.0 / Chromium 153.0.8010.12, sharing a disk cache directory across
concurrent contexts delayed search document responses, even with a fresh cache.
Removing those flags allowed all four concurrent searches to navigate promptly.
Cached concurrent workloads can reproduce those timeouts; `cache_dir=None`
is an available workaround. This does not establish an upstream browser
regression or corruption of existing cache files.

Each result has a top-level `query` column containing the original string from
`queries` (for example, `"cafe_hanoi"`). If a place appears in two searches, it
appears in two rows, one for each query. This applies to both `use_spa` modes;
the fallback mode fetches each unique place URL only once. The `query` column
remains present when `fields` selects other columns, when `flatten=True`, and
when the result is empty.

## Features

- **Blazing Fast**: Asynchronous I/O powered by Playwright and Polars.
- **Rich Data**: Extracts 27+ structured fields including detailed address components (`street`, `sublocality`, `district`, `city`, `postal_code`, `country_code`).
- **Resilient**: Multi-layer parsing, zero DOM query fragility, adaptive waits, and retry loop.
- **Anti-Bot & Stealth**: Integrated CAPTCHA handling and undetected browser flags.
