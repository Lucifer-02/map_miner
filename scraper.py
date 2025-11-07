import asyncio  # Changed from time
import itertools
import logging
import random
import time
import traceback
from typing import List, Set
from urllib.parse import quote_plus

from geopy.point import Point
from playwright.async_api import ChromiumBrowserContext, Page, async_playwright
from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
)

import extractor

logger = logging.getLogger("root.scraper")

# --- Constants ---
DEFAULT_TIMEOUT = 30000  # 30 seconds for navigation and selectors
MAX_SCROLL_ATTEMPTS_WITHOUT_NEW_LINKS = (
    2  # Stop scrolling if no new links found after this many scrolls
)


def make_place_url(query: str, geo_coordinates: Point, zoom: float):
    # URL encode the query to handle spaces and special characters
    encoded_query = quote_plus(query)
    return f"https://www.google.com/maps/search/{encoded_query}/@{geo_coordinates.latitude},{geo_coordinates.longitude},{zoom}z?hl=en"


# --- Main Scraping Logic ---
async def scrape_google_maps(
    queries: List[str],
    geo_coordinates: Point,
    zoom: float,
    max_places: int,
    lang: str = "en",
    headless=False,
):
    """
    Scrapes Google Maps for places based on a query.

    Args:
        query (str): The search query (e.g., "restaurants in New York").
        max_places (int, optional): Maximum number of places to scrape. Defaults to None (scrape all found).
        lang (str, optional): Language code for Google Maps (e.g., 'en', 'es'). Defaults to "en".
        headless (bool, optional): Whether to run the browser in headless mode. Defaults to True.

    Returns:
        list: A list of dictionaries, each containing details for a scraped place.
              Returns an empty list if no places are found or an error occurs.
    """
    results = []
    browser = None

    async with async_playwright() as p:  # Changed to async
        try:
            browser = await p.chromium.launch(
                headless=headless,
                proxy={"server": "socks5://127.0.0.1:9050"},
                args=[
                    "--disable-dev-shm-usage",  # Use /tmp instead of /dev/shm for shared memory
                    "--no-sandbox",  # Required for running in Docker
                    "--disable-setuid-sandbox",
                ],
            )  # Added await
            context = await browser.new_context(  # Added await
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
                java_script_enabled=True,
                accept_downloads=False,
                # Consider setting viewport, locale, timezone if needed
                locale=lang,
            )

            # Create a list of tasks to be run concurrently
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
            # Run the tasks in parallel
            list_of_sets_of_links = await asyncio.gather(*tasks)
            # Flatten the list of sets into a single list of unique links
            place_links = list(
                set(itertools.chain.from_iterable(list_of_sets_of_links))
            )
            logger.debug(f"List of place links: {place_links}.")

            # --- Scraping Individual Places ---
            logger.info(f"\nScraping details for {len(place_links)} places...")
            total = len(place_links)
            semaphore = asyncio.Semaphore(16)

            # Create tasks
            tasks = [
                process_link(context, link, semaphore, i + 1, total)
                for i, link in enumerate(place_links)
            ]
            # Run all in parallel
            results = await asyncio.gather(*tasks)
            # Filter out None values
            results = [r for r in results if r]
            logger.info(f"\n✅ Collected {len(results)} results")

            await browser.close()  # Added await

        except PlaywrightTimeoutError:
            logger.info("Timeout error during scraping process.")
        except Exception as e:
            logger.info(f"An error occurred during scraping: {e}")

            traceback.print_exc()  # Print detailed traceback for debugging
        finally:
            # Ensure browser is closed if an error occurred mid-process
            if (
                browser and browser.is_connected()
            ):  # Check if browser exists and is connected
                await browser.close()  # Added await

    logger.info(f"\nScraping finished. Found details for {len(results)} places.")
    return results


async def process_link(
    context: ChromiumBrowserContext,
    link: str,
    semaphore: asyncio.Semaphore,
    count: int,
    total: int,
):
    async with semaphore:  # Limit concurrency
        logger.info(f"Processing link {count}/{total}: {link}")
        page = await context.new_page()
        try:
            await page.goto(link, wait_until="domcontentloaded", timeout=15000)
            html_content = await page.content()

            if (
                page.locator('iframe[name="a-280r0snlgxq2"]')
                .content_frame.get_by_text("I'm not a robot")
                .is_visible()
            ) and "sorry" in page.url:
                logging.debug("CAPTCHA DECTECTED!!!")
                await page.screenshot(path=f"image_{int(time.time())}.png")

            place_data = extractor.extract_place_data(html_content)

            if place_data:
                place_data["link"] = link
                logger.info(f"  ✅ Extracted: {link}")
                return place_data
            else:
                logger.info(f"  ⚠️ Failed to extract: {link}")
        except PlaywrightTimeoutError:
            logger.error(f"  ⏰ Timeout for: {link}")
        except Exception as e:
            logger.error(f"  ❌ Error for {link}: {e}")
        finally:
            await page.close()
        return None


async def pass_consent(search_page: Page):
    logging.debug("Passing consent...")
    accept_button = search_page.get_by_role("button", name="Reject all")
    await accept_button.click()


async def get_place_urls(
    context: ChromiumBrowserContext,
    max_places: int,
    query: str,
    geo_coordinates: Point,
    zoom: float,
) -> Set[str]:
    search_page = await context.new_page()  # Added await
    if not search_page:
        raise Exception(
            "Failed to create a new browser page (context.new_page() returned None)."
        )
    # Removed problematic: await page.set_default_timeout(DEFAULT_TIMEOUT)
    # Removed associated debug logger.infos
    search_url = make_place_url(query=query, geo_coordinates=geo_coordinates, zoom=zoom)

    logger.info(f"Navigating to search URL: {search_url}")
    await search_page.goto(search_url, wait_until="domcontentloaded")  # Added await
    await asyncio.sleep(random.uniform(1, 3))  # Changed to asyncio.sleep, added await

    if "consent" in search_page.url:
        logging.debug("CONSENT DETECTED!!!")
        await pass_consent(search_page=search_page)

    # recaptchaSolver = RecaptchaSolver(driver=search_page)
    # recaptchaSolver.solveCaptcha()

    place_links = set()

    # --- Scrolling and Link Extraction ---
    logger.debug("Scrolling to load places...")
    feed_selector = '[role="feed"]'
    try:
        await search_page.wait_for_selector(
            feed_selector, state="visible", timeout=25000
        )  # Added await
    except PlaywrightTimeoutError:
        # Check if it's a single result page (maps/place/)
        if "/maps/place/" in search_page.url:
            logger.debug("Detected single place page.")
            place_links.add(search_page.url)
        else:
            logger.error(
                f"Error: Feed element '{feed_selector}' not found. Maybe no results or page structure changed."
            )
            # await browser.close()  # Added await
            return set()  # No results or page structure changed

    if await search_page.locator(feed_selector).count() > 0:  # Added await
        last_height = await search_page.evaluate(
            f"document.querySelector('{feed_selector}').scrollHeight"
        )  # Added await

        scroll_attempts_no_new = 0
        SCROLL_PAUSE_TIME = 0.5  # Pause between scrolls
        while True:
            # Scroll down
            await search_page.evaluate(
                f"document.querySelector('{feed_selector}').scrollTop = document.querySelector('{feed_selector}').scrollHeight"
            )  # Added await
            await asyncio.sleep(
                SCROLL_PAUSE_TIME
            )  # Changed to asyncio.sleep, added await

            # Extract links after scroll
            current_links_list = await search_page.locator(
                f'{feed_selector} a[href*="/maps/place/"]'
            ).evaluate_all("elements => elements.map(a => a.href)")  # Added await
            current_links = set(current_links_list)
            new_links_found = len(current_links - place_links) > 0
            place_links.update(current_links)
            logger.debug(f"Found {len(place_links)} unique place links so far...")

            if max_places is not None and len(place_links) >= max_places:
                logger.debug(f"Reached max_places limit ({max_places}).")
                place_links = set(itertools.islice(place_links, max_places))
                break

            # Check if scroll height has changed
            new_height = await search_page.evaluate(
                f"document.querySelector('{feed_selector}').scrollHeight"
            )  # Added await
            if new_height == last_height:
                # Check for the "end of results" marker
                end_marker_xpath = (
                    '//span[contains(text(), "You\'ve reached the end of the list.")]'
                )
                if (
                    await search_page.locator(end_marker_xpath).count() > 0
                ):  # Added await
                    logger.debug("Reached the end of the results list.")
                    break
                else:
                    # If height didn't change but end marker isn't there, maybe loading issue?
                    # Increment no-new-links counter
                    if not new_links_found:
                        scroll_attempts_no_new += 1
                        logger.debug(
                            f"Scroll height unchanged and no new links. Attempt {scroll_attempts_no_new}/{MAX_SCROLL_ATTEMPTS_WITHOUT_NEW_LINKS}"
                        )
                        if (
                            scroll_attempts_no_new
                            >= MAX_SCROLL_ATTEMPTS_WITHOUT_NEW_LINKS
                        ):
                            logger.debug("Stopping scroll due to lack of new links.")
                            break
                    else:
                        scroll_attempts_no_new = (
                            0  # Reset if new links were found this cycle
                        )
            else:
                last_height = new_height
                scroll_attempts_no_new = 0  # Reset if scroll height changed

            # Optional: Add a hard limit on scrolls to prevent infinite loops
            # if scroll_count > MAX_SCROLLS: break
    await search_page.close()
    return place_links
