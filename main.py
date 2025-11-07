import asyncio
import logging

from geopy import Point

from scraper import scrape_google_maps


def main():
    queries = [
        "museum",
        "place of worship",
        "local government office",
        "art gallery",
        "library",
        "gas station",
        "lodging",
        "shopping mall",
        "cafe",
        "airport",
        "transit station",
        "parking",
        "pharmacy",
        "courthouse",
        "stadium",
        "car dealer",
        "police",
        "hospital",
        "tourist attraction",
        "embassy",
        "supermarket",
        "train station",
        "bus station",
        "university",
        "restaurant",
        "bank",
        "atm",
        "amusement park",
        "movie theater",
        "school",
        "convenience store",
        "gym",
        "post office",
        "clothing store",
        "book store",
        "city hall",
        "store",
        "home goods store",
    ]
    queries_2 = ["atm", "restaurant", "hotel", "cafe", "pharmacy"]
    # logging.getLogger("root.scraper").disabled = True
    logging.getLogger("main.scraper").setLevel(logging.INFO)
    results = asyncio.run(
        scrape_google_maps(
            queries=queries,
            max_places=50,
            lang="en",
            headless=False,
            geo_coordinates=Point(10.7784382, 106.640777),
            zoom=18,
        )
    )

    logging.info(f"Length of results: {len(results)}")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    main()
