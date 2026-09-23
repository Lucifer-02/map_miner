import asyncio
import hashlib
import itertools
import logging
import random
import re
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import polars as pl
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

from .extractor import (
    DEFAULT_FLATTEN_COLUMNS,
    REQUIRED_COLUMNS,
    calculate_distance,
    extract_coordinates_from_url,
    extract_feed_item_dom,
    extract_place_data,
    format_places_dataframe,
    is_preview_response_for_link,
    is_within_range,
    make_place_url,
    parse_preview_json,
)
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
    "DEFAULT_DISK_CACHE_SIZE",
    "DEFAULT_FLATTEN_COLUMNS",
    "DEFAULT_MAX_CAPTCHA_RETRIES",
    "DEFAULT_PLACE_TIMEOUT",
    "DEFAULT_PROXY_BYPASS",
    "DEFAULT_QUERY_TIMEOUT",
    "DEFAULT_RANGE_LIMIT",
    "DEFAULT_SPA_PREVIEW_TIMEOUT",
    "DEFAULT_STAGGER_DELAY",
    "DEFAULT_STATIC_CACHE_DIR",
    "DEFAULT_TIMEOUT",
    "MAX_CONSECUTIVE_EMPTY_SCROLLS",
    "MAX_CONSECUTIVE_OUT_OF_RANGE_SCROLLS",
    "MAX_SCROLL_ATTEMPTS_WITHOUT_NEW_LINKS",
    "REQUIRED_COLUMNS",
    "ProxyRotator",
    "calculate_distance",
    "create_browser_context",
    "extract_coordinates_from_url",
    "extract_feed_item_dom",
    "find_feed_selector",
    "format_places_dataframe",
    "get_place_urls",
    "get_tor_rotating_proxy",
    "global_route_handler",
    "handle_captcha_if_present",
    "is_feed_at_end",
    "is_no_results_page",
    "is_preview_response_for_link",
    "is_within_range",
    "make_place_url",
    "pass_consent",
    "process_link",
    "renew_tor_circuit_control",
    "safe_close_browser",
    "safe_close_context",
    "safe_close_page",
    "scrape_google_maps",
    "scrape_query_spa",
    "scroll_feed",
]

# --- Constants ---
DEFAULT_TIMEOUT: int = 30000  # ms (30s)
DEFAULT_QUERY_TIMEOUT: float = 300.0  # seconds (5 min)
DEFAULT_PLACE_TIMEOUT: float = 45.0  # seconds
DEFAULT_CAPTCHA_TIMEOUT: float = 85.0  # seconds
DEFAULT_SPA_PREVIEW_TIMEOUT: float | int = 10000  # ms (10s)
DEFAULT_RANGE_LIMIT: float = 10000.0  # meters (10 km)
DEFAULT_MAX_CAPTCHA_RETRIES: int = 2
DEFAULT_STAGGER_DELAY: tuple[float, float] | float = (1.5, 3.5)
DEFAULT_CACHE_DIR: Path | None = Path(".cache") / "chromium_cache"
DEFAULT_STATIC_CACHE_DIR: Path = Path(".cache") / "static_assets"
DEFAULT_DISK_CACHE_SIZE: int = 1073741824  # 1 GB
MAX_CONSECUTIVE_EMPTY_SCROLLS: int = 4
MAX_CONSECUTIVE_OUT_OF_RANGE_SCROLLS: int = 3
MAX_SCROLL_ATTEMPTS_WITHOUT_NEW_LINKS: int = 5

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


_active_route_tasks: set[asyncio.Task[Any]] = set()


async def global_route_handler(route: Route) -> None:
    """Context-wide route handler to block heavy resources and tracking,
    saving significant network bandwidth while caching static assets
    and preserving reCAPTCHA and core APIs.
    """
    curr_task = asyncio.current_task()
    if curr_task is not None:
        _active_route_tasks.add(curr_task)
        curr_task.add_done_callback(_active_route_tasks.discard)

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
    except (PlaywrightError, asyncio.CancelledError):
        # Do not attempt route.continue_() when context/page is closed or task is cancelled
        return


async def safe_close_page(page: Page | None) -> None:
    """Safely closes a Playwright Page, ensuring proper cleanup even under task cancellation."""
    if not page:
        return
    try:
        if hasattr(page, "is_closed") and page.is_closed():
            return
        close_task = asyncio.create_task(page.close())
        try:
            await asyncio.shield(close_task)
        except asyncio.CancelledError:
            try:
                await close_task
            except BaseException:  # noqa: BLE001, S110
                pass
    except BaseException:  # noqa: BLE001, S110
        pass


async def safe_close_context(context: BrowserContext | None) -> None:
    """Safely closes a Playwright BrowserContext, unrouting handlers and ensuring
    proper cleanup without dangling pending tasks under cancellation."""
    if not context:
        return
    try:
        try:
            if hasattr(context, "unroute"):
                await context.unroute("**/*")
        except BaseException:  # noqa: BLE001, S110
            pass
        close_task = asyncio.create_task(context.close())
        try:
            await asyncio.shield(close_task)
        except asyncio.CancelledError:
            try:
                await close_task
            except BaseException:  # noqa: BLE001, S110
                pass
    except BaseException:  # noqa: BLE001, S110
        pass


async def safe_close_browser(browser: Browser | None) -> None:
    """Safely closes a Playwright Browser instance without leaving dangling tasks."""
    if not browser:
        return
    try:
        if hasattr(browser, "is_connected") and not browser.is_connected():
            return
        close_task = asyncio.create_task(browser.close())
        try:
            await asyncio.shield(close_task)
        except asyncio.CancelledError:
            try:
                await close_task
            except BaseException:  # noqa: BLE001, S110
                pass
    except BaseException:  # noqa: BLE001, S110
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
    links_collector: set[str] | None = None,
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
        links_collector (set[str] | None, optional): Mutable set to collect links in-place for zero data loss on timeout.

    Returns:
        set[str]: Collected place URLs.
    """
    search_page = await context.new_page()
    if not search_page:
        raise RuntimeError("Failed to create search browser page.")

    place_links: set[str] = links_collector if links_collector is not None else set()

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
                search_page,
                context_label="search_page",
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
                dist = calculate_distance(geo_coordinates, coords)
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
                    dist = calculate_distance(geo_coordinates, coords)
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
        consecutive_out_of_range_scrolls = 0
        processed_links: set[str] = set()

        while True:
            elapsed = time.monotonic() - query_start
            remaining_time = query_timeout - elapsed
            if remaining_time <= 2.0:
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
            batch_had_out_of_range = False

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
                    dist = calculate_distance(geo_coordinates, coords)
                    if dist > range_limit:
                        processed_links.add(canonical_link)
                        batch_had_out_of_range = True
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
                    if len(place_links) > max_places:
                        excess = list(place_links)[max_places:]
                        for item in excess:
                            place_links.discard(item)
                    break

            if max_places is not None and len(place_links) >= max_places:
                break

            logger.debug("Found %d unique place links so far...", len(place_links))

            # Stopping condition (consecutive_empty_scrolls):
            if new_links_count == 0:
                consecutive_empty_scrolls += 1
                if batch_had_out_of_range:
                    consecutive_out_of_range_scrolls += 1
                    if (
                        consecutive_out_of_range_scrolls
                        >= MAX_CONSECUTIVE_OUT_OF_RANGE_SCROLLS
                    ):
                        logger.debug(
                            "Stopping scroll in get_place_urls: %d consecutive scrolls with only out-of-range links.",
                            consecutive_out_of_range_scrolls,
                        )
                        break
                else:
                    consecutive_out_of_range_scrolls = 0

                if consecutive_empty_scrolls >= MAX_CONSECUTIVE_EMPTY_SCROLLS:
                    logger.debug(
                        "Stopping scroll in get_place_urls: %d consecutive scrolls without new links.",
                        consecutive_empty_scrolls,
                    )
                    break
            else:
                consecutive_empty_scrolls = 0
                consecutive_out_of_range_scrolls = 0

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
        await safe_close_page(search_page)

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
    max_captcha_retries: int = DEFAULT_MAX_CAPTCHA_RETRIES,
    query_timeout: float = DEFAULT_QUERY_TIMEOUT,
    preview_timeout: float = DEFAULT_SPA_PREVIEW_TIMEOUT,
    results_collector: list[dict[str, Any]] | None = None,
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
        results_collector (list[dict[str, Any]] | None, optional): Mutable list to collect
            places in-place for zero data loss on timeout.

    Returns:
        list[dict[str, Any]]: List of place dictionaries.
    """
    active_context = context
    created_contexts: list[BrowserContext] = []
    results: list[dict[str, Any]] = (
        results_collector if results_collector is not None else []
    )
    processed_links: set[str] = set()
    search_page: Page | None = None
    fallback_rescue_links: list[str] = []

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
                    search_page,
                    context_label="spa_search",
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
                    await safe_close_page(search_page)
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
                dist = calculate_distance(geo_coordinates, coords)
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
        consecutive_out_of_range_scrolls = 0
        last_height = await search_page.evaluate(
            "(sel) => document.querySelector(sel)?.scrollHeight || 0",
            active_feed_selector,
        )

        while max_places is None or len(results) < max_places:
            elapsed = time.monotonic() - query_start
            remaining_time = query_timeout - elapsed
            if remaining_time <= 2.0:
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
            batch_had_out_of_range = False

            for el in link_elements:
                if max_places is not None and len(results) >= max_places:
                    break

                elapsed = time.monotonic() - query_start
                remaining_time = query_timeout - elapsed
                if remaining_time <= 2.0:
                    logger.warning(
                        "⚠️ Query '%s' approaching timeout limit (%.1fs remaining). Returning %d collected places.",
                        query,
                        remaining_time,
                        len(results),
                    )
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
                    dist = calculate_distance(geo_coordinates, coords)
                    if dist > range_limit:
                        processed_links.add(canonical_link)
                        batch_had_out_of_range = True
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

                # Human-like pre-click jitter delay
                await asyncio.sleep(random.uniform(0.3, 0.8))

                preview_timeout_ms = (
                    int(preview_timeout * 1000)
                    if preview_timeout < 1000
                    else int(preview_timeout)
                )
                cur_timeout_ms = min(
                    preview_timeout_ms,
                    max(1000, int((remaining_time - 0.5) * 1000)),
                )

                # Ensure element is scrolled into view before clicking
                try:
                    await el.scroll_into_view_if_needed(timeout=1000)
                except PlaywrightError:
                    pass

                preview_json: str | None = None
                try:
                    async with search_page.expect_response(
                        is_matching_preview,
                        timeout=cur_timeout_ms,
                    ) as response_info:
                        try:
                            await el.evaluate("e => e.click()")
                        except PlaywrightError:
                            await el.scroll_into_view_if_needed(timeout=1000)
                            await el.click(force=True, timeout=1000)

                    response = await response_info.value
                    preview_json = await response.text()
                except (PlaywrightTimeoutError, PlaywrightError) as e:
                    logger.warning(
                        "  ⚠️ Error or timeout waiting for SPA preview (timeout=%dms): %s (%s)",
                        cur_timeout_ms,
                        canonical_link,
                        e,
                    )

                preview_blob = (
                    parse_preview_json(preview_json) if preview_json else None
                )

                if preview_blob is not None:
                    # Add to processed_links after successful preview parsing
                    processed_links.add(canonical_link)
                    place_data = extract_place_data(
                        html_content=None,
                        preview_blob=preview_blob,
                        preview_json=preview_json,
                        fields=fields,
                    )

                    # Post-extraction radius filtering if coords were not in the URL
                    if coords is None and place_data:
                        p_lat = place_data.get("latitude")
                        p_lng = place_data.get("longitude")
                        if p_lat is not None and p_lng is not None:
                            try:
                                post_dist = calculate_distance(
                                    geo_coordinates,
                                    (float(p_lat), float(p_lng)),
                                )
                                if post_dist > range_limit:
                                    batch_had_out_of_range = True
                                    logger.debug(
                                        "Post-extraction drop: Place %s is %.1fm away, exceeding range limit (%.1fm).",
                                        canonical_link,
                                        post_dist,
                                        range_limit,
                                    )
                                    continue
                            except (ValueError, TypeError):
                                pass

                    if place_data and (
                        fields is not None
                        or "name" in place_data
                        or len(place_data) >= 3
                    ):
                        if fields is None or "link" in fields:
                            place_data["link"] = link
                        results.append(place_data)
                        found_new_in_batch = True
                        logger.info(
                            "  ✅ [SPA %d/%s] Extracted: %s",
                            len(results),
                            str(max_places),
                            place_data.get("name", canonical_link),
                        )
                else:
                    # 🆘 Fallback Rescue via feed card DOM
                    card_html = ""
                    try:
                        card_html = await el.evaluate(
                            "e => e.closest('div.Nv2PK, div[role=\"article\"], div.THOPZb, div.fontBodyMedium')?.outerHTML || e.parentElement?.parentElement?.outerHTML || e.outerHTML"
                        )
                    except PlaywrightError:
                        pass

                    rescued_place = (
                        extract_feed_item_dom(
                            card_html, link=link, coords=coords, fields=fields
                        )
                        if card_html
                        else {}
                    )

                    if rescued_place and rescued_place.get("name"):
                        r_coords = coords
                        if (
                            r_coords is None
                            and rescued_place.get("latitude") is not None
                            and rescued_place.get("longitude") is not None
                        ):
                            try:
                                r_coords = (
                                    float(rescued_place["latitude"]),
                                    float(rescued_place["longitude"]),
                                )
                            except (ValueError, TypeError):
                                r_coords = None

                        is_in_range = True
                        if r_coords is not None:
                            post_dist = calculate_distance(geo_coordinates, r_coords)
                            if post_dist > range_limit:
                                is_in_range = False
                                processed_links.add(canonical_link)
                                batch_had_out_of_range = True
                                logger.debug(
                                    "Rescued place out of range: %s (%.1fm > %.1fm)",
                                    canonical_link,
                                    post_dist,
                                    range_limit,
                                )

                        if is_in_range:
                            if fields is None or "link" in fields:
                                rescued_place["link"] = link
                            results.append(rescued_place)
                            found_new_in_batch = True
                            processed_links.add(canonical_link)
                            logger.info(
                                "  🆘 [Fallback Rescue] Rescued place data from feed DOM for: %s",
                                rescued_place.get("name"),
                            )
                    else:
                        logger.warning(
                            "  ⚠️ Could not rescue place data from feed DOM for: %s",
                            canonical_link,
                        )
                        fallback_rescue_links.append(link)

                await asyncio.sleep(random.uniform(0.15, 0.35))

            if max_places is not None and len(results) >= max_places:
                break

            if (query_timeout - (time.monotonic() - query_start)) <= 2.0:
                logger.warning(
                    "⚠️ Query '%s' reached timeout limit (%.1fs). Returning %d collected places.",
                    query,
                    query_timeout,
                    len(results),
                )
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

            # Stopping condition (consecutive_empty_scrolls & out of range scrolls):
            if not found_new_in_batch:
                consecutive_empty_scrolls += 1
                if batch_had_out_of_range:
                    consecutive_out_of_range_scrolls += 1
                    if (
                        consecutive_out_of_range_scrolls
                        >= MAX_CONSECUTIVE_OUT_OF_RANGE_SCROLLS
                    ):
                        logger.debug(
                            "Stopping SPA scroll: %d consecutive scrolls with only out-of-range items.",
                            consecutive_out_of_range_scrolls,
                        )
                        break
                else:
                    consecutive_out_of_range_scrolls = 0

                if consecutive_empty_scrolls >= MAX_CONSECUTIVE_EMPTY_SCROLLS:
                    logger.debug(
                        "Stopping SPA scroll: %d consecutive scrolls without new items.",
                        consecutive_empty_scrolls,
                    )
                    break
            else:
                consecutive_empty_scrolls = 0
                consecutive_out_of_range_scrolls = 0

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

        # Secondary fallback rescue for unresolved links via process_link if time permits
        if fallback_rescue_links and (max_places is None or len(results) < max_places):
            remaining_time = query_timeout - (time.monotonic() - query_start)
            if remaining_time > 5.0:
                logger.info(
                    "Attempting secondary fallback rescue for %d unresolved links...",
                    len(fallback_rescue_links),
                )
                rescue_sem = asyncio.Semaphore(min(4, len(fallback_rescue_links)))
                for f_link in fallback_rescue_links:
                    f_canon = f_link.split("?")[0]
                    if f_canon in processed_links:
                        continue
                    if max_places is not None and len(results) >= max_places:
                        break
                    if (query_timeout - (time.monotonic() - query_start)) <= 3.0:
                        break
                    try:
                        f_data = await process_link(
                            context=context,
                            link=f_link,
                            count=len(results) + 1,
                            total=max_places
                            or (len(results) + len(fallback_rescue_links)),
                            semaphore=rescue_sem,
                            fields=fields,
                            max_retries=1,
                        )
                        if f_data and (
                            fields is not None or "name" in f_data or len(f_data) >= 3
                        ):
                            processed_links.add(f_canon)
                            results.append(f_data)
                            logger.info(
                                "  🆘 [Secondary Rescue] Rescued via process_link: %s",
                                f_data.get("name", f_link),
                            )
                    except Exception as rescue_err:  # noqa: BLE001
                        logger.debug(
                            "Secondary rescue error for %s: %s",
                            f_link,
                            rescue_err,
                        )

    except Exception as e:  # noqa: BLE001
        logger.error("Error during scrape_query_spa: %s", e)
    finally:
        await safe_close_page(search_page)
        for extra_ctx in created_contexts:
            await safe_close_context(extra_ctx)

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
                await safe_close_page(page)

        return None


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
    stagger_delay: tuple[float, float] | float = DEFAULT_STAGGER_DELAY,
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
                        collector: list[dict[str, Any]] = []
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
                                results_collector=collector,
                            )
                            return await asyncio.wait_for(
                                coro, timeout=query_timeout + 10.0
                            )
                        except TimeoutError:
                            logger.warning(
                                "🚨 Hard watchdog timeout for query '%s' after %.1fs. Returning %d rescued places.",
                                q,
                                query_timeout + 10.0,
                                len(collector),
                            )
                            return collector
                        except asyncio.CancelledError:
                            logger.debug("run_spa_query cancelled for query '%s'", q)
                            raise
                        except Exception as e:  # noqa: BLE001
                            logger.error(
                                "❌ Error in run_spa_query for query '%s': %s",
                                q,
                                e,
                            )
                            return collector
                        finally:
                            await safe_close_context(context)

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
                        links_collector: set[str] = set()
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
                                links_collector=links_collector,
                            )
                            return await asyncio.wait_for(
                                coro, timeout=query_timeout + 10.0
                            )
                        except TimeoutError:
                            logger.warning(
                                "🚨 Hard watchdog timeout for get_place_urls on query '%s' after %.1fs. Returning %d rescued links.",
                                q,
                                query_timeout + 10.0,
                                len(links_collector),
                            )
                            return links_collector
                        except asyncio.CancelledError:
                            logger.debug("run_get_urls cancelled for query '%s'", q)
                            raise
                        except Exception as e:  # noqa: BLE001
                            logger.error(
                                "❌ Error in get_place_urls for query '%s': %s",
                                q,
                                e,
                            )
                            return links_collector
                        finally:
                            await safe_close_context(context)

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
                            await safe_close_context(context)

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
            await safe_close_browser(browser)

    return format_places_dataframe(results, flatten=flatten, fields=fields)
