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
            queries=set(queries_2),
            max_places=120,
            lang="en",
            headless=True,
            # geo_coordinates=Point(10.7784382, 106.640777),
            geo_coordinates=Point(21.037912, 105.821952),
            zoom=18,
            # proxy={
            #     "server": "http://103.162.31.234:49060",
            #     "username": "user49060",
            #     "password": "zDBKBdlIO4",
            # },
            # proxy={
            #     "server": "http://154.202.3.40:49230",
            #     "username": "user49230",
            #     "password": "GQJ62IBqX2",
            # },
            proxy=None,
        )
    )

    logging.info(results)
    logging.info(f"Length of results: {len(results)}")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    main()
