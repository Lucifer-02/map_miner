import asyncio
import logging

from geopy.point import Point

from map_miner import DEFAULT_PROXY_BYPASS, scrape_google_maps

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)


def main():
    pois = asyncio.run(
        scrape_google_maps(
            queries={
                "cafe",
                "atm",
                # "hospital",
                # "gym",
                # "store",
                # "restaurant",
                "bank",
                # "gas",
            },
            max_places=2,
            lang="en",
            headless=False,
            geo_coordinates=Point(21.018785, 105.830415),
            zoom=18,
            fields=None,
            proxy={
                "server": "http://gate.decodo.com:10000",
                "username": "spp86iv7zu",
                "password": "6yoqpXiuaF5bT_83sV",
                "bypass": DEFAULT_PROXY_BYPASS,
            },
            n_semaphore=12,
        )
    )

    print(pois)
    print(pois.columns)
    pois.write_excel("out.xlsx")


if __name__ == "__main__":
    main()
