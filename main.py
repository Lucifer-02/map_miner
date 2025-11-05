import asyncio

from geopy import Point

from scraper import scrape_google_maps

results = asyncio.run(
    scrape_google_maps(
        query="atm",
        max_places=20,
        lang="en",
        headless=False,
        geo_coordinates=Point(10.7784382, 106.640777),
        zoom=18.5,
    )
)

print(results)
