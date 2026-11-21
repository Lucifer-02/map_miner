# Map Miner 🗺️

High-performance Google Maps scraper.

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

Chromium disk caching is opt-in. Set `cache_dir=Path(...)` to enable the
Chromium disk cache flags. Sharing a cache directory across concurrent contexts
can delay search responses. Application static caching is configured separately
with `static_cache_dir` and defaults to `.cache/static_assets/`. Playwright
routing disables the browser's HTTP cache. [Playwright routing documentation](https://playwright.dev/python/docs/api/class-browsercontext#browser-context-route)

Each result has a top-level `query` column containing the original string from
`queries` (for example, `"cafe_hanoi"`). If a place appears in two searches, it
appears in two rows, one for each query. This applies to both `use_spa` modes;
the fallback mode fetches each unique place URL only once. The `query` column
remains present when `fields` selects other columns, when `flatten=True`, and
when the result is empty.

Set `on_query_incomplete` to a callback that accepts `query` and `reasons` to
receive detected failures such as query timeouts or unreadable place details.
The returned DataFrame includes any rows already collected for those queries.

## Features

- Concurrent searches and place-detail requests using Playwright, returning Polars DataFrames.
- Structured place fields including address components, categories, ratings and review counts when available.
- Preview extraction with DOM/detail fallback, timeouts and retry handling; extraction remains best effort as Google Maps changes.
- Proxy rotation and CAPTCHA diagnostics/solving attempts; browser flags do not guarantee avoidance of bot detection.
