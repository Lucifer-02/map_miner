# Map Miner 🗺️

High-performance asynchronous Google Maps scraper using Playwright and Polars.

## Installation

```bash
pip install map_miner
playwright install chromium
```

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

## Features

- **Blazing Fast**: Asynchronous I/O powered by Playwright and Polars.
- **Rich Data**: Extracts 27+ structured fields including detailed address components (`street`, `sublocality`, `district`, `city`, `postal_code`, `country_code`).
- **Resilient**: Multi-layer parsing, zero DOM query fragility, adaptive waits, and retry loop.
- **Anti-Bot & Stealth**: Integrated CAPTCHA handling and undetected browser flags.
