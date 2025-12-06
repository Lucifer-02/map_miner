import asyncio
import itertools
import logging
import random
import time
import traceback
from typing import Dict, Optional, Set
from urllib.parse import quote_plus

import polars as pl
from geopy.point import Point
from playwright.async_api import (
    BrowserContext,
    ChromiumBrowserContext,
    Page,
    async_playwright,
)
from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
)

from .extractor import extract_place_data
from .RecaptchaSolver import RecaptchaSolver

logger = logging.getLogger("root.scraper")

# --- Constants ---
DEFAULT_TIMEOUT = 30000  # 30 seconds for navigation and selectors
MAX_SCROLL_ATTEMPTS_WITHOUT_NEW_LINKS = (
    2  # Stop scrolling if no new links found after this many scrolls
)
# Launch args tuned to reduce headless fingerprints and cut noisy features
LAUNCH_ARGS = [
    # "--start-maximized",
    "--no-default-browser-check",
    "--disable-dev-shm-usage",
    "--disable-setuid-sandbox",
    "--no-sandbox",
    "--no-zygote",
    "--disable-gpu",
    # "--mute-audio",
    "--disable-extensions",
    "--disable-breakpad",
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
    "--disable-accelerated-2d-canvas",
    "--no-first-run",
    "--single-process",
    # "--headless=new",
]


def make_place_url(query: str, geo_coordinates: Point, zoom: float):
    # URL encode the query to handle spaces and special characters
    encoded_query = quote_plus(query)
    return f"https://www.google.com/maps/search/{encoded_query}/@{geo_coordinates.latitude},{geo_coordinates.longitude},{zoom}z?hl=en"


# --- Main Scraping Logic ---
async def scrape_google_maps(
    queries: Set[str],
    geo_coordinates: Point,
    zoom: float,
    proxy: Dict | None = None,
    max_places: int = 120,
    lang: str = "en",
    headless=False,
    n_semaphore: int = 8,
) -> pl.DataFrame:
    """
    Scrapes Google Maps for places based on a query.

    Args:
        query (str): The search query (e.g., "restaurants in New York").
        max_places (int, optional): Maximum number of places to scrape. Defaults to None (scrape all found).
        lang (str, optional): Language code for Google Maps (e.g., 'en', 'es'). Defaults to "en". headless (bool, optional): Whether to run the browser in headless mode. Defaults to True.

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
                proxy=proxy,
                args=LAUNCH_ARGS,
            )  # Added await
            context = await browser.new_context(  # Added await
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.7390.37 Safari/537.36",
                java_script_enabled=True,
                accept_downloads=False,
                # Consider setting viewport, locale, timezone if needed
                viewport={
                    "width": 1920 + random.randint(-50, 50),
                    "height": 1080 + random.randint(-50, 50),
                },
                timezone_id="Asia/Singapore",
                locale=lang,
            )

            # Create a list of tasks to be run concurrently
            tasks = [
                get_place_urls(
                    context=context,
                    max_places=max_places,
                    query=query.replace("_", " "),
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

            semaphore = asyncio.Semaphore(n_semaphore)

            # Create tasks
            tasks = [
                process_link(context, link, semaphore, i + 1, total)
                for i, link in enumerate(place_links)
            ]
            results = await asyncio.gather(*tasks)

            # Filter out None values
            results = [r for r in results if r is not None]
            logger.info(f"\n✅ Collected {len(results)} results")

            await browser.close()  # Added await

        except PlaywrightTimeoutError:
            logger.info("Timeout error during scraping process.")
        except Exception as e:
            logger.info(f"An error occurred during scraping: {e}")

            traceback.print_exc()
        finally:
            # Ensure browser is closed if an error occurred mid-process
            if (
                browser and browser.is_connected()
            ):  # Check if browser exists and is connected
                await browser.close()

    logger.info(f"\nScraping finished. Found details for {len(results)} places.")
    return pl.from_dicts(results)


# another version
async def process_link_1(
    context: ChromiumBrowserContext,
    link: str,
    semaphore: asyncio.Semaphore,
    count: int,
    total: int,
) -> Dict[str, str] | None:
    async with semaphore:
        current = time.time()
        page: Page | None = None
        try:
            logger.info(f"Processing link {count}/{total}: {link}")
            page = await context.new_page()

            # Set headers once
            await page.set_extra_http_headers(
                {
                    "Referer": "https://www.google.com/",
                    "Accept-Language": "en-US,en;q=0.9",
                }
            )

            # Navigate with optimized wait strategy
            try:
                await page.goto(link, wait_until="domcontentloaded", timeout=30000)
            except PlaywrightTimeoutError:
                logger.warning(f"  ❌ Timeout loading: {link}")
                return None
            except Exception as e:
                logger.error(f"  ❌ Error loading {link}: {e}")
                return None

            # Parallel humanization + content check
            scroll_task = page.mouse.wheel(0, random.randint(300, 700))
            url_check = page.url

            await scroll_task

            # Quick CAPTCHA check first (before expensive operations)
            if "sorry/index" in url_check:
                logger.warning("  🚨 CAPTCHA/Ban Detected (URL check)!")
                await page.screenshot(path=f"captcha_{int(current)}.png")
                return None

            # Reduced sleep time
            await asyncio.sleep(random.uniform(0.3, 0.8))

            # Move mouse (non-blocking)
            await page.mouse.move(random.randint(100, 500), random.randint(100, 500))

            # Use Promise.race pattern for faster completion
            try:
                await asyncio.wait_for(
                    page.wait_for_load_state("networkidle"),
                    timeout=3.0,  # Reduced from 5s
                )
            except asyncio.TimeoutError:
                pass

            # Combined content extraction and CAPTCHA check
            # Use evaluate to get both HTML and text in single call
            page_data = await page.evaluate("""() => {
                return {
                    html: document.documentElement.outerHTML,
                    bodyText: document.body.innerText,
                    hasH1: !!document.querySelector('h1')
                };
            }""")

            # Fast text-based CAPTCHA check
            if "Our systems have detected unusual traffic" in page_data["bodyText"]:
                logger.warning("  🚨 CAPTCHA Detected (Text check)!")
                await page.screenshot(path=f"captcha_{int(current)}.png")
                return None

            # Extract data from already-fetched HTML
            place_data = extract_place_data(page_data["html"])

            if place_data:
                place_data["link"] = link
                logger.info(f"  ✅ Extracted: {link} in {time.time() - current:.2f}s")
                return place_data
            else:
                logger.info(f"  ⚠️ Failed to extract (Structure changed?): {link}")
                # Only save files on failure (saves I/O)
                save_tasks = [
                    page.screenshot(path=f"failed_extract_{int(current)}.png"),
                    asyncio.to_thread(
                        lambda: open(
                            f"failed_{int(current)}.html", "w", encoding="utf-8"
                        ).write(page_data["html"])
                    ),
                ]
                await asyncio.gather(*save_tasks, return_exceptions=True)
                return None

        except Exception as e:
            logger.error(f"  ❌ Unexpected error: {e}")
            return None

        finally:
            if page:
                # Close without waiting (fire-and-forget)
                asyncio.create_task(page.close())


async def process_link(
    context: BrowserContext,
    link: str,
    semaphore: asyncio.Semaphore,
    count: int,
    total: int,
) -> Optional[Dict[str, str]]:
    async with semaphore:
        # Define resource types to block to save bandwidth/time
        BLOCKED_RESOURCE_TYPES = ["image", "font", "media", "stylesheet", "other"]
        current = time.time()
        page: Optional[Page] = None

        try:
            logger.info(f"Processing link {count}/{total}")
            page = await context.new_page()

            # 1. OPTIMIZATION: Block unnecessary resources
            # This is the biggest speed gain. Loading images/fonts is useless for scraping text.
            await page.route(
                "**/*",
                lambda route: route.abort()
                if route.request.resource_type in BLOCKED_RESOURCE_TYPES
                else route.continue_(),
            )

            # 2. Set Headers
            await page.set_extra_http_headers(
                {
                    "Referer": "https://www.google.com/",
                    "Accept-Language": "en-US,en;q=0.9",
                }
            )

            # 3. Navigation
            try:
                # 'domcontentloaded' is sufficient for 90% of sites if we wait for a specific selector later
                # Reduced timeout to fail fast
                await page.goto(link, wait_until="domcontentloaded", timeout=15000)
            except PlaywrightTimeoutError:
                logger.warning(f"  ❌ Timeout loading: {link}")
                return None

            # 4. Light Humanization (Concurrent)
            # Instead of serial sleeps, we perform checks while "simulating" reading
            # We skip the specific 'networkidle' wait because it is extremely slow/flaky

            # Fast scroll to trigger lazy loads (if JS needs it)
            # Executing this in JS is faster than Python calls
            await page.evaluate("""
                window.scrollTo(0, 300);
                setTimeout(() => window.scrollTo(0, 0), 200);
            """)

            # 5. Check for CAPTCHA / Bans (Optimized)
            # checking 'page.url' is instant.
            if "sorry/index" in page.url:
                logger.warning("  🚨 CAPTCHA detected (URL)!")
                return None

            # Optimization: Check specific title or limited text instead of entire body.innerText
            # or use a very specific selector for the "Unusual traffic" box.
            # Here we grab the first 1000 chars of text to avoid huge string serialization
            start_text = await page.evaluate(
                "document.body.innerText.substring(0, 1000)"
            )
            if "Our systems have detected unusual traffic" in start_text:
                logger.warning("  🚨 CAPTCHA detected (Text)!")
                return None

            # 6. Extract Data (Wait for critical element)
            # Instead of wait_for_selector inside a try/catch, we just wait.
            # If the Critical Element (e.g. h1) isn't there, the page is likely broken/garbage anyway.
            try:
                # Wait max 3 seconds for the main header to appear
                await page.wait_for_selector("h1", timeout=3000, state="attached")
            except PlaywrightTimeoutError:
                logger.warning(f"  ⚠️ Content not found (H1 missing): {link}")
                # Optional: Snapshot only on failure
                await page.screenshot(path=f"failed_{int(current)}.png")
                return None

            # 7. Heavy Optimization: Extraction Strategy
            # NOTE: Ideally, move the logic of 'extract_place_data' inside page.evaluate()
            # to return JSON directly. Passing full HTML to Python is slow.
            # Assuming you must keep Python extraction:
            html_content = await page.content()

            # Offload CPU-bound parsing to a thread if 'extract_place_data' is complex/slow
            # place_data = await asyncio.to_thread(extract_place_data, html_content)
            place_data = extract_place_data(html_content)

            if place_data:
                place_data["link"] = link
                logger.info(f"  ✅ Extracted in {time.time() - current:.2f}s")
                return place_data
            else:
                logger.info(f"  ⚠️ Failed to extract (Structure changed?): {link}")
                # Only save files on failure (saves I/O)
                save_tasks = [
                    page.screenshot(path=f"failed_extract_{int(current)}.png"),
                    asyncio.to_thread(
                        lambda: open(
                            f"failed_{int(current)}.html", "w", encoding="utf-8"
                        ).write(html_content)
                    ),
                ]
                await asyncio.gather(*save_tasks, return_exceptions=True)
                return None

        except Exception as e:
            logger.error(f"  ❌ Error: {e}")
            return None

        finally:
            if page:
                await page.close()


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
    search_url = make_place_url(query=query, geo_coordinates=geo_coordinates, zoom=zoom)

    logger.info(f"Navigating to search URL: {search_url}")
    await search_page.goto(search_url, wait_until="domcontentloaded")  # Added await
    await asyncio.sleep(random.uniform(1, 3))  # Changed to asyncio.sleep, added await

    if "consent" in search_page.url:
        logging.debug("CONSENT DETECTED!!!")
        await pass_consent(search_page=search_page)

    # if "sorry" in search_page.url:

    if (
        "sorry/index" in search_page.url
        or await search_page.locator(
            'text="Our systems have detected unusual traffic"'
        ).count()
        > 0
    ):
        logging.info("CAPTCHA DECTECTED!!!")
        await search_page.screenshot(path=f"captcha_{int(time.time())}.png")

        recaptchaSolver = RecaptchaSolver(search_page)
        await recaptchaSolver.solveCaptcha()

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
        SCROLL_PAUSE_TIME = 0.5
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
