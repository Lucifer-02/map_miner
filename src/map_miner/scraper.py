import asyncio
import itertools
import logging
import random
import re
import time
from collections.abc import Sequence
from typing import Any
from urllib.parse import quote_plus, unquote

import polars as pl
from geopy.point import Point
from playwright.async_api import (
    ChromiumBrowserContext,
    Page,
    ProxySettings,
    Route,
    async_playwright,
)
from playwright.async_api import (
    Error as PlaywrightError,
)
from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
)

from .extractor import extract_place_data, parse_preview_json
from .recaptcha_solver import RecaptchaSolver

logger = logging.getLogger(__name__)

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
BLOCKED_RESOURCE_TYPES = {"image", "media", "font"}

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

CONSENT_BUTTON_REGEX = re.compile(
    r"(?i)(?:reject all|từ chối tất cả|alle ablehnen|tout refuser|rechazar todo|"
    r"rifiuta tutto|accept all|chấp nhận tất cả|alle akzeptieren|tout accepter|i agree|tôi đồng ý)"
)

FEED_FALLBACK_SELECTORS = [
    '[role="feed"]',
    'div[aria-label*="Results for"]',
    'div[aria-label*="Kết quả cho"]',
    'div[role="main"] div[tabindex="-1"]',
]

END_OF_FEED_XPATHS = [
    '//span[contains(text(), "reached the end of the list")]',
    '//span[contains(text(), "hết danh sách")]',
    '//div[contains(text(), "reached the end")]',
]


async def global_route_handler(route: Route) -> None:
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

        # Block by resource type or URL pattern
        if req.resource_type in BLOCKED_RESOURCE_TYPES or any(
            pattern in url for pattern in BLOCKED_URL_PATTERNS
        ):
            await route.abort()
            return

        await route.continue_()
    except PlaywrightError:
        try:
            await route.continue_()
        except PlaywrightError:
            pass


def make_place_url(
    query: str, geo_coordinates: Point, zoom: float, lang: str = "en"
) -> str:
    """Builds a localized Google Maps search URL."""
    encoded_query = quote_plus(query)
    return (
        f"https://www.google.com/maps/search/{encoded_query}/"
        f"@{geo_coordinates.latitude},{geo_coordinates.longitude},{zoom}z?hl={lang}"
    )


async def pass_consent(page: Page) -> bool:
    """
    Attempts to dismiss Google's cookie/privacy consent banner
    across multiple languages safely.
    """
    logger.debug("Checking for consent dialog...")
    try:
        button = page.get_by_role("button", name=CONSENT_BUTTON_REGEX)
        if await button.count() > 0 and await button.first.is_visible():
            await button.first.click()
            await asyncio.sleep(1.0)
            logger.debug("Consent dismissed via button.")
            return True
    except PlaywrightError as e:
        logger.debug("Consent button check error: %s", e)

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
    except PlaywrightError as e:
        logger.debug("Consent form check error: %s", e)

    return False


async def handle_captcha_if_present(page: Page, context_label: str = "") -> bool:
    """Detects and attempts to solve reCAPTCHA if encountered."""
    is_captcha = "sorry/index" in page.url
    if not is_captcha:
        try:
            loc = page.locator('text="Our systems have detected unusual traffic"')
            if asyncio.iscoroutine(loc):
                loc = await loc
            is_captcha = (await loc.count()) > 0
        except PlaywrightError:
            pass

    if is_captcha:
        tag = f" ({context_label})" if context_label else ""
        logger.warning("🚨 CAPTCHA detected%s!", tag)
        solver = RecaptchaSolver(page)
        try:
            return await solver.solve_captcha()
        except (PlaywrightError, RuntimeError, OSError) as e:
            logger.error("Failed to solve CAPTCHA: %s", e)
            return False

    return False


async def find_feed_selector(page: Page, timeout: int = 15000) -> str | None:
    """Locates the results feed container selector on a Google Maps search page."""
    try:
        await page.wait_for_selector('[role="feed"]', state="visible", timeout=timeout)
        return '[role="feed"]'
    except PlaywrightTimeoutError:
        for selector in FEED_FALLBACK_SELECTORS[1:]:
            if await page.locator(selector).count() > 0:
                return selector
    return None


async def scroll_feed(page: Page, feed_selector: str) -> None:
    """Scrolls down the search results feed container."""
    try:
        feed_locator = page.locator(feed_selector)
        await feed_locator.hover()
        await page.mouse.wheel(0, 5000)
    except PlaywrightError as e:
        logger.debug("Wheel scroll error: %s", e)

    try:
        await page.evaluate(
            """(sel) => {
                const el = document.querySelector(sel);
                if (el) el.scrollTop = el.scrollHeight;
            }""",
            feed_selector,
        )
    except PlaywrightError as e:
        logger.debug("Scroll evaluation error: %s", e)

    await asyncio.sleep(random.uniform(1.0, 1.6))


async def is_feed_at_end(page: Page) -> bool:
    """Checks if any multi-lingual end of results list marker is visible."""
    for xpath in END_OF_FEED_XPATHS:
        try:
            loc = page.locator(xpath)
            if asyncio.iscoroutine(loc):
                loc = await loc
            if await loc.count() > 0 and await loc.first.is_visible():
                return True
        except PlaywrightError:
            continue
    return False


class PreviewInterceptor:
    """
    Listens for /maps/preview/place XHR responses on a Page,
    capturing the rich place detail payload.
    """

    def __init__(self, page: Page) -> None:
        self.preview_json: str | None = None
        self.preview_event = asyncio.Event()
        self._page = page
        page.on("response", self._handle_response)

    async def _handle_response(self, response: Any) -> None:
        if "maps/preview/place" in response.url:
            try:
                text = await response.text()
                if text and parse_preview_json(text) is not None:
                    self.preview_json = text
                    self.preview_event.set()
            except PlaywrightError:
                pass

    def reset(self) -> None:
        """Clears the captured payload and resets the wait event."""
        self.preview_json = None
        self.preview_event.clear()

    async def wait_for_preview(self, timeout: float = 3.5) -> str | None:
        """Waits up to timeout seconds for a rich preview payload."""
        try:
            await asyncio.wait_for(self.preview_event.wait(), timeout=timeout)
            return self.preview_json
        except TimeoutError:
            return None


def is_preview_response_for_link(response: Any, canonical_link: str) -> bool:
    """
    Validates whether an intercepted HTTP response corresponds to the place preview
    for a specific canonical link, avoiding race conditions and cross-place data leakage.
    """
    raw_url = getattr(response, "url", "")
    status = getattr(response, "status", 0)
    ok = getattr(response, "ok", status == 200)

    if "maps/preview/place" not in raw_url or (status != 200 and not ok):
        return False

    decoded_canonical = unquote(canonical_link)
    decoded_url = unquote(raw_url).lower()

    hex_match = re.search(r"0x[0-9a-fA-F]+:0x[0-9a-fA-F]+", decoded_canonical)
    if hex_match:
        place_hex_id = hex_match.group(0).lower()
        if place_hex_id not in decoded_url:
            return False

    return True


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
    Used in multi-page fallback mode.
    """
    search_page = await context.new_page()
    if not search_page:
        raise RuntimeError("Failed to create search browser page.")

    place_links: set[str] = set()

    try:
        search_url = make_place_url(
            query=query, geo_coordinates=geo_coordinates, zoom=zoom, lang=lang
        )
        logger.info("Navigating to search URL: %s", search_url)

        await search_page.goto(
            search_url, wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT
        )
        await asyncio.sleep(random.uniform(1.0, 2.5))

        if "consent" in search_page.url:
            await pass_consent(search_page)

        await handle_captcha_if_present(search_page, context_label="search_page")

        # Check if single result redirect happened
        if "/maps/place/" in search_page.url:
            logger.debug("Detected single place redirect.")
            place_links.add(search_page.url)
            return place_links

        active_feed_selector = await find_feed_selector(search_page)
        if not active_feed_selector:
            if "/maps/place/" in search_page.url:
                place_links.add(search_page.url)
                return place_links
            logger.error("Could not find results feed selector on search page.")
            return place_links

        last_height = await search_page.evaluate(
            "(sel) => document.querySelector(sel)?.scrollHeight || 0",
            active_feed_selector,
        )
        scroll_attempts_no_new = 0

        while True:
            await scroll_feed(search_page, active_feed_selector)

            current_links_list = await search_page.locator(
                f'{active_feed_selector} a[href*="/maps/place/"]'
            ).evaluate_all("elements => elements.map(a => a.href)")
            current_links = set(current_links_list)
            new_links = current_links - place_links
            place_links.update(current_links)
            logger.debug("Found %d unique place links so far...", len(place_links))

            if max_places is not None and len(place_links) >= max_places:
                logger.debug("Reached max_places limit (%d).", max_places)
                place_links = set(itertools.islice(place_links, max_places))
                break

            new_height = await search_page.evaluate(
                "(sel) => document.querySelector(sel)?.scrollHeight || 0",
                active_feed_selector,
            )

            is_at_end = await is_feed_at_end(search_page)
            if is_at_end and not new_links:
                logger.debug("Reached end of results list marker and no new links.")
                break

            if new_height == last_height and not new_links:
                scroll_attempts_no_new += 1
                logger.debug(
                    "Scroll height unchanged (%d/%d).",
                    scroll_attempts_no_new,
                    MAX_SCROLL_ATTEMPTS_WITHOUT_NEW_LINKS,
                )
                await asyncio.sleep(1.5)
                if scroll_attempts_no_new >= MAX_SCROLL_ATTEMPTS_WITHOUT_NEW_LINKS:
                    break
            else:
                last_height = new_height
                scroll_attempts_no_new = 0

    except PlaywrightError as e:
        logger.error("Playwright error during get_place_urls: %s", e)
    finally:
        if search_page and not search_page.is_closed():
            try:
                await search_page.close()
            except PlaywrightError:
                pass

    return place_links


async def scrape_query_spa(
    context: ChromiumBrowserContext,
    query: str,
    geo_coordinates: Point,
    zoom: float,
    max_places: int = 120,
    lang: str = "en",
    fields: Sequence[str] | set[str] | None = None,
) -> list[dict[str, Any]]:
    """
    Scrapes Google Maps places using client-side SPA navigation:
    - Navigates once to the search feed URL.
    - Clicks each place card in the feed client-side without full page reloads.
    - Intercepts /maps/preview/place XHR payloads (~90% request savings).
    - Dynamically scrolls the feed as items are consumed.
    """
    search_page = await context.new_page()
    if not search_page:
        raise RuntimeError("Failed to create search browser page.")

    results: list[dict[str, Any]] = []
    processed_links: set[str] = set()

    try:
        search_url = make_place_url(
            query=query, geo_coordinates=geo_coordinates, zoom=zoom, lang=lang
        )
        logger.info("Navigating to search URL (SPA mode): %s", search_url)

        await search_page.goto(
            search_url, wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT
        )
        await asyncio.sleep(random.uniform(1.0, 2.0))

        if "consent" in search_page.url:
            await pass_consent(search_page)

        await handle_captcha_if_present(search_page, context_label="spa_search")

        # Check if single result redirect happened
        if "/maps/place/" in search_page.url:
            logger.debug("Detected single place redirect.")
            html_content = await search_page.content()
            place_data = extract_place_data(html_content=html_content, fields=fields)
            if place_data:
                if fields is None or "link" in fields:
                    place_data["link"] = search_page.url
                results.append(place_data)
            return results

        active_feed_selector = await find_feed_selector(search_page)
        if not active_feed_selector:
            logger.error("Could not find results feed selector on search page.")
            return results

        scroll_attempts_no_new = 0
        last_height = await search_page.evaluate(
            "(sel) => document.querySelector(sel)?.scrollHeight || 0",
            active_feed_selector,
        )

        while max_places is None or len(results) < max_places:
            link_elements = await search_page.locator(
                f'{active_feed_selector} a[href*="/maps/place/"]'
            ).all()

            found_new_in_batch = False

            for el in link_elements:
                if max_places is not None and len(results) >= max_places:
                    break

                link = await el.get_attribute("href")
                if not link:
                    continue

                canonical_link = link.split("?")[0]
                if canonical_link in processed_links:
                    continue

                def is_matching_preview(
                    resp: Any, target: str = canonical_link
                ) -> bool:
                    return is_preview_response_for_link(resp, target)

                click_succeeded = False
                preview_json: str | None = None

                try:
                    async with search_page.expect_response(
                        is_matching_preview,
                        timeout=5000,
                    ) as response_info:
                        try:
                            await el.evaluate("e => e.click()")
                            click_succeeded = True
                        except PlaywrightError:
                            await el.scroll_into_view_if_needed(timeout=1000)
                            await el.click(force=True, timeout=1000)
                            click_succeeded = True

                    response = await response_info.value
                    preview_json = await response.text()
                except (PlaywrightTimeoutError, PlaywrightError) as e:
                    if click_succeeded:
                        # Click succeeded but preview timed out; mark to avoid repeated attempts
                        processed_links.add(canonical_link)
                    logger.warning(
                        "  ⚠️ Error or timeout waiting for SPA preview: %s (%s)",
                        canonical_link,
                        e,
                    )
                    continue

                # Add to processed_links only after click succeeded
                processed_links.add(canonical_link)
                found_new_in_batch = True

                preview_blob = (
                    parse_preview_json(preview_json) if preview_json else None
                )
                if not preview_blob:
                    logger.warning(
                        "  ⚠️ Invalid preview JSON structure for: %s",
                        canonical_link,
                    )
                    continue

                place_data = extract_place_data(
                    html_content=None,
                    preview_blob=preview_blob,
                    preview_json=preview_json,
                    fields=fields,
                )
                if place_data and (
                    fields is not None or "name" in place_data or len(place_data) >= 3
                ):
                    if fields is None or "link" in fields:
                        place_data["link"] = link
                    results.append(place_data)
                    logger.info(
                        "  ✅ [SPA %d/%s] Extracted: %s",
                        len(results),
                        str(max_places),
                        place_data.get("name", canonical_link),
                    )

                await asyncio.sleep(random.uniform(0.15, 0.35))

            if max_places is not None and len(results) >= max_places:
                break

            await scroll_feed(search_page, active_feed_selector)

            is_at_end = await is_feed_at_end(search_page)
            remaining_links = await search_page.locator(
                f'{active_feed_selector} a[href*="/maps/place/"]'
            ).evaluate_all("els => els.map(a => a.href.split('?')[0])")
            has_unprocessed = any(l not in processed_links for l in remaining_links)

            if is_at_end and not has_unprocessed and not found_new_in_batch:
                logger.debug(
                    "Reached end of results list marker in SPA feed and all items processed."
                )
                break

            new_height = await search_page.evaluate(
                "(sel) => document.querySelector(sel)?.scrollHeight || 0",
                active_feed_selector,
            )

            if (
                new_height == last_height
                and not found_new_in_batch
                and not has_unprocessed
            ):
                scroll_attempts_no_new += 1
                await asyncio.sleep(1.5)
                if scroll_attempts_no_new >= MAX_SCROLL_ATTEMPTS_WITHOUT_NEW_LINKS:
                    logger.debug("Stopping SPA scroll due to lack of new items.")
                    break
            else:
                last_height = new_height
                scroll_attempts_no_new = 0

    except PlaywrightError as e:
        logger.error("Playwright error during scrape_query_spa: %s", e)
    finally:
        if search_page and not search_page.is_closed():
            try:
                await search_page.close()
            except PlaywrightError:
                pass

    return results


async def process_link(
    context: ChromiumBrowserContext,
    link: str,
    semaphore: asyncio.Semaphore,
    count: int,
    total: int,
    fields: Sequence[str] | set[str] | None = None,
    max_retries: int = 2,
) -> dict[str, Any] | None:
    """
    Processes a single place link in multi-page fallback mode:
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
                    "Processing link [%d/%d] (attempt %d/%d): %s",
                    count,
                    total,
                    attempt,
                    max_retries,
                    link,
                )
                page = await context.new_page()
                interceptor = PreviewInterceptor(page)

                await page.set_extra_http_headers(
                    {
                        "Referer": "https://www.google.com/",
                        "Accept-Language": "en-US,en;q=0.9,vi;q=0.8",
                    }
                )

                try:
                    await page.goto(
                        link,
                        wait_until="domcontentloaded",
                        timeout=DEFAULT_TIMEOUT,
                    )
                except PlaywrightTimeoutError:
                    logger.warning("  ❌ Timeout navigating to: %s", link)
                    if attempt < max_retries:
                        await asyncio.sleep(random.uniform(0.8, 1.5))
                        continue
                    return None
                except PlaywrightError as e:
                    logger.error("  ❌ Navigation error for %s: %s", link, e)
                    if attempt < max_retries:
                        await asyncio.sleep(random.uniform(0.8, 1.5))
                        continue
                    return None

                # Wait for preview API response
                preview_json = await interceptor.wait_for_preview(timeout=4.5)
                if not preview_json:
                    try:
                        await page.wait_for_selector("h1, [role='main']", timeout=2000)
                    except PlaywrightError:
                        pass

                # Early exit if rich preview JSON was intercepted
                if preview_json:
                    place_data = extract_place_data(
                        html_content=None,
                        preview_json=preview_json,
                        fields=fields,
                    )
                    if place_data is not None and (
                        fields is not None
                        or "name" in place_data
                        or len(place_data) >= 3
                    ):
                        if fields is None or "link" in fields:
                            place_data["link"] = link
                        elapsed = time.time() - start_time
                        logger.info(
                            "  ✅ Extracted (early preview): %s in %.2fs",
                            link,
                            elapsed,
                        )
                        return place_data

                # CAPTCHA verification
                captcha_solved = await handle_captcha_if_present(
                    page, context_label="process_link"
                )
                if (
                    not captcha_solved
                    and "sorry/index" in page.url
                    and attempt < max_retries
                ):
                    await asyncio.sleep(random.uniform(1.0, 2.0))
                    continue

                html_content = await page.content()
                place_data = extract_place_data(
                    html_content=html_content,
                    preview_json=preview_json,
                    fields=fields,
                )

                if place_data is not None:
                    if fields is None or "link" in fields:
                        place_data["link"] = link
                    elapsed = time.time() - start_time
                    logger.info("  ✅ Extracted: %s in %.2fs", link, elapsed)
                    return place_data

                logger.warning("  ⚠️ Extraction returned None: %s", link)
                if attempt < max_retries:
                    await asyncio.sleep(random.uniform(0.8, 1.5))
                    continue
                return None

            except PlaywrightError as e:
                logger.error(
                    "  ❌ Error processing %s (attempt %d): %s",
                    link,
                    attempt,
                    e,
                )
                if attempt < max_retries:
                    await asyncio.sleep(random.uniform(0.8, 1.5))
                else:
                    return None
            finally:
                if page and not page.is_closed():
                    try:
                        await page.close()
                    except PlaywrightError:
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
    fields: Sequence[str] | set[str] | None = None,
    use_spa: bool = True,
) -> pl.DataFrame:
    """
    Scrapes Google Maps for places based on queries.

    Args:
        queries (set[str]): Search queries (e.g. {"cafe", "restaurant"}).
        geo_coordinates (Point): Center coordinates for search.
        zoom (float): Map zoom level.
        proxy (ProxySettings, optional): Proxy configuration for Playwright.
        max_places (int, optional): Maximum places to collect per query. Defaults to 120.
        lang (str, optional): Language code for Google Maps. Defaults to "en".
        headless (bool, optional): Whether to run headless browser. Defaults to False.
        n_semaphore (int, optional): Maximum concurrent browser tabs/queries. Defaults to 8.
        fields (Sequence[str] | set[str], optional): Selected fields to extract. Defaults to None (all fields).
        use_spa (bool, optional): Whether to use high-speed SPA navigation. Defaults to True.

    Returns:
        pl.DataFrame: DataFrame containing scraped places data.
    """
    results: list[dict[str, Any]] = []
    browser = None

    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(
                headless=headless,
                proxy=proxy,
                args=LAUNCH_ARGS,
            )
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
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

            # Lightweight stealth override
            await context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => false });
                if (!window.chrome) { window.chrome = {}; }
                window.chrome.runtime = window.chrome.runtime || {};
            """)

            # Context-wide bandwidth-saving route handler
            await context.route("**/*", global_route_handler)

            query_semaphore = asyncio.Semaphore(n_semaphore)

            if use_spa:
                logger.info(
                    "Scraping in SPA Navigation mode (max concurrency: %d)...",
                    n_semaphore,
                )

                async def run_spa_query(q: str):
                    async with query_semaphore:
                        return await scrape_query_spa(
                            context=context,
                            query=q.replace("_", " "),
                            geo_coordinates=geo_coordinates,
                            zoom=zoom,
                            max_places=max_places,
                            lang=lang,
                            fields=fields,
                        )

                spa_tasks = [run_spa_query(query) for query in queries]
                list_of_results = await asyncio.gather(*spa_tasks)
                results = list(itertools.chain.from_iterable(list_of_results))
                logger.info(
                    "✅ Successfully collected %d places via SPA Navigation.",
                    len(results),
                )
            else:
                # Multi-page fallback mode
                async def run_get_urls(q: str):
                    async with query_semaphore:
                        return await get_place_urls(
                            context=context,
                            max_places=max_places,
                            query=q.replace("_", " "),
                            geo_coordinates=geo_coordinates,
                            zoom=zoom,
                            lang=lang,
                        )

                tasks = [run_get_urls(query) for query in queries]
                list_of_sets_of_links = await asyncio.gather(*tasks)
                place_links = list(
                    set(itertools.chain.from_iterable(list_of_sets_of_links))
                )
                logger.info("Collected %d unique place URLs.", len(place_links))

                logger.info(
                    "Scraping details for %d places (concurrency: %d)...",
                    len(place_links),
                    n_semaphore,
                )
                total = len(place_links)
                detail_semaphore = asyncio.Semaphore(n_semaphore)

                detail_tasks = [
                    process_link(
                        context,
                        link,
                        detail_semaphore,
                        i + 1,
                        total,
                        fields=fields,
                    )
                    for i, link in enumerate(place_links)
                ]
                raw_results = await asyncio.gather(*detail_tasks)
                results = [r for r in raw_results if r is not None]
                logger.info("✅ Successfully collected %d places.", len(results))

        except PlaywrightTimeoutError:
            logger.error("Playwright timeout error during scraping process.")
        except Exception as e:  # noqa: BLE001
            logger.error("Unexpected error during scraping: %s", e)
        finally:
            if browser and browser.is_connected():
                try:
                    await browser.close()
                except PlaywrightError:
                    pass

    if not results:
        return pl.DataFrame()

    return pl.from_dicts(results, infer_schema_length=None)
