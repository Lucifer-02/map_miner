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
                "hospital",
                "gym",
                "store",
                "restaurant",
                "bank",
                "gas",
            },
            max_places=40,
            lang="en",
            headless=True,
            geo_coordinates=Point(21.018785, 105.830415),
            zoom=18,
            fields=None,
            # Output format:
            # - flatten=False (default): Standardizes 11 common columns at top-level
            #   (name, place_id, latitude, longitude, address, link, categories, rating, reviews_count, plus_code, city)
            #   and bundles other fields into 'details' JSON
            flatten=False,
            preview_timeout=30000,
            # Proxy configuration:
            # - Residential proxy (Decodo): Best for avoiding CAPTCHAs
            # - Direct (None): Fast and reliable with modern stealth fingerprints
            # - Tor (socks5://127.0.0.1:9050): Keep n_semaphore low (2-3)
            # proxy={
            #     # decodo residential service (Recommended to evade CAPTCHAs)
            #     # "server": "http://gate.decodo.com:10000",
            #     # "username": "spp86iv7zu",
            #     # "password": "6yoqpXiuaF5bT_83sV",
            #     # "bypass": DEFAULT_PROXY_BYPASS,
            #     # tor proxy alternative:
            #     "server": "socks5://127.0.0.1:9050",
            #     "bypass": DEFAULT_PROXY_BYPASS,
            # },
            n_semaphore=12,
        )
    )

    print(pois)
    print(pois.columns)
    pois.write_excel("out.xlsx")


if __name__ == "__main__":
    main()
