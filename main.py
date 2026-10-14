import asyncio
import logging

from geopy.point import Point

from map_miner import scrape_google_maps


def main():
    pois = asyncio.run(
        scrape_google_maps(
            queries={"cafe", "atm", "hospital", "gym"},
            max_places=50,
            lang="en",
            geo_coordinates=Point(21.018785, 105.830415),
            zoom=18,
            n_semaphore=8,
        )
    )
    print(pois)
    print(pois.columns)
    pois.write_excel("out.xlsx")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    )
    main()
