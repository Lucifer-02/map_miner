import asyncio

from geopy.point import Point

from scraper import scrape_google_maps


def main():

    pois = asyncio.run(
        scrape_google_maps(
            queries={"cafe"},
            max_places=20,
            lang="en",
            headless=True,
            geo_coordinates=Point(20.985322, 105.781289),
            zoom=18,
            fields=None,  # Or select specific fields, e.g. ["name", "address", "phone", "rating", "link"]
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
            # proxy={"server": "socks5://127.0.0.1:9050"},
            proxy=None,
            n_semaphore=8,
        )
    )
    print(pois)
    print(pois.columns)
    pois.write_excel("out.xlsx")


if __name__ == "__main__":
    main()
