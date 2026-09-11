import asyncio
import logging

from geopy.point import Point

from map_miner import scrape_google_maps

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)


def main():
    pois = asyncio.run(
        scrape_google_maps(
            queries={"cafe"},
            max_places=10,
            lang="en",
            headless=False,
            geo_coordinates=Point(20.985322, 105.781289),
            zoom=18,
            fields=None,
            # proxy={
            #     "server": "http://gate.decodo.com:10000",
            #     "username": "spp86iv7zu",
            #     "password": "6yoqpXiuaF5bT_83sV",
            # },
            n_semaphore=8,
        )
    )

    print(pois)
    print(pois.columns)
    pois.write_excel("out.xlsx")


if __name__ == "__main__":
    main()
