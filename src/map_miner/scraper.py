import asyncio
import itertools
import logging
import os
import random
import re
import time
import traceback
from typing import Any
from urllib.parse import quote_plus

import polars as pl
from geopy.point import Point
from playwright.async_api import (
    ChromiumBrowserContext,
    Page,
    ProxySettings,
    async_playwright,
)
from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
)

from .extractor import extract_place_data
from .recaptcha_solver import RecaptchaSolver

logger = logging.getLogger("root.scraper")

# --- Constants ---
DEFAULT_TIMEOUT = 30000  # 30 seconds for navigation and selectors
MAX_SCROLL_ATTEMPTS_WITHOUT_NEW_LINKS = (
    5  # Allow enough attempts for slow network / lazy load
)

# Stable launch args: headless/stealth-safe, avoids crashes on multi-page Chromium
LAUNCH_ARGS = [
    "--no-default-browser-check",
    "--disable-dev-shm-usage",
    "--disable-setuid-sandbox",
    "--no-sandbox",
    "--no-zygote",
    "--disable-gpu",
    "--disable-extensions",
    "--disable-breakpad",
    "--disable-ipc-flooding-protection",
    "--disable-default-apps",
    "--disable-notifications",
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    # Bandwidth & Resource Optimization Flags
    "--blink-settings=imagesEnabled=false",
    "--disable-remote-fonts",
    "--mute-audio",
    "--disable-background-networking",
]

# Resource types to abort across all pages (Search and Detail)
BLOCKED_RESOURCE_TYPES = {"image", "media", "font", "stylesheet"}

# URL substrings to abort (Map vector/satellite tiles, tracking, telemetry, photo CDN)
BLOCKED_URL_PATTERNS = [
    "/maps/vt",
    "/vt/pb=",
    "/vt/data=",
    "khms",
    "/kh/v=",
    "google.com/vt",
    "google-analytics.com",
    "play.google.com/log",
    "stats.g.doubleclick.net",
    "/gen_204",
    "googleusercontent.com",
    "ggpht.com",
    "streetviewpixels",
]


async def global_route_handler(route):
    """
    Context-wide route handler to block heavy resources and tracking,
    saving significant network bandwidth while preserving reCAPTCHA and core APIs.
    """
    try:
        req = route.request
        url = req.url

        # Always permit reCAPTCHA verification requests
        if "recaptcha" in url:
            await route.continue_()
            return

        # Block by resource type (images, media, fonts, stylesheets)
        if req.resource_type in BLOCKED_RESOURCE_TYPES:
            await route.abort()
            return

        # Block by URL pattern (Map vector tiles, tracking, telemetry, photo CDN)
        if any(pattern in url for pattern in BLOCKED_URL_PATTERNS):
            await route.abort()
            return

        await route.continue_()
    except Exception:
        try:
            await route.continue_()
        except Exception:
            pass


def make_place_url(
    query: str, geo_coordinates: Point, zoom: float, lang: str = "en"
) -> str:
    """Builds a localized Google Maps search URL."""
    encoded_query = quote_plus(query)
    return f"https://www.google.com/maps/search/{encoded_query}/@{geo_coordinates.latitude},{geo_coordinates.longitude},{zoom}z?hl={lang}"


async def pass_consent(page: Page) -> bool:
    """
    Attempts to bypass or dismiss Google's cookie/privacy consent banner
    across multiple languages safely without crashing.
    """
    logger.debug("Checking for consent dialog...")
    consent_patterns = [
        r"(?i)reject all",
        r"(?i)từ chối tất cả",
        r"(?i)alle ablehnen",
        r"(?i)tout refuser",
        r"(?i)rechazar todo",
        r"(?i)rifiuta tutto",
        r"(?i)accept all",
        r"(?i)chấp nhận tất cả",
        r"(?i)alle akzeptieren",
        r"(?i)tout accepter",
        r"(?i)i agree",
        r"(?i)tôi đồng ý",
    ]
    for pattern in consent_patterns:
        try:
            button = page.get_by_role("button", name=re.compile(pattern))
            if await button.count() > 0 and await button.first.is_visible():
                await button.first.click()
                await asyncio.sleep(1.0)
                logger.debug(f"Consent dismissed with button matching: {pattern}")
                return True
        except Exception:
            continue

    # Fallback to standard Google consent forms
    try:
        forms = page.locator('form[action*="consent"]')
        if await forms.count() > 0:
            btn = forms.locator("button")
            if await btn.count() > 0 and await btn.first.is_visible():
                await btn.first.click()
                await asyncio.sleep(1.0)
                logger.debug("Consent dismissed via form button.")
                return True
    except Exception:
        pass

    return False


async def get_place_urls(
    context: ChromiumBrowserContext,
    max_places: int,
    query: str,
    geo_coordinates: Point,
    zoom: float,
    lang: str = "en",
) -> set[str]:
    """
    Navigates the search feed and scrolls to collect place links.
    Ensures safe resource cleanup and resilient selector handling.
    """
    search_page = await context.new_page()
    if not search_page:
        raise RuntimeError("Failed to create search browser page.")

    place_links: set[str] = set()

    try:
        search_url = make_place_url(
            query=query, geo_coordinates=geo_coordinates, zoom=zoom, lang=lang
        )
        logger.info(f"Navigating to search URL: {search_url}")

        await search_page.goto(
            search_url, wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT
        )
        await asyncio.sleep(random.uniform(1.0, 2.5))

        if "consent" in search_page.url:
            logger.debug("Consent page detected, attempting bypass...")
            await pass_consent(search_page)

        # CAPTCHA check
        if (
            "sorry/index" in search_page.url
            or await search_page.locator(
                'text="Our systems have detected unusual traffic"'
            ).count()
            > 0
        ):
            logger.warning("🚨 CAPTCHA detected on search page!")
            os.makedirs("debug", exist_ok=True)
            await search_page.screenshot(
                path=f"debug/captcha_search_{int(time.time())}.png"
            )
            recaptcha_solver = RecaptchaSolver(search_page)
            await recaptcha_solver.solveCaptcha()

        # Check if single result redirect happened
        if "/maps/place/" in search_page.url:
            logger.debug("Detected single place redirect.")
            place_links.add(search_page.url)
            return place_links

        feed_selector = '[role="feed"]'
        active_feed_selector = None
        try:
            await search_page.wait_for_selector(
                feed_selector, state="visible", timeout=15000
            )
            active_feed_selector = feed_selector
        except PlaywrightTimeoutError:
            for fallback in [
                'div[aria-label*="Results for"]',
                'div[aria-label*="Kết quả cho"]',
                'div[role="main"] div[tabindex="-1"]',
            ]:
                if await search_page.locator(fallback).count() > 0:
                    active_feed_selector = fallback
                    break

        if not active_feed_selector:
            # Check once more for single place page
            if "/maps/place/" in search_page.url:
                place_links.add(search_page.url)
                return place_links
            logger.error("Could not find results feed selector on search page.")
            return place_links

        last_height = await search_page.evaluate(
            f"document.querySelector('{active_feed_selector}').scrollHeight"
        )
        scroll_attempts_no_new = 0

        while True:
            # Scroll feed down
            await search_page.evaluate(
                f"document.querySelector('{active_feed_selector}').scrollTop = document.querySelector('{active_feed_selector}').scrollHeight"
            )
            await asyncio.sleep(random.uniform(0.7, 1.3))

            # Extract place links from feed
            current_links_list = await search_page.locator(
                f'{active_feed_selector} a[href*="/maps/place/"]'
            ).evaluate_all("elements => elements.map(a => a.href)")
            current_links = set(current_links_list)
            new_links = current_links - place_links
            place_links.update(current_links)
            logger.debug(f"Found {len(place_links)} unique place links so far...")

            if max_places is not None and len(place_links) >= max_places:
                logger.debug(f"Reached max_places limit ({max_places}).")
                place_links = set(itertools.islice(place_links, max_places))
                break

            new_height = await search_page.evaluate(
                f"document.querySelector('{active_feed_selector}').scrollHeight"
            )

            # Check for end of list markers (multi-lingual)
            end_markers = [
                '//span[contains(text(), "reached the end of the list")]',
                '//span[contains(text(), "hết danh sách")]',
                '//div[contains(text(), "reached the end")]',
            ]
            is_at_end = False
            for marker in end_markers:
                if await search_page.locator(marker).count() > 0:
                    is_at_end = True
                    break

            if is_at_end:
                logger.debug("Reached end of results list marker.")
                break

            if new_height == last_height and not new_links:
                scroll_attempts_no_new += 1
                logger.debug(
                    f"Scroll height unchanged and no new links. Attempt {scroll_attempts_no_new}/{MAX_SCROLL_ATTEMPTS_WITHOUT_NEW_LINKS}"
                )
                # Adaptive pause when no new items appear
                await asyncio.sleep(1.5)
                if scroll_attempts_no_new >= MAX_SCROLL_ATTEMPTS_WITHOUT_NEW_LINKS:
                    logger.debug("Stopping scroll due to lack of new links.")
                    break
            else:
                last_height = new_height
                scroll_attempts_no_new = 0

    except Exception as e:
        logger.error(f"Error during get_place_urls: {e}")
    finally:
        if search_page and not search_page.is_closed():
            try:
                await search_page.close()
            except Exception:
                pass

    return place_links


async def process_link(
    context: ChromiumBrowserContext,
    link: str,
    semaphore: asyncio.Semaphore,
    count: int,
    total: int,
    fields: list[str] | set[str] | None = None,
    max_retries: int = 2,
) -> dict[str, Any] | None:
    """
    Processes a single place link to extract data:
    - Pure I/O: intercepts rich network payload or collects HTML content
    - Pure extraction: delegates parsing to extractor.py
    - Includes automatic retry and resilient page lifecycle cleanup
    """
    async with semaphore:
        for attempt in range(1, max_retries + 1):
            start_time = time.time()
            page: Page | None = None
            try:
                logger.info(
                    f"Processing link [{count}/{total}] (attempt {attempt}/{max_retries}): {link}"
                )
                page = await context.new_page()

                # Intercept Google Maps rich place preview API response
                preview_json: str | None = None
                preview_event = asyncio.Event()

                async def handle_response(response):
                    nonlocal preview_json
                    if "maps/preview/place" in response.url:
                        try:
                            text = await response.text()
                            # Rich place detail payload is large and begins with Google's XSSI prefix
                            if text and len(text) > 1500 and ")]}'" in text:
                                preview_json = text
                                preview_event.set()
                        except Exception:
                            pass

                page.on("response", handle_response)

                await page.set_extra_http_headers(
                    {
                        "Referer": "https://www.google.com/",
                        "Accept-Language": "en-US,en;q=0.9,vi;q=0.8",
                    }
                )

                try:
                    await page.goto(
                        link, wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT
                    )
                except PlaywrightTimeoutError:
                    logger.warning(f"  ❌ Timeout navigating to: {link}")
                    if attempt < max_retries:
                        await asyncio.sleep(random.uniform(0.8, 1.5))
                        continue
                    return None
                except Exception as e:
                    logger.error(f"  ❌ Navigation error for {link}: {e}")
                    if attempt < max_retries:
                        await asyncio.sleep(random.uniform(0.8, 1.5))
                        continue
                    return None

                # Adaptive wait for preview API response (usually arrives within 0.3s - 1.5s, max wait 4.5s)
                try:
                    await asyncio.wait_for(preview_event.wait(), timeout=4.5)
                except TimeoutError:
                    # Fallback wait for main title element if preview API is delayed
                    try:
                        await page.wait_for_selector("h1, [role='main']", timeout=2000)
                    except Exception:
                        pass

                # Early exit: if rich preview JSON was intercepted, extract immediately without waiting for DOM
                if preview_json:
                    place_data = extract_place_data(
                        html_content=None,
                        preview_json=preview_json,
                        fields=fields,
                    )
                    if place_data is not None and (
                        "name" in place_data or len(place_data) >= 3
                    ):
                        if fields is None or "link" in fields:
                            place_data["link"] = link
                        elapsed = time.time() - start_time
                        logger.info(
                            f"  ✅ Extracted (early preview): {link} in {elapsed:.2f}s"
                        )
                        return place_data

                # Anti-bot human jitter: slight random scroll & mouse move (fallback path)
                try:
                    await page.mouse.move(
                        random.randint(100, 400), random.randint(100, 400)
                    )
                    await page.mouse.wheel(0, random.randint(150, 400))
                except Exception:
                    pass

                # CAPTCHA verification
                current_url = page.url
                if "sorry/index" in current_url:
                    logger.warning("  🚨 CAPTCHA detected (URL)!")
                    os.makedirs("debug", exist_ok=True)
                    await page.screenshot(path=f"debug/captcha_{int(start_time)}.png")
                    if attempt < max_retries:
                        await asyncio.sleep(random.uniform(1.0, 2.0))
                        continue
                    return None

                try:
                    body_text = await page.inner_text("body", timeout=1500)
                    if "Our systems have detected unusual traffic" in body_text:
                        logger.warning("  🚨 CAPTCHA detected (Text check)!")
                        os.makedirs("debug", exist_ok=True)
                        await page.screenshot(
                            path=f"debug/captcha_{int(start_time)}.png"
                        )
                        if attempt < max_retries:
                            await asyncio.sleep(random.uniform(1.0, 2.0))
                            continue
                        return None
                except Exception:
                    pass

                # Retrieve raw HTML for DOM fallback parsing
                html_content = await page.content()

                # Delegate pure extraction to extractor.py (no side-effects)
                place_data = extract_place_data(
                    html_content=html_content,
                    preview_json=preview_json,
                    fields=fields,
                )

                if place_data is not None:
                    if fields is None or "link" in fields:
                        place_data["link"] = link
                    elapsed = time.time() - start_time
                    logger.info(f"  ✅ Extracted: {link} in {elapsed:.2f}s")
                    return place_data
                else:
                    logger.warning(f"  ⚠️ Extraction returned None: {link}")
                    if attempt < max_retries:
                        await asyncio.sleep(random.uniform(0.8, 1.5))
                        continue
                    else:
                        os.makedirs("debug", exist_ok=True)
                        await page.screenshot(
                            path=f"debug/failed_{int(start_time)}.png"
                        )
                        with open(
                            f"debug/failed_{int(start_time)}.html",
                            "w",
                            encoding="utf-8",
                        ) as f:
                            f.write(html_content)
                        return None

            except Exception as e:
                logger.error(f"  ❌ Error processing {link} (attempt {attempt}): {e}")
                if attempt < max_retries:
                    await asyncio.sleep(random.uniform(0.8, 1.5))
                else:
                    return None
            finally:
                if page and not page.is_closed():
                    try:
                        await page.close()
                    except Exception:
                        pass

        return None


async def scrape_google_maps(
    queries: set[str],
    geo_coordinates: Point,
    zoom: float,
    proxy: ProxySettings | None = None,
    max_places: int = 120,
    lang: str = "en",
    headless: bool = False,
    n_semaphore: int = 8,
    fields: list[str] | set[str] | None = None,
) -> pl.DataFrame:
    """
    Scrapes Google Maps for places based on queries.

    Args:
        queries (set[str]): Search queries (e.g. {"cafe", "restaurant"}).
        geo_coordinates (Point): Center coordinates for search.
        zoom (float): Map zoom level.
        proxy (dict, optional): Proxy configuration for Playwright.
        max_places (int, optional): Maximum places to collect per query. Defaults to 120.
        lang (str, optional): Language code for Google Maps. Defaults to "en".
        headless (bool, optional): Whether to run headless browser. Defaults to False.
        n_semaphore (int, optional): Concurrency level for scraping links. Defaults to 8.
        fields (list[str] | set[str], optional): Selected fields to extract. Defaults to None (all fields).

    Returns:
        pl.DataFrame: DataFrame containing scraped places data.
    """
    results = []
    browser = None

    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(
                headless=headless,
                proxy=proxy,
                args=LAUNCH_ARGS,
            )
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                java_script_enabled=True,
                accept_downloads=False,
                viewport={
                    "width": 1920 + random.randint(-50, 50),
                    "height": 1080 + random.randint(-50, 50),
                },
                permissions=["geolocation"],
                geolocation={
                    "latitude": geo_coordinates.latitude,
                    "longitude": geo_coordinates.longitude,
                },
                timezone_id="Asia/Ho_Chi_Minh",
                locale=lang,
            )
            # Lightweight, bug-free stealth override across all pages
            await context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => false });
                if (!window.chrome) { window.chrome = {}; }
                window.chrome.runtime = window.chrome.runtime || {};
            """)

            # Register context-wide bandwidth-saving route handler (search & detail pages)
            await context.route("**/*", global_route_handler)

            # 1. Fetch place URLs across queries
            tasks = [
                get_place_urls(
                    context=context,
                    max_places=max_places,
                    query=query.replace("_", " "),
                    geo_coordinates=geo_coordinates,
                    zoom=zoom,
                    lang=lang,
                )
                for query in queries
            ]
            list_of_sets_of_links = await asyncio.gather(*tasks)
            place_links = list(
                set(itertools.chain.from_iterable(list_of_sets_of_links))
            )
            logger.info(f"Collected {len(place_links)} unique place URLs.")

            # 2. Extract details for each place
            logger.info(
                f"\nScraping details for {len(place_links)} places (concurrency: {n_semaphore})..."
            )
            total = len(place_links)
            semaphore = asyncio.Semaphore(n_semaphore)

            detail_tasks = [
                process_link(context, link, semaphore, i + 1, total, fields=fields)
                for i, link in enumerate(place_links)
            ]
            raw_results = await asyncio.gather(*detail_tasks)

            results = [r for r in raw_results if r is not None]
            logger.info(f"\n✅ Successfully collected {len(results)} places.")

        except PlaywrightTimeoutError:
            logger.error("Playwright timeout error during scraping process.")
        except Exception as e:
            logger.error(f"Unexpected error during scraping: {e}")
            traceback.print_exc()
        finally:
            if browser and browser.is_connected():
                try:
                    await browser.close()
                except Exception:
                    pass

    if not results:
        return pl.DataFrame()

    return pl.from_dicts(results)
