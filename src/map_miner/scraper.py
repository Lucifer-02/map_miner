import asyncio
import hashlib
import itertools
import json
import logging
import random
import re
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus, unquote

import polars as pl
from geopy.distance import geodesic
from geopy.point import Point
from playwright.async_api import (
    Browser,
    BrowserContext,
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
from .proxy import (
    DEFAULT_PROXY_BYPASS,
    ProxyRotator,
    get_tor_rotating_proxy,
    renew_tor_circuit_control,
)
from .recaptcha_solver import (
    RecaptchaBlockedError,
    RecaptchaError,
    RecaptchaSolver,
)

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_CAPTCHA_TIMEOUT",
    "DEFAULT_FLATTEN_COLUMNS",
    "DEFAULT_PLACE_TIMEOUT",
    "DEFAULT_PROXY_BYPASS",
    "DEFAULT_QUERY_TIMEOUT",
    "DEFAULT_RANGE_LIMIT",
    "DEFAULT_SPA_PREVIEW_TIMEOUT",
    "DEFAULT_STATIC_CACHE_DIR",
    "MAX_CONSECUTIVE_EMPTY_SCROLLS",
    "REQUIRED_COLUMNS",
    "ProxyRotator",
    "create_browser_context",
    "extract_coordinates_from_url",
    "find_feed_selector",
    "format_places_dataframe",
    "get_place_urls",
    "get_tor_rotating_proxy",
    "global_route_handler",
    "handle_captcha_if_present",
    "is_feed_at_end",
    "is_no_results_page",
    "is_preview_response_for_link",
    "make_place_url",
    "pass_consent",
    "process_link",
    "renew_tor_circuit_control",
    "scrape_google_maps",
    "scrape_query_spa",
    "scroll_feed",
]

# --- Constants ---
DEFAULT_FLATTEN_COLUMNS: tuple[str, ...] = (
    "name",
    "place_id",
    "latitude",
    "longitude",
    "address",
    "link",
    "categories",
    "rating",
    "reviews_count",
    "plus_code",
    "city",
)
REQUIRED_COLUMNS: tuple[str, ...] = (
    DEFAULT_FLATTEN_COLUMNS  # Backward compatibility alias
)
DEFAULT_TIMEOUT = 30000  # 30 seconds for navigation and selectors
DEFAULT_QUERY_TIMEOUT = 300.0  # 5 minutes per query
DEFAULT_PLACE_TIMEOUT = 45.0  # 45 seconds per detail place link
DEFAULT_CAPTCHA_TIMEOUT = 85.0  # 85 seconds for multi-round reCAPTCHA solving
DEFAULT_SPA_PREVIEW_TIMEOUT = 10000  # 10 seconds (10000ms) for SPA preview XHR response
DEFAULT_RANGE_LIMIT: float = 10000.0  # 10 km default ceiling radius
MAX_CONSECUTIVE_EMPTY_SCROLLS = 6
MAX_SCROLL_ATTEMPTS_WITHOUT_NEW_LINKS = (
    5  # Allow enough attempts for slow network / lazy load
)
DEFAULT_CACHE_DIR = Path(".cache") / "chromium_cache"
DEFAULT_STATIC_CACHE_DIR = Path(".cache") / "static_assets"
DEFAULT_DISK_CACHE_SIZE = 1073741824  # 1 GB

DYNAMIC_ROUTE_PATTERNS: tuple[str, ...] = (
    "/maps/preview/",
    "/maps/rpc/",
    "/maps/search/",
    "sorry/",
    "recaptcha",
)
STATIC_ROUTE_PATTERNS: tuple[str, ...] = (
    "/maps/_/js/",
    "/maps/_/ss/",
    "/maps/res/",
)
STATIC_EXTENSIONS: tuple[str, ...] = (
    ".js",
    ".css",
    ".woff2",
    ".png",
)

# Stable launch args: headless/stealth-safe, avoids crashes on multi-page Chromium
LAUNCH_ARGS = [
    "--no-default-browser-check",
    "--disable-dev-shm-usage",
    "--disable-setuid-sandbox",
    "--no-sandbox",
    "--no-zygote",
    "--enable-webgl",
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
    "client_204",
    "cspreport",
    "/maps/photometa",
    "googleusercontent.com",
    "ggpht.com",
    "streetviewpixels",
    "feedback-pa.clients6.google.com",
    "ogads-pa.clients6.google.com",
    "/maps/preview/entity",
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

NO_RESULTS_SELECTORS: tuple[str, ...] = (
    "div.Q27duf",
    'div[role="main"] div.Q27duf',
)
NO_RESULTS_TEXT_PATTERNS: tuple[str, ...] = (
    "Google Maps can't find",
    "Google Maps không thể tìm thấy",
    "No results found",
    "Không tìm thấy kết quả",
    "Make sure your search is spelled correctly",
    "Hãy đảm bảo rằng bạn đã viết đúng chính tả",
)


async def global_route_handler(route: Route) -> None:
    """
    Context-wide route handler to block heavy resources and tracking,
    saving significant network bandwidth while caching static assets
    and preserving reCAPTCHA and core APIs.
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

        # Application-level static asset caching
        is_static_asset = False
        if req.method.upper() == "GET" and not any(
            dyn in url for dyn in DYNAMIC_ROUTE_PATTERNS
        ):
            if any(pattern in url for pattern in STATIC_ROUTE_PATTERNS):
                is_static_asset = True
            elif "gstatic.com" in url:
                clean_url = url.split("?")[0].split("#")[0]
                if any(clean_url.endswith(ext) for ext in STATIC_EXTENSIONS):
                    is_static_asset = True

        if is_static_asset:
            cache_key = hashlib.sha256(url.encode("utf-8")).hexdigest()
            cache_file = DEFAULT_STATIC_CACHE_DIR / cache_key

            try:
                if cache_file.is_file() and cache_file.stat().st_size > 0:
                    cached_bytes = cache_file.read_bytes()
                    if ".js" in url or "/js/" in url:
                        content_type = "application/javascript; charset=utf-8"
                    elif ".css" in url or "/ss/" in url:
                        content_type = "text/css; charset=utf-8"
                    else:
                        content_type = "application/octet-stream"

                    await route.fulfill(
                        body=cached_bytes,
                        status=200,
                        headers={
                            "content-type": content_type,
                            "x-cache": "HIT-ROUTE-CACHE",
                        },
                    )
                    return
            except OSError:
                pass

            response = await route.fetch()
            if response.status == 200:
                try:
                    body = await response.body()
                    DEFAULT_STATIC_CACHE_DIR.mkdir(parents=True, exist_ok=True)
                    cache_file.write_bytes(body)
                except OSError:
                    pass
            await route.fulfill(response=response)
            return

        await route.continue_()
    except PlaywrightError:
        try:
            await route.continue_()
        except PlaywrightError:
            pass


async def create_browser_context(
    browser: Browser,
    geo_coordinates: Point,
    lang: str = "en",
    proxy: (
        ProxySettings
        | dict[str, Any]
        | Sequence[ProxySettings | dict[str, Any] | Any]
        | str
        | Sequence[str]
        | None
    ) = None,
) -> BrowserContext:
    """Creates and configures an isolated BrowserContext with proxy settings,
    stealth overrides, geolocation, and global resource blocking.

    Args:
        browser (Browser): Playwright browser instance.
        geo_coordinates (Point): Geographic center coordinates for geolocation mocking.
        lang (str, optional): Language code. Defaults to "en".
        proxy (ProxySettings | Sequence[ProxySettings] | str | Sequence[str] | None, optional):
            Proxy settings for this context. Defaults to None.

    Returns:
        BrowserContext: Configured isolated browser context.
    """
    accept_lang = f"{lang}-{lang.upper()},{lang};q=0.9,en-US;q=0.8,en;q=0.7"
    context_options: dict[str, Any] = {
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "extra_http_headers": {
            "sec-ch-ua": (
                '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"'
            ),
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "Accept-Language": accept_lang,
        },
        "java_script_enabled": True,
        "accept_downloads": False,
        "viewport": {
            "width": 1920 + random.randint(-50, 50),
            "height": 1080 + random.randint(-50, 50),
        },
        "permissions": ["geolocation"],
        "timezone_id": "Asia/Ho_Chi_Minh",
        "locale": lang,
        "geolocation": {
            "latitude": geo_coordinates.latitude,
            "longitude": geo_coordinates.longitude,
        },
    }

    if proxy is not None:
        context_options["proxy"] = proxy

    context = await browser.new_context(**context_options)

    # Comprehensive stealth and anti-fingerprint override script
    stealth_script = f"""
        // 1. Remove automation indicators
        Object.defineProperty(navigator, 'webdriver', {{ get: () => false }});
        Object.defineProperty(navigator, 'platform', {{ get: () => 'Win32' }});
        Object.defineProperty(navigator, 'languages', {{
            get: () => ['{lang}-{lang.upper()}', '{lang}', 'en-US', 'en']
        }});
        Object.defineProperty(navigator, 'hardwareConcurrency', {{ get: () => 8 }});
        Object.defineProperty(navigator, 'deviceMemory', {{ get: () => 8 }});

        // 2. Standard navigator.plugins simulation
        Object.defineProperty(navigator, 'plugins', {{
            get: () => {{
                const plugins = [
                    {{ name: "Chrome PDF Viewer", filename: "internal-pdf-viewer", description: "Portable Document Format" }},
                    {{ name: "Chromium PDF Viewer", filename: "internal-pdf-viewer", description: "Portable Document Format" }},
                    {{ name: "Microsoft Edge PDF Viewer", filename: "internal-pdf-viewer", description: "Portable Document Format" }},
                    {{ name: "WebKit built-in PDF", filename: "internal-pdf-viewer", description: "Portable Document Format" }}
                ];
                plugins.item = (i) => plugins[i] || null;
                plugins.namedItem = (name) => plugins.find(p => p.name === name) || null;
                plugins.refresh = () => {{}};
                return plugins;
            }}
        }});

        // 3. Mock window.chrome runtime
        if (!window.chrome) {{ window.chrome = {{}}; }}
        window.chrome.runtime = window.chrome.runtime || {{}};
        window.chrome.app = window.chrome.app || {{}};

        // 4. WebGL vendor/renderer spoofing (prevent SwiftShader / llvmpipe leaks)
        const patchWebGL = (glProto) => {{
            if (!glProto || !glProto.getParameter) return;
            const origGetParameter = glProto.getParameter;
            glProto.getParameter = function(param) {{
                if (param === 37445) {{
                    return "Google Inc. (NVIDIA)";
                }}
                if (param === 37446) {{
                    return "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)";
                }}
                const res = origGetParameter.apply(this, arguments);
                if (typeof res === 'string' && (res.includes('SwiftShader') || res.includes('llvmpipe'))) {{
                    return "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)";
                }}
                return res;
            }};
        }};
        if (typeof WebGLRenderingContext !== 'undefined') {{
            patchWebGL(WebGLRenderingContext.prototype);
        }}
        if (typeof WebGL2RenderingContext !== 'undefined') {{
            patchWebGL(WebGL2RenderingContext.prototype);
        }}
    """
    await context.add_init_script(stealth_script)

    # Context-wide bandwidth-saving route handler
    await context.route("**/*", global_route_handler)

    return context


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


async def handle_captcha_if_present(
    page: Page,
    context_label: str = "",
    timeout: float = DEFAULT_CAPTCHA_TIMEOUT,
) -> bool:
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
        if hasattr(solver, "save_captcha_diagnostics"):
            try:
                diag_res = solver.save_captcha_diagnostics(context_label=context_label)
                if asyncio.iscoroutine(diag_res):
                    await diag_res
            except Exception as e:  # noqa: BLE001
                logger.debug("Failed to save captcha diagnostics: %s", e)
        try:
            return await asyncio.wait_for(solver.solve_captcha(), timeout=timeout)
        except TimeoutError:
            logger.warning("🚨 reCAPTCHA solving timed out after %.1fs%s", timeout, tag)
            return False
        except RecaptchaBlockedError as e:
            logger.warning(
                "🚨 reCAPTCHA challenge hard-blocked by Google%s: %s", tag, e
            )
            return False
        except (RecaptchaError, PlaywrightError, RuntimeError, OSError) as e:
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
        feed_locator = page.locator(feed_selector).first
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


async def is_no_results_page(page: Page) -> bool:
    """Checks if the search page indicates that no results were found for the query."""
    for selector in NO_RESULTS_SELECTORS:
        try:
            loc = page.locator(selector)
            if asyncio.iscoroutine(loc):
                loc = await loc
            if await loc.count() > 0 and await loc.first.is_visible():
                return True
        except PlaywrightError:
            continue

    for pattern in NO_RESULTS_TEXT_PATTERNS:
        try:
            loc = page.locator(f'text="{pattern}"')
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


def extract_coordinates_from_url(url: str) -> tuple[float, float] | None:
    """
    Extracts geographic coordinates (latitude, longitude) from a Google Maps URL.

    Matches formats:
    1. Feed / detail link protobuf format: '!3d<lat>...!4d<lon>'
    2. Viewport coordinate format: '@<lat>,<lon>'

    Args:
        url (str): The Google Maps URL string.

    Returns:
        tuple[float, float] | None: (latitude, longitude) tuple or None if not found/invalid.
    """
    if not url or not isinstance(url, str):
        return None

    decoded_url = unquote(url)

    # 1. Primary feed / place protobuf format: !3d<lat>...!4d<lon>
    match = re.search(r"!3d(-?\d+(?:\.\d+)?).*?!4d(-?\d+(?:\.\d+)?)", decoded_url)
    if match:
        try:
            lat = float(match.group(1))
            lon = float(match.group(2))
            if -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0:
                return lat, lon
        except (ValueError, TypeError):
            pass

    # 2. Fallback viewport format: @<lat>,<lon>
    match = re.search(r"@(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)", decoded_url)
    if match:
        try:
            lat = float(match.group(1))
            lon = float(match.group(2))
            if -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0:
                return lat, lon
        except (ValueError, TypeError):
            pass

    return None


async def get_place_urls(
    context: BrowserContext,
    max_places: int,
    query: str,
    geo_coordinates: Point,
    zoom: float,
    lang: str = "en",
    range_limit: float = DEFAULT_RANGE_LIMIT,
    proxy_rotator: ProxyRotator | None = None,
    query_timeout: float = DEFAULT_QUERY_TIMEOUT,
) -> set[str]:
    """Navigates the search feed and scrolls to collect place links.

    Used in multi-page fallback mode.
    Supports early drop when places exceed range_limit.

    Args:
        context (BrowserContext): Isolated browser context.
        max_places (int): Maximum number of valid place links to collect. Places
            dropped via range_limit (early drop) do not count toward this limit.
        query (str): Search query string.
        geo_coordinates (Point): Center coordinates for search.
        zoom (float): Map zoom level.
        lang (str, optional): Language code. Defaults to "en".
        range_limit (float): Maximum radius distance in meters from geo_coordinates.
            Google Maps local ranking combines Relevance, Distance, and Prominence
            (https://support.google.com/business/answer/7091). Prominent places further away
            may be returned before closer ones, so results are not strictly monotonic by distance.
            range_limit filters out places exceeding this radius (early drop).
            Defaults to DEFAULT_RANGE_LIMIT (10000.0m).
        proxy_rotator (ProxyRotator | None, optional): Rotator to renew proxy on CAPTCHA block. Defaults to None.
        query_timeout (float): Maximum seconds allowed for this query. Defaults to DEFAULT_QUERY_TIMEOUT (300.0s).

    Returns:
        set[str]: Collected place URLs.
    """
    search_page = await context.new_page()
    if not search_page:
        raise RuntimeError("Failed to create search browser page.")

    place_links: set[str] = set()

    try:
        query_start = time.monotonic()
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

        is_blocked = False
        try:
            captcha_solved = await handle_captcha_if_present(
                search_page, context_label="search_page"
            )
        except RecaptchaBlockedError:
            captcha_solved = False
            is_blocked = True

        if (
            (is_blocked or not captcha_solved)
            and "sorry/index" in search_page.url
            and proxy_rotator is not None
        ):
            proxy_rotator.renew()

        # Check if single result redirect happened
        if "/maps/place/" in search_page.url:
            logger.debug("Detected single place redirect.")
            coords = extract_coordinates_from_url(search_page.url)
            if coords is not None:
                dist = geodesic(
                    (geo_coordinates.latitude, geo_coordinates.longitude),
                    coords,
                ).meters
                if dist > range_limit:
                    logger.debug(
                        "Early drop: Place %s is %.1fm away, exceeding range limit (%.1fm).",
                        search_page.url,
                        dist,
                        range_limit,
                    )
                    return place_links
            place_links.add(search_page.url)
            return place_links

        active_feed_selector = await find_feed_selector(search_page)
        if not active_feed_selector:
            if "/maps/place/" in search_page.url:
                coords = extract_coordinates_from_url(search_page.url)
                if coords is not None:
                    dist = geodesic(
                        (geo_coordinates.latitude, geo_coordinates.longitude),
                        coords,
                    ).meters
                    if dist > range_limit:
                        logger.debug(
                            "Early drop: Place %s is %.1fm away, exceeding range limit (%.1fm).",
                            search_page.url,
                            dist,
                            range_limit,
                        )
                        return place_links
                place_links.add(search_page.url)
                return place_links
            if await is_no_results_page(search_page):
                logger.info("No results found for query '%s'.", query)
                return place_links
            logger.error("Could not find results feed selector on search page.")
            return place_links

        last_height = await search_page.evaluate(
            "(sel) => document.querySelector(sel)?.scrollHeight || 0",
            active_feed_selector,
        )
        scroll_attempts_no_new = 0
        consecutive_empty_scrolls = 0
        processed_links: set[str] = set()

        while True:
            if (time.monotonic() - query_start) >= query_timeout:
                logger.warning(
                    "⚠️ Query '%s' reached timeout limit (%.1fs) in get_place_urls. Returning %d collected links.",
                    query,
                    query_timeout,
                    len(place_links),
                )
                break

            await scroll_feed(search_page, active_feed_selector)

            current_links_list = await search_page.locator(
                f'{active_feed_selector} a[href*="/maps/place/"]'
            ).evaluate_all("elements => elements.map(a => a.href)")

            new_links_count = 0

            for link in current_links_list:
                if not link:
                    continue

                canonical_link = link.split("?")[0]
                if canonical_link in processed_links:
                    continue

                # Early Drop per element: Google Maps local results balance Relevance, Distance,
                # and Prominence (https://support.google.com/business/answer/7091). Due to high prominence,
                # a distant place (> range_limit) can appear interspersed among closer places (< range_limit).
                # An abrupt Early Stop would miss valid nearby places in subsequent scrolls.
                # Therefore, each element is individually checked and dropped if out of range.
                coords = extract_coordinates_from_url(link)
                if coords is not None:
                    dist = geodesic(
                        (geo_coordinates.latitude, geo_coordinates.longitude),
                        coords,
                    ).meters
                    if dist > range_limit:
                        processed_links.add(canonical_link)
                        logger.debug(
                            "Early drop: Place %s is %.1fm away, exceeding range limit (%.1fm).",
                            canonical_link,
                            dist,
                            range_limit,
                        )
                        continue

                processed_links.add(canonical_link)
                place_links.add(link)
                new_links_count += 1

                if max_places is not None and len(place_links) >= max_places:
                    logger.debug("Reached max_places limit (%d).", max_places)
                    place_links = set(itertools.islice(place_links, max_places))
                    break

            if max_places is not None and len(place_links) >= max_places:
                break

            logger.debug("Found %d unique place links so far...", len(place_links))

            # Stopping condition (consecutive_empty_scrolls):
            # When multiple consecutive scrolls yield no new valid links (MAX_CONSECUTIVE_EMPTY_SCROLLS = 6),
            # the feed has exhausted relevant results or all remaining items exceed the range limit,
            # avoiding infinite scrolling, resource waste, and bot detection.
            if new_links_count == 0:
                consecutive_empty_scrolls += 1
                if consecutive_empty_scrolls >= MAX_CONSECUTIVE_EMPTY_SCROLLS:
                    logger.debug(
                        "Stopping scroll in get_place_urls: %d consecutive scrolls without new links.",
                        consecutive_empty_scrolls,
                    )
                    break
            else:
                consecutive_empty_scrolls = 0

            new_height = await search_page.evaluate(
                "(sel) => document.querySelector(sel)?.scrollHeight || 0",
                active_feed_selector,
            )

            is_at_end = await is_feed_at_end(search_page)
            if is_at_end and new_links_count == 0:
                logger.debug("Reached end of results list marker and no new links.")
                break

            if new_height == last_height and new_links_count == 0:
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

    except Exception as e:  # noqa: BLE001
        logger.error("Error during get_place_urls: %s", e)
    finally:
        if search_page and not search_page.is_closed():
            try:
                await asyncio.shield(search_page.close())
            except BaseException:  # noqa: BLE001, S110
                pass

    return place_links


async def scrape_query_spa(
    context: BrowserContext,
    query: str,
    geo_coordinates: Point,
    zoom: float,
    max_places: int = 120,
    lang: str = "en",
    fields: Sequence[str] | set[str] | None = None,
    range_limit: float = DEFAULT_RANGE_LIMIT,
    proxy_rotator: ProxyRotator | None = None,
    max_captcha_retries: int = 2,
    query_timeout: float = DEFAULT_QUERY_TIMEOUT,
    preview_timeout: float = DEFAULT_SPA_PREVIEW_TIMEOUT,
) -> list[dict[str, Any]]:
    """Scrapes Google Maps places using client-side SPA navigation:

    - Navigates once to the search feed URL.
    - Clicks each place card in the feed client-side without full page reloads.
    - Intercepts /maps/preview/place XHR payloads (~90% request savings).
    - Dynamically scrolls the feed as items are consumed.
    - Supports early drop when places exceed range_limit.
    - Automatically rotates proxy and retries with new context when CAPTCHA is blocked.

    Args:
        context (BrowserContext): Isolated browser context.
        query (str): Search query string.
        geo_coordinates (Point): Center coordinates for search.
        zoom (float): Map zoom level.
        max_places (int, optional): Maximum valid places to collect. Places dropped
            via range_limit (early drop) do not count toward this limit. Defaults to 120.
        lang (str, optional): Language code. Defaults to "en".
        fields (Sequence[str] | set[str] | None, optional): Selected fields. Defaults to None.
        range_limit (float): Maximum radius distance in meters from geo_coordinates.
            Google Maps local ranking combines Relevance, Distance, and Prominence
            (https://support.google.com/business/answer/7091). Prominent places further away
            may be returned before closer ones, so results are not strictly monotonic by distance.
            range_limit filters out places exceeding this radius (early drop).
            Defaults to DEFAULT_RANGE_LIMIT (10000.0m).
        proxy_rotator (ProxyRotator | None, optional): Proxy rotator to renew proxy on CAPTCHA. Defaults to None.
        max_captcha_retries (int, optional): Max retries on CAPTCHA sorry page. Defaults to 2.
        query_timeout (float): Maximum seconds allowed for this query. Defaults to DEFAULT_QUERY_TIMEOUT (300.0s).
        preview_timeout (float | int, optional): Maximum timeout in ms (or seconds if < 1000)
            waiting for SPA place preview XHR response. Defaults to DEFAULT_SPA_PREVIEW_TIMEOUT.

    Returns:
        list[dict[str, Any]]: List of place dictionaries.
    """
    active_context = context
    created_contexts: list[BrowserContext] = []
    results: list[dict[str, Any]] = []
    processed_links: set[str] = set()
    search_page: Page | None = None

    try:
        query_start = time.monotonic()
        search_url = make_place_url(
            query=query, geo_coordinates=geo_coordinates, zoom=zoom, lang=lang
        )

        for captcha_attempt in range(max_captcha_retries + 1):
            search_page = await active_context.new_page()
            if not search_page:
                raise RuntimeError("Failed to create search browser page.")

            logger.info("Navigating to search URL (SPA mode): %s", search_url)

            await search_page.goto(
                search_url,
                wait_until="domcontentloaded",
                timeout=DEFAULT_TIMEOUT,
            )
            await asyncio.sleep(random.uniform(1.0, 2.0))

            if "consent" in search_page.url:
                await pass_consent(search_page)

            is_blocked = False
            try:
                captcha_solved = await handle_captcha_if_present(
                    search_page, context_label="spa_search"
                )
            except RecaptchaBlockedError:
                captcha_solved = False
                is_blocked = True

            if ("sorry/index" in search_page.url or is_blocked) and not captcha_solved:
                if captcha_attempt < max_captcha_retries:
                    logger.warning(
                        "CAPTCHA blocked/unsolved on sorry page (attempt %d/%d). "
                        "Rotating proxy and retrying with fresh context...",
                        captcha_attempt + 1,
                        max_captcha_retries,
                    )
                    await search_page.close()
                    search_page = None

                    new_proxy = proxy_rotator.renew() if proxy_rotator else None
                    browser = active_context.browser
                    if browser is not None:
                        new_context = await create_browser_context(
                            browser=browser,
                            geo_coordinates=geo_coordinates,
                            lang=lang,
                            proxy=new_proxy,
                        )
                        created_contexts.append(new_context)
                        active_context = new_context
                        continue
                    break
                else:
                    logger.error("Exceeded max CAPTCHA retries on Google Sorry page.")
                    return results

            break

        if not search_page or search_page.is_closed():
            return results

        # Check if single result redirect happened
        if "/maps/place/" in search_page.url:
            logger.debug("Detected single place redirect.")
            coords = extract_coordinates_from_url(search_page.url)
            if coords is not None:
                dist = geodesic(
                    (geo_coordinates.latitude, geo_coordinates.longitude),
                    coords,
                ).meters
                if dist > range_limit:
                    logger.debug(
                        "Early drop: Place %s is %.1fm away, exceeding range limit (%.1fm).",
                        search_page.url,
                        dist,
                        range_limit,
                    )
                    return results
            html_content = await search_page.content()
            place_data = extract_place_data(html_content=html_content, fields=fields)
            if place_data:
                if fields is None or "link" in fields:
                    place_data["link"] = search_page.url
                results.append(place_data)
            return results

        active_feed_selector = await find_feed_selector(search_page)
        if not active_feed_selector:
            if await is_no_results_page(search_page):
                logger.info("No results found for query '%s'.", query)
                return results
            logger.error("Could not find results feed selector on search page.")
            return results

        scroll_attempts_no_new = 0
        consecutive_empty_scrolls = 0
        last_height = await search_page.evaluate(
            "(sel) => document.querySelector(sel)?.scrollHeight || 0",
            active_feed_selector,
        )

        while max_places is None or len(results) < max_places:
            if (time.monotonic() - query_start) >= query_timeout:
                logger.warning(
                    "⚠️ Query '%s' reached timeout limit (%.1fs). Returning %d collected places.",
                    query,
                    query_timeout,
                    len(results),
                )
                break

            link_elements = await search_page.locator(
                f'{active_feed_selector} a[href*="/maps/place/"]'
            ).all()

            found_new_in_batch = False

            for el in link_elements:
                if max_places is not None and len(results) >= max_places:
                    break

                try:
                    link = await el.get_attribute("href", timeout=1000)
                except (PlaywrightError, TypeError):
                    link = None
                if not link:
                    continue

                canonical_link = link.split("?")[0]
                if canonical_link in processed_links:
                    continue

                # Early Drop per element: Google Maps local results balance Relevance, Distance,
                # and Prominence (https://support.google.com/business/answer/7091). Due to high prominence,
                # a distant place (> range_limit) can appear interspersed among closer places (< range_limit).
                # An abrupt Early Stop would miss valid nearby places in subsequent scrolls.
                # Therefore, each element is individually checked and dropped if out of range.
                coords = extract_coordinates_from_url(link)
                if coords is not None:
                    dist = geodesic(
                        (
                            geo_coordinates.latitude,
                            geo_coordinates.longitude,
                        ),
                        coords,
                    ).meters
                    if dist > range_limit:
                        processed_links.add(canonical_link)
                        logger.debug(
                            "Early drop: Place %s is %.1fm away, exceeding range limit (%.1fm).",
                            canonical_link,
                            dist,
                            range_limit,
                        )
                        continue

                def is_matching_preview(
                    resp: Any, target: str = canonical_link
                ) -> bool:
                    return is_preview_response_for_link(resp, target)

                click_succeeded = False

                # Human-like pre-click jitter delay
                await asyncio.sleep(random.uniform(0.3, 0.8))

                preview_timeout_ms = (
                    int(preview_timeout * 1000)
                    if preview_timeout < 1000
                    else int(preview_timeout)
                )

                try:
                    async with search_page.expect_response(
                        is_matching_preview,
                        timeout=preview_timeout_ms,
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
                        "  ⚠️ Error or timeout waiting for SPA preview (timeout=%dms): %s (%s)",
                        preview_timeout_ms,
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
            # Human-like post-scroll reading delay
            await asyncio.sleep(random.uniform(1.2, 2.2))

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

            # Stopping condition (consecutive_empty_scrolls):
            # When multiple consecutive scrolls yield no new items (MAX_CONSECUTIVE_EMPTY_SCROLLS = 6),
            # the feed has exhausted relevant results or all remaining items exceed the range limit,
            # avoiding infinite scrolling, resource waste, and bot detection.
            if not found_new_in_batch:
                consecutive_empty_scrolls += 1
                if consecutive_empty_scrolls >= MAX_CONSECUTIVE_EMPTY_SCROLLS:
                    logger.debug(
                        "Stopping SPA scroll: %d consecutive scrolls without new items.",
                        consecutive_empty_scrolls,
                    )
                    break
            else:
                consecutive_empty_scrolls = 0

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

    except Exception as e:  # noqa: BLE001
        logger.error("Error during scrape_query_spa: %s", e)
    finally:
        if search_page and not search_page.is_closed():
            try:
                await asyncio.shield(search_page.close())
            except BaseException:  # noqa: BLE001, S110
                pass
        for extra_ctx in created_contexts:
            try:
                await asyncio.shield(extra_ctx.close())
            except BaseException:  # noqa: BLE001, S110
                pass

    return results


async def process_link(
    context: BrowserContext,
    link: str,
    semaphore: asyncio.Semaphore,
    count: int,
    total: int,
    fields: Sequence[str] | set[str] | None = None,
    max_retries: int = 2,
    proxy_rotator: ProxyRotator | None = None,
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
                is_blocked = False
                try:
                    captcha_solved = await handle_captcha_if_present(
                        page, context_label="process_link"
                    )
                except RecaptchaBlockedError:
                    captcha_solved = False
                    is_blocked = True

                if (
                    (not captcha_solved and "sorry/index" in page.url) or is_blocked
                ) and attempt < max_retries:
                    if proxy_rotator is not None:
                        logger.warning(
                            "CAPTCHA blocked/unsolved in process_link, renewing proxy/circuit..."
                        )
                        proxy_rotator.renew()
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

            except Exception as e:  # noqa: BLE001
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
                        await asyncio.shield(page.close())
                    except BaseException:  # noqa: BLE001, S110
                        pass

        return None


def _get_flatten_column_type(col: str) -> pl.DataType | type[pl.DataType]:
    if col in ("latitude", "longitude", "rating"):
        return pl.Float64
    if col == "reviews_count":
        return pl.Int64
    if col == "categories":
        return pl.List(pl.String)
    return pl.String


def format_places_dataframe(
    results: list[dict[str, Any]],
    flatten: bool = False,
    fields: Sequence[str] | set[str] | None = None,
) -> pl.DataFrame:
    """Formats scraped places data into a Polars DataFrame.

    When flatten=False (default), keeps 11 default top-level columns
    ('name', 'place_id', 'latitude', 'longitude', 'address', 'link',
    'categories', 'rating', 'reviews_count', 'plus_code', 'city')
    and bundles all other metadata into a 'details' JSON string column.

    When flatten=True, outputs all fields as flattened columns at top-level.

    Args:
        results (list[dict[str, Any]]): List of scraped place dictionaries.
        flatten (bool, optional): Whether to flatten all fields into individual columns.
            Defaults to False.
        fields (Sequence[str] | set[str] | None, optional): Specific fields to include.
            Defaults to None.

    Returns:
        pl.DataFrame: Formatted Polars DataFrame.
    """
    if flatten:
        if not results:
            if fields is not None:
                schema = {f: pl.String for f in fields}
                return pl.DataFrame(schema=schema)
            return pl.DataFrame()
        if fields is not None:
            filtered_results = [
                {k: item[k] for k in fields if k in item} for item in results
            ]
            return pl.from_dicts(filtered_results, infer_schema_length=None)
        return pl.from_dicts(results, infer_schema_length=None)

    if fields is None:
        if not results:
            schema: dict[str, Any] = {
                c: _get_flatten_column_type(c) for c in DEFAULT_FLATTEN_COLUMNS
            }
            schema["details"] = pl.String
            return pl.DataFrame(schema=schema)

        formatted_rows: list[dict[str, Any]] = []
        for item in results:
            row: dict[str, Any] = {c: item.get(c) for c in DEFAULT_FLATTEN_COLUMNS}
            details_dict = {
                k: v
                for k, v in item.items()
                if k not in DEFAULT_FLATTEN_COLUMNS and v is not None
            }
            row["details"] = json.dumps(details_dict, ensure_ascii=False, default=str)
            formatted_rows.append(row)
        schema_overrides = {
            "latitude": pl.Float64,
            "longitude": pl.Float64,
            "rating": pl.Float64,
            "reviews_count": pl.Int64,
            "categories": pl.List(pl.String),
        }
        return pl.from_dicts(
            formatted_rows,
            infer_schema_length=None,
            schema_overrides=schema_overrides,
        )

    # fields is not None and flatten is False
    top_cols = [f for f in fields if f in DEFAULT_FLATTEN_COLUMNS]
    other_cols = [f for f in fields if f not in DEFAULT_FLATTEN_COLUMNS]
    if not results:
        schema_dict: dict[str, Any] = {c: _get_flatten_column_type(c) for c in top_cols}
        if other_cols:
            schema_dict["details"] = pl.String
        return pl.DataFrame(schema=schema_dict)

    formatted_rows = []
    for item in results:
        row = {c: item.get(c) for c in top_cols}
        if other_cols:
            details_dict = {
                k: item[k] for k in other_cols if k in item and item[k] is not None
            }
            row["details"] = json.dumps(details_dict, ensure_ascii=False, default=str)
        formatted_rows.append(row)

    overrides: dict[str, Any] = {
        c: _get_flatten_column_type(c)
        for c in top_cols
        if c in ("latitude", "longitude", "rating", "reviews_count", "categories")
    }

    return pl.from_dicts(
        formatted_rows,
        infer_schema_length=None,
        schema_overrides=overrides or None,
    )


async def scrape_google_maps(
    queries: set[str],
    geo_coordinates: Point,
    zoom: float,
    proxy: (
        ProxySettings
        | dict[str, Any]
        | Sequence[ProxySettings | dict[str, Any] | Any]
        | str
        | Sequence[str]
        | None
    ) = None,
    max_places: int = 120,
    lang: str = "en",
    headless: bool = True,
    n_semaphore: int = 8,
    fields: Sequence[str] | set[str] | None = None,
    flatten: bool = False,
    use_spa: bool = True,
    cache_dir: Path | None = DEFAULT_CACHE_DIR,
    range_limit: float = DEFAULT_RANGE_LIMIT,
    query_timeout: float = DEFAULT_QUERY_TIMEOUT,
    place_timeout: float = DEFAULT_PLACE_TIMEOUT,
    preview_timeout: float = DEFAULT_SPA_PREVIEW_TIMEOUT,
    stagger_delay: tuple[float, float] | float = (1.5, 3.5),
) -> pl.DataFrame:
    """Scrapes Google Maps for places based on queries.

    Args:
        queries (set[str]): Search queries (e.g. {"cafe", "restaurant"}).
        geo_coordinates (Point): Center coordinates for search.
        zoom (float): Map zoom level.
        proxy (ProxySettings | Sequence[ProxySettings] | str | Sequence[str] | None, optional): Single proxy
            or sequence of proxies for round-robin rotation. Defaults to None.
        max_places (int, optional): Maximum valid places to collect per query. Places
            dropped via range_limit (early drop) do not count toward this limit. Defaults to 120.
        lang (str, optional): Language code for Google Maps. Defaults to "en".
        headless (bool, optional): Whether to run headless browser. Defaults to False.
        n_semaphore (int, optional): Maximum concurrent browser tabs/queries. Defaults to 8.
        fields (Sequence[str] | set[str], optional): Selected fields to extract. Defaults to None (all fields).
        flatten (bool, optional): Whether to flatten all fields into individual columns.
            If False (default), bundles non-default fields into a 'details' JSON string column,
            keeping 11 common columns at top-level. Defaults to False.
        use_spa (bool, optional): Whether to use high-speed SPA navigation. Defaults to True.
        cache_dir (Path | None, optional): Directory to store persistent Chromium disk cache.
            Defaults to DEFAULT_CACHE_DIR (".cache/chromium_cache"). If None, disk caching
            flags will not be passed.
        range_limit (float): Maximum radius distance in meters from geo_coordinates.
            Google Maps local ranking combines Relevance, Distance, and Prominence
            (https://support.google.com/business/answer/7091). Prominent places further away
            may be returned before closer ones, so results are not strictly monotonic by distance.
            range_limit filters out places exceeding this radius (early drop).
            Defaults to DEFAULT_RANGE_LIMIT (10000.0m).
        query_timeout (float): Maximum seconds allowed per query before early return.
            Defaults to DEFAULT_QUERY_TIMEOUT (300.0s).
        place_timeout (float, optional): Maximum seconds allowed to scrape a place in fallback mode.
            Defaults to DEFAULT_PLACE_TIMEOUT (45.0s).
        preview_timeout (float | int, optional): Maximum timeout in ms (or seconds if < 1000)
            waiting for SPA place preview XHR response. Defaults to DEFAULT_SPA_PREVIEW_TIMEOUT.
        stagger_delay (tuple[float, float] | float, optional): Delay range (min, max) in
            seconds to stagger the initial launch of concurrent queries. Set to 0 to
            disable. Defaults to (1.5, 3.5).

    Returns:
        pl.DataFrame: DataFrame containing scraped places data.
    """
    results: list[dict[str, Any]] = []
    browser = None
    proxy_rotator = ProxyRotator(proxy)

    launch_args = list(LAUNCH_ARGS)
    if cache_dir is not None:
        resolved_cache = cache_dir.resolve()
        resolved_cache.mkdir(parents=True, exist_ok=True)
        launch_args.extend(
            [
                f"--disk-cache-dir={resolved_cache}",
                f"--disk-cache-size={DEFAULT_DISK_CACHE_SIZE}",
            ]
        )

    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(
                headless=headless,
                args=launch_args,
            )

            query_semaphore = asyncio.Semaphore(n_semaphore)

            if use_spa:
                logger.info(
                    "Scraping in SPA Navigation mode (max concurrency: %d, proxies: %d)...",
                    n_semaphore,
                    proxy_rotator.total,
                )

                async def run_spa_query(idx: int, q: str) -> list[dict[str, Any]]:
                    if stagger_delay is not None and idx > 0:
                        if isinstance(stagger_delay, (tuple, list)):
                            s_min = float(stagger_delay[0])
                            s_max = float(stagger_delay[1])
                            if s_max > 0:
                                delay = idx * random.uniform(s_min, s_max)
                                logger.info(
                                    "Staggered query %d ('%s') delay %.2fs...",
                                    idx,
                                    q,
                                    delay,
                                )
                                await asyncio.sleep(delay)
                        elif (
                            isinstance(stagger_delay, (int, float))
                            and stagger_delay > 0
                        ):
                            delay = idx * float(stagger_delay)
                            logger.info(
                                "Staggered query %d ('%s') delay %.2fs...",
                                idx,
                                q,
                                delay,
                            )
                            await asyncio.sleep(delay)

                    async with query_semaphore:
                        allocated_proxy = proxy_rotator.get()
                        context = await create_browser_context(
                            browser=browser,
                            geo_coordinates=geo_coordinates,
                            lang=lang,
                            proxy=allocated_proxy,
                        )
                        try:
                            coro = scrape_query_spa(
                                context=context,
                                query=q.replace("_", " "),
                                geo_coordinates=geo_coordinates,
                                zoom=zoom,
                                max_places=max_places,
                                lang=lang,
                                fields=fields,
                                range_limit=range_limit,
                                proxy_rotator=proxy_rotator,
                                query_timeout=query_timeout,
                                preview_timeout=preview_timeout,
                            )
                            return await asyncio.wait_for(
                                coro, timeout=query_timeout + 10.0
                            )
                        except TimeoutError:
                            logger.warning(
                                "🚨 Hard watchdog timeout for query '%s' after %.1fs",
                                q,
                                query_timeout + 10.0,
                            )
                            return []
                        except asyncio.CancelledError:
                            logger.debug("run_spa_query cancelled for query '%s'", q)
                            raise
                        except Exception as e:  # noqa: BLE001
                            logger.error(
                                "❌ Error in run_spa_query for query '%s': %s",
                                q,
                                e,
                            )
                            return []
                        finally:
                            if context:
                                try:
                                    await asyncio.shield(context.close())
                                except BaseException:  # noqa: BLE001, S110
                                    pass

                spa_tasks = [
                    asyncio.create_task(run_spa_query(i, query))
                    for i, query in enumerate(queries)
                ]
                try:
                    list_of_results = await asyncio.gather(
                        *spa_tasks, return_exceptions=True
                    )
                except asyncio.CancelledError:
                    for t in spa_tasks:
                        if not t.done():
                            t.cancel()
                    await asyncio.gather(*spa_tasks, return_exceptions=True)
                    raise
                valid_results: list[list[dict[str, Any]]] = []
                for res in list_of_results:
                    if isinstance(res, Exception):
                        logger.error(
                            "Unexpected exception gathered in SPA query: %s",
                            res,
                        )
                    elif isinstance(res, list):
                        valid_results.append(res)
                results = list(itertools.chain.from_iterable(valid_results))
                logger.info(
                    "✅ Successfully collected %d places via SPA Navigation.",
                    len(results),
                )
            else:
                # Multi-page fallback mode
                async def run_get_urls(idx: int, q: str) -> set[str]:
                    if stagger_delay is not None and idx > 0:
                        if isinstance(stagger_delay, (tuple, list)):
                            s_min, s_max = (
                                float(stagger_delay[0]),
                                float(stagger_delay[1]),
                            )
                            if s_max > 0:
                                delay = idx * random.uniform(s_min, s_max)
                                logger.info(
                                    "Staggered startup: query %d ('%s') sleeping for %.2fs before launch...",
                                    idx,
                                    q,
                                    delay,
                                )
                                await asyncio.sleep(delay)
                        elif (
                            isinstance(stagger_delay, (int, float))
                            and stagger_delay > 0
                        ):
                            delay = idx * float(stagger_delay)
                            logger.info(
                                "Staggered startup: query %d ('%s') sleeping for %.2fs before launch...",
                                idx,
                                q,
                                delay,
                            )
                            await asyncio.sleep(delay)

                    async with query_semaphore:
                        allocated_proxy = proxy_rotator.get()
                        context = await create_browser_context(
                            browser=browser,
                            geo_coordinates=geo_coordinates,
                            lang=lang,
                            proxy=allocated_proxy,
                        )
                        try:
                            coro = get_place_urls(
                                context=context,
                                max_places=max_places,
                                query=q.replace("_", " "),
                                geo_coordinates=geo_coordinates,
                                zoom=zoom,
                                lang=lang,
                                range_limit=range_limit,
                                proxy_rotator=proxy_rotator,
                                query_timeout=query_timeout,
                            )
                            return await asyncio.wait_for(
                                coro, timeout=query_timeout + 10.0
                            )
                        except TimeoutError:
                            logger.warning(
                                "🚨 Hard watchdog timeout for get_place_urls on query '%s'",
                                q,
                            )
                            return set()
                        except asyncio.CancelledError:
                            logger.debug("run_get_urls cancelled for query '%s'", q)
                            raise
                        except Exception as e:  # noqa: BLE001
                            logger.error(
                                "❌ Error in get_place_urls for query '%s': %s",
                                q,
                                e,
                            )
                            return set()
                        finally:
                            if context:
                                try:
                                    await asyncio.shield(context.close())
                                except BaseException:  # noqa: BLE001, S110
                                    pass

                tasks = [
                    asyncio.create_task(run_get_urls(i, query))
                    for i, query in enumerate(queries)
                ]
                try:
                    list_of_sets_of_links = await asyncio.gather(
                        *tasks, return_exceptions=True
                    )
                except asyncio.CancelledError:
                    for t in tasks:
                        if not t.done():
                            t.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    raise
                valid_link_sets: list[set[str]] = []
                for r in list_of_sets_of_links:
                    if isinstance(r, Exception):
                        logger.error(
                            "Unexpected exception gathered in get_place_urls: %s",
                            r,
                        )
                    elif isinstance(r, set):
                        valid_link_sets.append(r)
                place_links = list(set(itertools.chain.from_iterable(valid_link_sets)))
                logger.info("Collected %d unique place URLs.", len(place_links))

                logger.info(
                    "Scraping details for %d places (concurrency: %d)...",
                    len(place_links),
                    n_semaphore,
                )
                total = len(place_links)
                detail_semaphore = asyncio.Semaphore(n_semaphore)

                async def run_process_link(
                    idx: int, link: str
                ) -> dict[str, Any] | None:
                    async with detail_semaphore:
                        allocated_proxy = proxy_rotator.get()
                        context = await create_browser_context(
                            browser=browser,
                            geo_coordinates=geo_coordinates,
                            lang=lang,
                            proxy=allocated_proxy,
                        )
                        try:
                            coro = process_link(
                                context,
                                link,
                                asyncio.Semaphore(1),
                                idx + 1,
                                total,
                                fields=fields,
                                proxy_rotator=proxy_rotator,
                            )
                            return await asyncio.wait_for(coro, timeout=place_timeout)
                        except TimeoutError:
                            logger.warning(
                                "🚨 Timeout processing place link after %.1fs: %s",
                                place_timeout,
                                link,
                            )
                            return None
                        except asyncio.CancelledError:
                            logger.debug(
                                "run_process_link cancelled for link '%s'", link
                            )
                            raise
                        except Exception as e:  # noqa: BLE001
                            logger.error(
                                "❌ Error processing place link %s: %s",
                                link,
                                e,
                            )
                            return None
                        finally:
                            if context:
                                try:
                                    await asyncio.shield(context.close())
                                except BaseException:  # noqa: BLE001, S110
                                    pass

                detail_tasks = [
                    asyncio.create_task(run_process_link(i, link))
                    for i, link in enumerate(place_links)
                ]
                try:
                    raw_results = await asyncio.gather(
                        *detail_tasks, return_exceptions=True
                    )
                except asyncio.CancelledError:
                    for t in detail_tasks:
                        if not t.done():
                            t.cancel()
                    await asyncio.gather(*detail_tasks, return_exceptions=True)
                    raise
                results = [r for r in raw_results if isinstance(r, dict)]
                logger.info("✅ Successfully collected %d places.", len(results))

        except asyncio.CancelledError:
            logger.info("Scraping cancelled by user/task cancellation.")
            raise
        except PlaywrightTimeoutError:
            logger.error("Playwright timeout error during scraping process.")
        except Exception as e:  # noqa: BLE001
            logger.error("Unexpected error during scraping: %s", e)
        finally:
            if browser and browser.is_connected():
                try:
                    await asyncio.shield(browser.close())
                except BaseException:  # noqa: BLE001, S110
                    pass

    return format_places_dataframe(results, flatten=flatten, fields=fields)
