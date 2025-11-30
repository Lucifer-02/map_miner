import asyncio
import itertools
import logging
import random
import time
import traceback
from typing import Iterable, List, Optional, Set
from urllib.parse import quote_plus

from geopy.point import Point
from playwright.async_api import ChromiumBrowserContext, Page, async_playwright
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

import extractor

logger = logging.getLogger("root.scraper")

# --- Constants ---
DEFAULT_TIMEOUT = 30000  # 30 seconds for navigation and selectors
MAX_SCROLL_ATTEMPTS_WITHOUT_NEW_LINKS = (
    2  # Stop scrolling if no new links found after this many scrolls
)

# Launch args tuned to reduce headless fingerprints and cut noisy features
LAUNCH_ARGS = [
    "--start-maximized",
    "--no-default-browser-check",
    "--disable-dev-shm-usage",
    "--disable-setuid-sandbox",
    "--no-sandbox",
    "--no-zygote",
    "--disable-gpu",
    "--mute-audio",
    "--disable-extensions",
    "--disable-breakpad",
    "--disable-features=TranslateUI,BlinkGenPropertyTrees",
    "--disable-ipc-flooding-protection",
    "--enable-features=NetworkService,NetworkServiceInProcess",
    "--disable-default-apps",
    "--disable-notifications",
    "--disable-webgl",
    "--disable-blink-features=AutomationControlled",
    "--ignore-certificate-errors",
    "--ignore-certificate-errors-spki-list",
    "--disable-web-security",
    "--blink-settings=imagesEnabled=false",
]


def make_place_url(query: str, geo_coordinates: Point, zoom: float) -> str:
    encoded_query = quote_plus(query)
    return f"https://www.google.com/maps/search/{encoded_query}/@{geo_coordinates.latitude},{geo_coordinates.longitude},{zoom}z?hl=en"


def pick_proxy(proxies: Optional[Iterable[str]]) -> Optional[dict]:
    prox_list = list([{"server": "socks5://127.0.0.1:9999"}])
    if not prox_list:
        return None
    return {"server": random.choice(prox_list)}


async def scrape_google_maps(
    queries: List[str],
    geo_coordinates: Point,
    zoom: float,
    max_places: int,
    lang: str = "en",
    headless: bool = False,
    proxies: Optional[Iterable[str]] = None,
    timezone_id: str = "UTC",
) -> List[dict]:
    results: List[dict] = []
    browser = None

    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(
                headless=headless,
                # proxy={"server": "socks5://127.0.0.1:9050"},
                # proxy={
                #     "server": "http://154.202.3.40:49230",
                #     "username": "user49230",
                #     "password": "GQJ62IBqX2",
                # },
                args=LAUNCH_ARGS,
            )
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
                java_script_enabled=True,
                accept_downloads=False,
                storage_state="./state.json",
                viewport={"width": 1920, "height": 1080},
                locale=lang,
                timezone_id=timezone_id,
            )
            await context.set_extra_http_headers(
                {
                    "sec-ch-ua": '"Google Chrome";v="120", "Chromium";v="120", "Not?A_Brand";v="24"',
                    "sec-ch-ua-mobile": "?0",
                    "sec-ch-ua-platform": '"Windows"',
                    "Upgrade-Insecure-Requests": "1",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Cache-Control": "max-age=0",
                }
            )

            tasks = [
                get_place_urls(
                    context=context,
                    max_places=max_places,
                    query=query,
                    geo_coordinates=geo_coordinates,
                    zoom=zoom,
                )
                for query in queries
            ]

            list_of_sets_of_links = await asyncio.gather(*tasks)
            place_links = list(
                set(itertools.chain.from_iterable(list_of_sets_of_links))
            )
            logger.debug("List of place links: %s.", place_links)

            logger.info("Scraping details for %d places...", len(place_links))
            semaphore = asyncio.Semaphore(8)
            tasks = [
                process_link(context, link, semaphore, i + 1, len(place_links))
                for i, link in enumerate(place_links)
            ]
            results = await asyncio.gather(*tasks)
            results = [r for r in results if r]
            logger.info("Collected %d results", len(results))

            await browser.close()

        except PlaywrightTimeoutError:
            logger.info("Timeout error during scraping process.")
        except Exception as exc:  # pylint: disable=broad-except
            logger.info("An error occurred during scraping: %s", exc)
            traceback.print_exc()
        finally:
            if browser and browser.is_connected():
                await browser.close()

    logger.info("Scraping finished. Found details for %d places.", len(results))
    return results


async def process_link(
    context: ChromiumBrowserContext,
    link: str,
    semaphore: asyncio.Semaphore,
    count: int,
    total: int,
):
    async with semaphore:
        logger.info("Processing link %d/%d: %s", count, total, link)
        page = await context.new_page()
        try:
            await page.goto(link, wait_until="domcontentloaded", timeout=15000)
            await page.mouse.move(random.randint(100, 1000), random.randint(100, 800))
            await asyncio.sleep(random.uniform(0.3, 1.2))

            html_content = await page.content()

            if (
                page.locator('iframe[name="a-280r0snlgxq2"]')
                .content_frame.get_by_text("I'm not a robot")
                .is_visible()
            ) and "sorry" in page.url:
                logger.debug("CAPTCHA detected")
                await page.screenshot(path=f"image_{int(time.time())}.png")

            place_data = extractor.extract_place_data(html_content)
            if place_data:
                place_data["link"] = link
                logger.info("  Extracted: %s", link)
                return place_data

            logger.info("  Failed to extract: %s", link)
        except PlaywrightTimeoutError:
            logger.error("  Timeout for: %s", link)
        except Exception as exc:  # pylint: disable=broad-except
            logger.error("  Error for %s: %s", link, exc)
        finally:
            await page.close()
        return None


async def pass_consent(search_page: Page):
    logger.debug("Passing consent...")
    accept_button = search_page.get_by_role("button", name="Reject all")
    await accept_button.click()


async def get_place_urls(
    context: ChromiumBrowserContext,
    max_places: int,
    query: str,
    geo_coordinates: Point,
    zoom: float,
) -> Set[str]:
    search_page = await context.new_page()
    if not search_page:
        raise RuntimeError("Failed to create a new browser page.")

    search_url = make_place_url(query=query, geo_coordinates=geo_coordinates, zoom=zoom)
    logger.info("Navigating to search URL: %s", search_url)
    await search_page.goto(search_url, wait_until="domcontentloaded")
    await asyncio.sleep(random.uniform(1, 3))

    if "consent" in search_page.url:
        logger.debug("Consent detected")
        await pass_consent(search_page=search_page)

    if "sorry" in search_page.url:
        logger.info("Captcha detected; screenshotting")
        await search_page.screenshot(path=f"image_{int(time.time())}.png")

    place_links: Set[str] = set()
    feed_selector = '[role="feed"]'

    try:
        await search_page.wait_for_selector(
            feed_selector, state="visible", timeout=25000
        )
    except PlaywrightTimeoutError:
        if "/maps/place/" in search_page.url:
            logger.debug("Detected single place page.")
            place_links.add(search_page.url)
        else:
            logger.error("Feed element '%s' not found.", feed_selector)
            await search_page.close()
            return set()

    if await search_page.locator(feed_selector).count() > 0:
        last_height = await search_page.evaluate(
            f"document.querySelector('{feed_selector}').scrollHeight"
        )
        scroll_attempts_no_new = 0
        scroll_pause = 0.5

        while True:
            await search_page.evaluate(
                f"document.querySelector('{feed_selector}').scrollTop = document.querySelector('{feed_selector}').scrollHeight"
            )
            await asyncio.sleep(scroll_pause)

            current_links_list = await search_page.locator(
                f'{feed_selector} a[href*="/maps/place/"]'
            ).evaluate_all("elements => elements.map(a => a.href)")
            current_links = set(current_links_list)
            new_links_found = len(current_links - place_links) > 0
            place_links.update(current_links)
            logger.debug("Found %d unique place links so far...", len(place_links))

            if max_places is not None and len(place_links) >= max_places:
                place_links = set(itertools.islice(place_links, max_places))
                break

            new_height = await search_page.evaluate(
                f"document.querySelector('{feed_selector}').scrollHeight"
            )
            if new_height == last_height:
                end_marker_xpath = (
                    '//span[contains(text(), "You\'ve reached the end of the list.")]'
                )
                if await search_page.locator(end_marker_xpath).count() > 0:
                    logger.debug("Reached the end of the results list.")
                    break

                if not new_links_found:
                    scroll_attempts_no_new += 1
                    logger.debug(
                        "Scroll height unchanged and no new links. Attempt %d/%d",
                        scroll_attempts_no_new,
                        MAX_SCROLL_ATTEMPTS_WITHOUT_NEW_LINKS,
                    )
                    if scroll_attempts_no_new >= MAX_SCROLL_ATTEMPTS_WITHOUT_NEW_LINKS:
                        logger.debug("Stopping scroll due to lack of new links.")
                        break
                else:
                    scroll_attempts_no_new = 0
            else:
                last_height = new_height
                scroll_attempts_no_new = 0

    await search_page.close()
    return place_links
