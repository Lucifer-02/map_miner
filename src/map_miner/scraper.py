import asyncio
import hashlib
import logging
import os
import random
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import polars as pl
from geopy.point import Point
from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
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
    DEFAULT_RANGE_LIMIT,
    calculate_distance,
    compare_place_url_ids,
    extract_coordinates_from_url,
    extract_feed_item_dom,
    extract_place_coordinates_from_url,
    extract_place_data,
    extract_place_identity_from_url,
    format_places_dataframe,
    is_preview_response_for_link,
    make_place_url,
    parse_preview_json,
    preview_blob_matches_link,
)
from .proxy import (
    ProxyRotator,
    _ProxyInput,
)
from .recaptcha_solver import (
    RecaptchaBlockedError,
    RecaptchaError,
    RecaptchaSolver,
)

logger = logging.getLogger(__name__)

__all__ = [
    "scrape_google_maps",
]

# --- Route & Browser Constants ---

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


_CONTEXT_CLEANUP_TIMEOUT = 5.0


async def _global_route_handler(
    route: Route,
    static_cache_dir: Path = Path(".cache") / "static_assets",
) -> None:
    """Context-wide route handler to block heavy resources and tracking,
    saving significant network bandwidth while caching static assets
    and preserving reCAPTCHA and core APIs.

    Args:
        route (Route): Playwright route object.
        static_cache_dir (Path, optional): Directory to store static assets cache.
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
            cache_file = static_cache_dir / cache_key

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

            try:
                response = await route.fetch()
                if response.status == 200:
                    body = await response.body()
                    try:
                        static_cache_dir.mkdir(parents=True, exist_ok=True)
                        cache_file.write_bytes(body)
                    except OSError:
                        pass
            except asyncio.CancelledError:
                # Resolve the request without retrying network work on cancellation.
                await route.abort()
                return
            except PlaywrightError as error:
                # fetch/body errors leave the intercepted browser request unresolved.
                # Let the browser retry it directly rather than stalling its route.
                logger.warning(
                    "Static asset fetch failed; continuing request: %s", error
                )
                await route.continue_()
                return
            await route.fulfill(response=response)
            return

        await route.continue_()
    except (PlaywrightError, asyncio.CancelledError):
        # Do not attempt route.continue_() when context/page is closed or task is cancelled
        return


async def _safe_close_page(
    page: Page | None, timeout: float = _CONTEXT_CLEANUP_TIMEOUT
) -> None:
    """Safely closes a Playwright Page, ensuring proper cleanup even under task cancellation
    and avoiding deadlocks with a bounded timeout."""
    if not page:
        return
    try:
        if hasattr(page, "is_closed") and page.is_closed():
            return
        close_task = asyncio.create_task(page.close())
        try:
            await asyncio.wait_for(asyncio.shield(close_task), timeout=timeout)
        except TimeoutError:
            logger.warning("Page close timed out after %s seconds", timeout)
            close_task.cancel()
            await asyncio.gather(close_task, return_exceptions=True)
        except asyncio.CancelledError:
            try:
                try:
                    await asyncio.wait_for(asyncio.shield(close_task), timeout=timeout)
                except TimeoutError:
                    logger.warning(
                        "Page close timed out after %s seconds on cancellation", timeout
                    )
            finally:
                if not close_task.done():
                    close_task.cancel()
                await asyncio.gather(close_task, return_exceptions=True)
    except BaseException:  # noqa: BLE001, S110
        pass


async def _safe_close_context(context: BrowserContext | None) -> None:
    """Safely closes a Playwright BrowserContext, unrouting handlers and ensuring
    proper cleanup without dangling pending tasks under cancellation."""
    if not context:
        return

    async def cleanup() -> None:
        drain_task = asyncio.create_task(context.unroute_all(behavior="wait"))
        try:
            await asyncio.wait_for(
                asyncio.shield(drain_task), timeout=_CONTEXT_CLEANUP_TIMEOUT
            )
        except (TimeoutError, PlaywrightError) as error:
            logger.debug("Route drain interrupted before context close: %s", error)
        finally:
            try:
                await asyncio.wait_for(
                    context.close(), timeout=_CONTEXT_CLEANUP_TIMEOUT
                )
            except (TimeoutError, PlaywrightError) as error:
                logger.debug("Context close failed: %s", error)
            try:
                await asyncio.wait_for(
                    asyncio.shield(drain_task), timeout=_CONTEXT_CLEANUP_TIMEOUT
                )
            except (TimeoutError, PlaywrightError):
                drain_task.cancel()
                await asyncio.gather(drain_task, return_exceptions=True)

    cleanup_task = asyncio.create_task(cleanup())
    try:
        await asyncio.shield(cleanup_task)
    except asyncio.CancelledError:
        # Caller cancellation must not abandon this context's in-flight handlers.
        try:
            await cleanup_task
        finally:
            raise
    except BaseException:  # noqa: BLE001, S110
        pass


async def _safe_close_browser(
    browser: Browser | None, timeout: float = _CONTEXT_CLEANUP_TIMEOUT
) -> None:
    """Safely closes a Playwright Browser instance without leaving dangling tasks
    and avoiding deadlocks with a bounded timeout."""
    if not browser:
        return
    try:
        if hasattr(browser, "is_connected") and not browser.is_connected():
            return
        close_task = asyncio.create_task(browser.close())
        try:
            await asyncio.wait_for(asyncio.shield(close_task), timeout=timeout)
        except TimeoutError:
            logger.warning("Browser close timed out after %s seconds", timeout)
            close_task.cancel()
            await asyncio.gather(close_task, return_exceptions=True)
        except asyncio.CancelledError:
            try:
                try:
                    await asyncio.wait_for(asyncio.shield(close_task), timeout=timeout)
                except TimeoutError:
                    logger.warning(
                        "Browser close timed out after %s seconds on cancellation",
                        timeout,
                    )
            finally:
                if not close_task.done():
                    close_task.cancel()
                await asyncio.gather(close_task, return_exceptions=True)
    except BaseException:  # noqa: BLE001, S110
        pass


async def _create_browser_context(
    browser: Browser,
    geo_coordinates: Point,
    lang: str = "en",
    proxy: _ProxyInput = None,
    static_cache_dir: Path = Path(".cache") / "static_assets",
    viewport_width: int = 1920,
    viewport_height: int = 1080,
    viewport_variance: int = 50,
    timezone_id: str | None = "UTC",
) -> BrowserContext:
    """Creates and configures an isolated BrowserContext with proxy settings,
    stealth overrides, geolocation, and global resource blocking.

    Args:
        browser (Browser): Playwright browser instance.
        geo_coordinates (Point): Geographic center coordinates for geolocation mocking.
        lang (str, optional): Language code.
        proxy (ProxySettings | Sequence[ProxySettings] | str | Sequence[str] | None, optional):
            Proxy settings for this context.
        static_cache_dir (Path, optional): Directory to store static assets cache.
        viewport_width (int, optional): Base viewport width.
        viewport_height (int, optional): Base viewport height.
        viewport_variance (int, optional): Random pixel variance added to viewport dimensions.
        timezone_id (str | None, optional): IANA timezone ID for browser context. Defaults to "UTC".

    Returns:
        BrowserContext: Configured isolated browser context.
    """
    accept_lang = f"{lang}-{lang.upper()},{lang};q=0.9,en-US;q=0.8,en;q=0.7"
    resolved_timezone = timezone_id or "UTC"
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
            "width": viewport_width
            + random.randint(-viewport_variance, viewport_variance),
            "height": viewport_height
            + random.randint(-viewport_variance, viewport_variance),
        },
        "permissions": ["geolocation"],
        "timezone_id": resolved_timezone,
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
    async def route_handler(route: Route) -> None:
        await _global_route_handler(route, static_cache_dir=static_cache_dir)

    await context.route("**/*", route_handler)

    return context


async def _pass_consent(page: Page, dismiss_delay: float = 1.0) -> bool:
    """Attempts to dismiss Google's cookie/privacy consent banner
    across multiple languages safely.

    Args:
        page (Page): Target browser page.
        dismiss_delay (float, optional): Delay in seconds after clicking consent button.

    Returns:
        bool: True if consent banner was dismissed, False otherwise.
    """
    logger.debug("Checking for consent dialog...")
    try:
        button = page.get_by_role("button", name=CONSENT_BUTTON_REGEX)
        if await button.count() > 0 and await button.first.is_visible():
            await button.first.click()
            await asyncio.sleep(dismiss_delay)
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
                await asyncio.sleep(dismiss_delay)
                logger.debug("Consent dismissed via form button.")
                return True
    except PlaywrightError as e:
        logger.debug("Consent form check error: %s", e)

    return False


async def _handle_captcha_if_present(
    page: Page,
    context_label: str = "",
    timeout: float = 85.0,
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
        safe_timeout = max(1.0, float(timeout))
        try:
            return await asyncio.wait_for(solver.solve_captcha(), timeout=safe_timeout)
        except TimeoutError:
            logger.warning(
                "🚨 reCAPTCHA solving timed out after %.1fs%s", safe_timeout, tag
            )
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


async def _find_feed_selector(page: Page, timeout: int = 15000) -> str | None:
    """Locates the results feed container selector on a Google Maps search page.

    Args:
        page (Page): Target browser page.
        timeout (int, optional): Timeout in ms to wait for the feed container.

    Returns:
        str | None: Active feed selector if found, None otherwise.
    """
    try:
        await page.wait_for_selector(
            '[role="feed"]', state="visible", timeout=max(1, timeout)
        )
        return '[role="feed"]'
    except PlaywrightTimeoutError:
        for selector in FEED_FALLBACK_SELECTORS[1:]:
            if await page.locator(selector).count() > 0:
                return selector
    return None


async def _scroll_feed(
    page: Page,
    feed_selector: str,
    scroll_delta_y: int = 5000,
    scroll_delay_range: tuple[float, float] = (1.0, 1.6),
) -> None:
    """Scrolls down the search results feed container.

    Args:
        page (Page): Target browser page.
        feed_selector (str): CSS or XPath selector of feed container.
        scroll_delta_y (int, optional): Vertical scroll delta in pixels.
        scroll_delay_range (tuple[float, float], optional): Range of random sleep delay
            after scrolling.
    """
    try:
        feed_locator = page.locator(feed_selector).first
        await feed_locator.hover()
        await page.mouse.wheel(0, scroll_delta_y)
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

    await asyncio.sleep(random.uniform(scroll_delay_range[0], scroll_delay_range[1]))


async def _is_feed_at_end(page: Page) -> bool:
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


async def _is_no_results_page(page: Page) -> bool:
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


class _PreviewInterceptor:
    """
    Listens for /maps/preview/place XHR responses on a Page,
    capturing the rich place detail payload.
    """

    def __init__(self, page: Page, target_link: str | None = None) -> None:
        self.preview_json: str | None = None
        self.preview_event = asyncio.Event()
        self._target_link = target_link
        page.on("response", self._handle_response)

    async def _handle_response(self, response: Any) -> None:
        if self.preview_event.is_set() or "maps/preview/place" not in response.url:
            return
        if self._target_link and not is_preview_response_for_link(
            response, self._target_link
        ):
            return
        try:
            text = await response.text()
        except PlaywrightError:
            return
        blob = parse_preview_json(text)
        if blob is None or self.preview_event.is_set():
            return
        if self._target_link and not preview_blob_matches_link(blob, self._target_link):
            return
        self.preview_json = text
        self.preview_event.set()

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


def _place_link_key(link: str) -> str:
    path = link.split("?", 1)[0]
    if path != link and extract_place_identity_from_url(path) is None:
        identity = extract_place_identity_from_url(link)
        if identity:
            return f"{path}?{identity}"
    return path


async def _get_place_urls(
    context: BrowserContext,
    max_places: int | None,
    query: str,
    geo_coordinates: Point,
    zoom: float,
    lang: str = "en",
    range_limit: float = DEFAULT_RANGE_LIMIT,
    proxy_rotator: ProxyRotator | None = None,
    query_timeout: float = 300.0,
    links_collector: set[str] | None = None,
    navigation_timeout: int = 30000,
    captcha_timeout: float = 85.0,
    max_consecutive_empty_scrolls: int = 4,
    max_consecutive_out_of_range_scrolls: int = 3,
    max_scroll_attempts_without_new_links: int = 5,
    initial_delay_range: tuple[float, float] = (1.0, 2.5),
    min_remaining_time: float = 2.0,
    scroll_retry_delay: float = 1.5,
    process_candidates: Callable[[list[str]], Awaitable[bool]] | None = None,
    query_issues: list[str] | None = None,
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
        lang (str, optional): Language code.
        range_limit (float): Maximum radius distance in meters from geo_coordinates.
            Google Maps local ranking combines Relevance, Distance, and Prominence
            (https://support.google.com/business/answer/7091). Prominent places further away
            may be returned before closer ones, so results are not strictly monotonic by distance.
            range_limit filters out places exceeding this radius (early drop).
        proxy_rotator (ProxyRotator | None, optional): Rotator to renew proxy on CAPTCHA block.
        query_timeout (float): Maximum seconds allowed for this query.
        links_collector (set[str] | None, optional): Mutable set to collect links in-place for zero data loss on timeout.
        navigation_timeout (int, optional): Navigation timeout in ms.
        captcha_timeout (float, optional): reCAPTCHA solving timeout in seconds.
        max_consecutive_empty_scrolls (int, optional): Max consecutive scrolls without new links before stopping.
        max_consecutive_out_of_range_scrolls (int, optional): Max consecutive scrolls with only out-of-range links before stopping.
        max_scroll_attempts_without_new_links (int, optional): Max scroll attempts when height is unchanged before stopping.
        initial_delay_range (tuple[float, float], optional): Range of random sleep delay after initial navigation.
        min_remaining_time (float, optional): Minimum remaining time threshold in seconds before early exit.
        scroll_retry_delay (float, optional): Sleep delay in seconds when scroll height is unchanged.
        process_candidates (Callable, optional): Process each ordered batch of candidates.
            When supplied, only the callback decides whether the place quota was reached.
        query_issues (list[str], optional): Collect reasons this query may be incomplete.

    Returns:
        set[str]: Collected place URLs.
    """
    search_page = await context.new_page()
    if not search_page:
        raise RuntimeError("Failed to create search browser page.")

    place_links: set[str] = links_collector if links_collector is not None else set()
    processed_links: set[str] = set()
    processing_seconds = 0.0

    async def collect_candidates(links: Sequence[str]) -> tuple[bool, bool, bool]:
        nonlocal processing_seconds
        candidates = []
        found_new_link = False
        for link in links:
            if not link:
                continue
            key = _place_link_key(link)
            if key in processed_links:
                continue
            processed_links.add(key)
            found_new_link = True
            coords = extract_place_coordinates_from_url(link)
            if coords is not None:
                distance = calculate_distance(geo_coordinates, coords)
                if distance > range_limit:
                    logger.debug(
                        "Early drop: Place %s is %.1fm away, exceeding range limit (%.1fm).",
                        link,
                        distance,
                        range_limit,
                    )
                    continue
            place_links.add(link)
            candidates.append(link)
            if (
                process_candidates is None
                and max_places is not None
                and len(place_links) >= max_places
            ):
                return True, True, True

        if candidates and process_candidates is not None:
            started = time.monotonic()
            try:
                if await process_candidates(candidates):
                    return True, True, True
            finally:
                processing_seconds += time.monotonic() - started
        return False, found_new_link, bool(candidates)

    try:
        if max_places is not None and max_places <= 0:
            return place_links
        query_start = time.monotonic()
        search_url = make_place_url(
            query=query, geo_coordinates=geo_coordinates, zoom=zoom, lang=lang
        )
        logger.info("Navigating to search URL: %s", search_url)

        await search_page.goto(
            search_url,
            wait_until="domcontentloaded",
            timeout=max(1, navigation_timeout),
        )
        await asyncio.sleep(
            random.uniform(initial_delay_range[0], initial_delay_range[1])
        )

        if "consent" in search_page.url:
            await _pass_consent(search_page)

        is_blocked = False
        try:
            captcha_solved = await _handle_captcha_if_present(
                search_page,
                context_label="search_page",
                timeout=captcha_timeout,
            )
        except RecaptchaBlockedError:
            captcha_solved = False
            is_blocked = True

        if is_blocked or (not captcha_solved and "sorry/index" in search_page.url):
            if proxy_rotator is not None:
                proxy_rotator.renew()
            if query_issues is not None:
                query_issues.append("CAPTCHA blocked search")
            return place_links

        if "/maps/place/" in search_page.url:
            await collect_candidates([search_page.url])
            return place_links

        active_feed_selector = await _find_feed_selector(search_page)
        if not active_feed_selector:
            if "/maps/place/" in search_page.url:
                await collect_candidates([search_page.url])
                return place_links
            if await _is_no_results_page(search_page):
                logger.info("No results found for query '%s'.", query)
                return place_links
            logger.error("Could not find results feed selector on search page.")
            if query_issues is not None:
                query_issues.append("results feed unavailable")
            return place_links

        link_selector = f'{active_feed_selector} a[href*="/maps/place/"]'
        initial_links = await search_page.locator(link_selector).evaluate_all(
            "elements => elements.map(a => a.href)"
        )
        reached_limit, new_links, had_candidates = await collect_candidates(
            initial_links
        )
        if reached_limit:
            return place_links

        last_height = await search_page.evaluate(
            "(sel) => document.querySelector(sel)?.scrollHeight || 0",
            active_feed_selector,
        )
        scroll_attempts_no_new = 0
        consecutive_empty_scrolls = 0
        consecutive_out_of_range_batches = int(new_links and not had_candidates)

        while True:
            elapsed = time.monotonic() - query_start - processing_seconds
            remaining_time = max(0.0, query_timeout - elapsed)
            if remaining_time <= min_remaining_time:
                logger.warning(
                    "⚠️ Query '%s' reached timeout limit (%.1fs) in get_place_urls. Returning %d collected links.",
                    query,
                    query_timeout,
                    len(place_links),
                )
                if query_issues is not None:
                    query_issues.append("search deadline reached")
                break

            await _scroll_feed(search_page, active_feed_selector)

            current_links = await search_page.locator(link_selector).evaluate_all(
                "elements => elements.map(a => a.href)"
            )
            reached_limit, new_links, had_candidates = await collect_candidates(
                current_links
            )
            if reached_limit:
                break

            new_height = await search_page.evaluate(
                "(sel) => document.querySelector(sel)?.scrollHeight || 0",
                active_feed_selector,
            )

            if new_links:
                consecutive_empty_scrolls = 0
                scroll_attempts_no_new = 0
                last_height = new_height
                consecutive_out_of_range_batches = (
                    0 if had_candidates else consecutive_out_of_range_batches + 1
                )
                continue

            if await _is_feed_at_end(search_page):
                logger.debug("Reached end of results list marker and no new links.")
                return place_links

            if (
                consecutive_out_of_range_batches
                and consecutive_out_of_range_batches
                >= max_consecutive_out_of_range_scrolls
            ):
                logger.debug(
                    "Stopping scroll in get_place_urls: no new links after %d out-of-range batches.",
                    consecutive_out_of_range_batches,
                )
                break
            consecutive_out_of_range_batches = 0
            consecutive_empty_scrolls += 1
            if consecutive_empty_scrolls >= max_consecutive_empty_scrolls:
                logger.debug(
                    "Stopping scroll in get_place_urls: %d consecutive scrolls without new links.",
                    consecutive_empty_scrolls,
                )
                break

            if new_height == last_height:
                scroll_attempts_no_new += 1
                logger.debug(
                    "Scroll height unchanged (%d/%d).",
                    scroll_attempts_no_new,
                    max_scroll_attempts_without_new_links,
                )
                await asyncio.sleep(scroll_retry_delay)
                if scroll_attempts_no_new >= max_scroll_attempts_without_new_links:
                    break
            else:
                last_height = new_height
                scroll_attempts_no_new = 0

        if (
            query_issues is not None
            and not processed_links
            and not query_issues
            and not await _is_no_results_page(search_page)
        ):
            query_issues.append("search feed contained no place links")

    except Exception as e:  # noqa: BLE001
        logger.error("Error during get_place_urls: %s", e)
        if query_issues is not None:
            query_issues.append(f"search failed: {type(e).__name__}")
    finally:
        await _safe_close_page(search_page)

    return place_links


async def _scrape_query_spa(
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
    query_timeout: float = 300.0,
    preview_timeout: float = 10000,
    results_collector: list[dict[str, Any]] | None = None,
    navigation_timeout: int = 30000,
    captcha_timeout: float = 85.0,
    max_consecutive_empty_scrolls: int = 4,
    max_consecutive_out_of_range_scrolls: int = 3,
    max_scroll_attempts_without_new_links: int = 5,
    static_cache_dir: Path = Path(".cache") / "static_assets",
    timezone_id: str | None = "UTC",
    initial_delay_range: tuple[float, float] = (1.0, 2.0),
    min_remaining_time: float = 2.0,
    element_timeout: int = 1000,
    pre_click_delay_range: tuple[float, float] = (0.3, 0.8),
    min_preview_timeout_ms: int = 1000,
    timeout_safety_margin: float = 0.5,
    post_item_delay_range: tuple[float, float] = (0.15, 0.35),
    post_scroll_delay_range: tuple[float, float] = (1.2, 2.2),
    scroll_retry_delay: float = 1.5,
    secondary_rescue_min_time: float = 5.0,
    secondary_rescue_concurrency: int = 4,
    secondary_rescue_cutoff: float = 3.0,
    min_valid_fields: int = 3,
    query_issues: list[str] | None = None,
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
            via range_limit (early drop) do not count toward this limit.
        lang (str, optional): Language code.
        fields (Sequence[str] | set[str] | None, optional): Selected fields.
        range_limit (float): Maximum radius distance in meters from geo_coordinates.
            Google Maps local ranking combines Relevance, Distance, and Prominence
            (https://support.google.com/business/answer/7091). Prominent places further away
            may be returned before closer ones, so results are not strictly monotonic by distance.
            range_limit filters out places exceeding this radius (early drop).
        proxy_rotator (ProxyRotator | None, optional): Proxy rotator to renew proxy on CAPTCHA.
        max_captcha_retries (int, optional): Max retries on CAPTCHA sorry page.
        query_timeout (float): Maximum seconds allowed for this query.
        preview_timeout (float | int, optional): Maximum timeout in ms (or seconds if < 1000)
            waiting for SPA place preview XHR response.
        results_collector (list[dict[str, Any]] | None, optional): Mutable list to collect
            places in-place for zero data loss on timeout.
        navigation_timeout (int, optional): Navigation timeout in ms.
        captcha_timeout (float, optional): reCAPTCHA solving timeout in seconds.
        max_consecutive_empty_scrolls (int, optional): Max consecutive scrolls without new links before stopping.
        max_consecutive_out_of_range_scrolls (int, optional): Max consecutive batches
            with only out-of-range links before stopping on a scroll without new links.
        max_scroll_attempts_without_new_links (int, optional): Max scroll attempts when height is unchanged before stopping.
        static_cache_dir (Path, optional): Directory to store static assets cache when creating rotated contexts.
        timezone_id (str | None, optional): IANA timezone ID for browser context. Defaults to "UTC".
        initial_delay_range (tuple[float, float], optional): Range of random sleep delay after initial navigation.
        min_remaining_time (float, optional): Minimum remaining time threshold in seconds before early exit.
        element_timeout (int, optional): Element operation timeout in ms.
        pre_click_delay_range (tuple[float, float], optional): Random jitter delay range before clicking an item.
        min_preview_timeout_ms (int, optional): Floor for preview response timeout in ms.
        timeout_safety_margin (float, optional): Safety margin in seconds subtracted from remaining time.
        post_item_delay_range (tuple[float, float], optional): Delay range after processing each item.
        post_scroll_delay_range (tuple[float, float], optional): Delay range after scrolling feed.
        scroll_retry_delay (float, optional): Sleep delay in seconds when scroll height is unchanged.
        secondary_rescue_min_time (float, optional): Minimum query remaining time to attempt secondary rescue.
        secondary_rescue_concurrency (int, optional): Maximum concurrency for secondary rescue.
        secondary_rescue_cutoff (float, optional): Remaining time cutoff below which secondary rescue aborts.
        min_valid_fields (int, optional): Minimum fields required for a place dictionary to be valid.
        query_issues (list[str] | None, optional): Collector for reasons the query ended with partial results.

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
    fallback_rescue_links: dict[str, str] = {}
    places_by_identity: dict[str, dict[str, Any]] = {}
    selected_fields = set(fields) if fields is not None else None
    extraction_fields = (
        selected_fields | {"name", "place_id", "latitude", "longitude"}
        if selected_fields is not None
        else None
    )
    requested_fields = (selected_fields or set()) - {"link"}

    def mark_incomplete(reason: str) -> None:
        if query_issues is not None and reason not in query_issues:
            query_issues.append(reason)

    def usable_place(data: dict[str, Any]) -> bool:
        useful_fields = {
            field
            for field, value in data.items()
            if value is not None and field not in {"link", "is_claimed"}
        }
        return bool(
            useful_fields & {"name", "place_id"}
            or len(useful_fields) >= min_valid_fields
            or useful_fields & requested_fields
        )

    def needs_detail(
        data: dict[str, Any], link: str, coords: tuple[float, float] | None
    ) -> bool:
        if coords is None and (
            data.get("latitude") is None or data.get("longitude") is None
        ):
            return True
        missing = {field for field in requested_fields if data.get(field) is None}
        if coords is not None:
            missing.difference_update(("latitude", "longitude"))
        if "place_id" in missing and extract_place_identity_from_url(link):
            missing.remove("place_id")
        return bool(missing)

    def fill_missing(data: dict[str, Any], extra: dict[str, Any]) -> None:
        coordinate_fields = ("latitude", "longitude")
        if any(data.get(field) is None for field in coordinate_fields) and all(
            extra.get(field) is not None for field in coordinate_fields
        ):
            data.update((field, extra[field]) for field in coordinate_fields)
        data.update(
            (field, value)
            for field, value in extra.items()
            if value is not None and data.get(field) is None
        )

    def is_out_of_range(
        data: dict[str, Any], coords: tuple[float, float] | None
    ) -> bool:
        latitude = data.get("latitude")
        longitude = data.get("longitude")
        if latitude is not None and longitude is not None:
            try:
                return (
                    calculate_distance(
                        geo_coordinates, (float(latitude), float(longitude))
                    )
                    > range_limit
                )
            except (ValueError, TypeError):
                pass
        return (
            coords is not None
            and calculate_distance(geo_coordinates, coords) > range_limit
        )

    def save_place(data: dict[str, Any], link: str) -> bool:
        data = data.copy()
        canonical_link = _place_link_key(link)
        identities = [canonical_link]
        if link_id := extract_place_identity_from_url(link):
            identities.append(link_id)
            if not data.get("place_id"):
                data["place_id"] = link_id
        if (data.get("latitude") is None or data.get("longitude") is None) and (
            url_coords := extract_place_coordinates_from_url(link)
        ) is not None:
            data["latitude"], data["longitude"] = url_coords
        place_id = data.get("place_id")
        if isinstance(place_id, str) and place_id:
            identities.append(extract_place_identity_from_url(place_id) or place_id)
        place = next(
            (
                places_by_identity[key]
                for key in identities
                if key in places_by_identity
            ),
            None,
        )
        if selected_fields is None:
            projected = {**data, "link": link}
        else:
            projected = {key: data.get(key) for key in selected_fields if key != "link"}
            if "link" in selected_fields:
                projected["link"] = link
        is_new = place is None
        if place is None:
            place = projected
            results.append(place)
        else:
            fill_missing(place, projected)
        for key in identities:
            places_by_identity[key] = place
        return is_new

    async def rescue_detail(
        link: str, semaphore: asyncio.Semaphore, remaining: float
    ) -> tuple[dict[str, Any], bool]:
        rejected: list[bool] = []
        try:
            async with asyncio.timeout(remaining - secondary_rescue_cutoff):
                data = await _process_link(
                    context=active_context,
                    link=urljoin(search_url, link),
                    count=len(results) + 1,
                    total=max_places
                    or (len(results) + max(1, len(fallback_rescue_links))),
                    semaphore=semaphore,
                    fields=extraction_fields,
                    max_retries=1,
                    proxy_rotator=proxy_rotator,
                    navigation_timeout=navigation_timeout,
                    captcha_timeout=captcha_timeout,
                    geo_coordinates=geo_coordinates,
                    lang=lang,
                    static_cache_dir=static_cache_dir,
                    timezone_id=timezone_id,
                    range_limit=range_limit,
                    out_of_range=rejected,
                )
        except Exception as error:  # noqa: BLE001
            logger.debug("Secondary rescue error for %s: %s", link, error)
            return {}, False
        return data or {}, bool(rejected) or is_out_of_range(
            data or {}, extract_place_coordinates_from_url(link)
        )

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
                timeout=max(1, navigation_timeout),
            )
            await asyncio.sleep(
                random.uniform(initial_delay_range[0], initial_delay_range[1])
            )

            if "consent" in search_page.url:
                await _pass_consent(search_page)

            is_blocked = False
            try:
                captcha_solved = await _handle_captcha_if_present(
                    search_page,
                    context_label="spa_search",
                    timeout=captcha_timeout,
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
                    await _safe_close_page(search_page)
                    search_page = None

                    new_proxy = proxy_rotator.renew() if proxy_rotator else None
                    browser = active_context.browser
                    if browser is not None:
                        new_context = await _create_browser_context(
                            browser=browser,
                            geo_coordinates=geo_coordinates,
                            lang=lang,
                            proxy=new_proxy,
                            static_cache_dir=static_cache_dir,
                            timezone_id=timezone_id,
                        )
                        created_contexts.append(new_context)
                        active_context = new_context
                        continue
                    break
                else:
                    logger.error("Exceeded max CAPTCHA retries on Google Sorry page.")
                    mark_incomplete("CAPTCHA retries exhausted")
                    return results

            break

        if not search_page or search_page.is_closed():
            mark_incomplete("Search page unavailable")
            return results

        active_feed_selector = None
        if "/maps/place/" not in search_page.url:
            active_feed_selector = await _find_feed_selector(search_page)

        # A single-place redirect can also finish while waiting for the results feed.
        if "/maps/place/" in search_page.url:
            logger.debug("Detected single place redirect.")
            link = search_page.url
            coords = extract_place_coordinates_from_url(link)
            if max_places == 0 or is_out_of_range({}, coords):
                return results
            html_content = await search_page.content()
            place_data = (
                extract_place_data(html_content=html_content, fields=extraction_fields)
                or {}
            )
            if is_out_of_range(place_data, coords):
                return results
            if not usable_place(place_data) or needs_detail(place_data, link, coords):
                remaining = query_timeout - (time.monotonic() - query_start)
                if remaining > secondary_rescue_min_time:
                    detail_data, rejected = await rescue_detail(
                        link, asyncio.Semaphore(1), remaining
                    )
                    if rejected:
                        return results
                    fill_missing(place_data, detail_data)
            if usable_place(place_data):
                save_place(place_data, link)
            else:
                mark_incomplete("Single result could not be extracted")
            return results

        if not active_feed_selector:
            if await _is_no_results_page(search_page):
                logger.info("No results found for query '%s'.", query)
                return results
            logger.error("Could not find results feed selector on search page.")
            mark_incomplete("Results feed unavailable")
            return results

        scroll_attempts_no_new = 0
        consecutive_empty_scrolls = 0
        consecutive_out_of_range_scrolls = 0
        rescue_sem = asyncio.Semaphore(max(1, secondary_rescue_concurrency))
        seen_link = False
        is_at_end = False
        last_height = await search_page.evaluate(
            "(sel) => document.querySelector(sel)?.scrollHeight || 0",
            active_feed_selector,
        )

        while max_places is None or len(results) < max_places:
            elapsed = time.monotonic() - query_start
            remaining_time = max(0.0, query_timeout - elapsed)
            if remaining_time <= min_remaining_time:
                mark_incomplete("Query time budget exhausted")
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
            new_links_in_batch: set[str] = set()
            out_of_range_links: set[str] = set()

            for el in link_elements:
                if max_places is not None and len(results) >= max_places:
                    break

                elapsed = time.monotonic() - query_start
                remaining_time = max(0.0, query_timeout - elapsed)
                if remaining_time <= min_remaining_time:
                    mark_incomplete("Query time budget exhausted")
                    logger.warning(
                        "⚠️ Query '%s' approaching timeout limit (%.1fs remaining). Returning %d collected places.",
                        query,
                        max(0.0, remaining_time),
                        len(results),
                    )
                    break

                try:
                    link = await el.get_attribute("href", timeout=element_timeout)
                except (PlaywrightError, TypeError):
                    link = None
                if not link:
                    continue

                seen_link = True
                canonical_link = _place_link_key(link)
                if canonical_link in processed_links:
                    continue
                if canonical_link not in fallback_rescue_links:
                    new_links_in_batch.add(canonical_link)

                # Early Drop per element: Google Maps local results balance Relevance, Distance,
                # and Prominence (https://support.google.com/business/answer/7091). Due to high prominence,
                # a distant place (> range_limit) can appear interspersed among closer places (< range_limit).
                # An abrupt Early Stop would miss valid nearby places in subsequent scrolls.
                # Therefore, each element is individually checked and dropped if out of range.
                coords = extract_place_coordinates_from_url(link)
                if coords is not None:
                    dist = calculate_distance(geo_coordinates, coords)
                    if dist > range_limit:
                        processed_links.add(canonical_link)
                        out_of_range_links.add(canonical_link)
                        logger.debug(
                            "Early drop: Place %s is %.1fm away, exceeding range limit (%.1fm).",
                            canonical_link,
                            dist,
                            range_limit,
                        )
                        continue

                def is_matching_preview(resp: Any, target: str = link) -> bool:
                    return is_preview_response_for_link(resp, target)

                # Human-like pre-click jitter delay
                await asyncio.sleep(
                    random.uniform(pre_click_delay_range[0], pre_click_delay_range[1])
                )

                preview_timeout_ms = max(
                    1,
                    int(preview_timeout * 1000)
                    if preview_timeout < 1000
                    else int(preview_timeout),
                )
                cur_timeout_ms = max(
                    1,
                    min(
                        preview_timeout_ms,
                        max(
                            min_preview_timeout_ms,
                            int(
                                max(0.0, remaining_time - timeout_safety_margin) * 1000
                            ),
                        ),
                    ),
                )

                # Ensure element is scrolled into view before clicking
                try:
                    await el.scroll_into_view_if_needed(timeout=element_timeout)
                except PlaywrightError:
                    pass

                preview_json: str | None = None
                try:
                    async with search_page.expect_response(
                        is_matching_preview,
                        timeout=max(1, cur_timeout_ms),
                    ) as response_info:
                        try:
                            clicked = await el.evaluate(
                                """(e, href) => {
                                    if (e.getAttribute('href') !== href) return false;
                                    e.click();
                                    return true;
                                }""",
                                link,
                            )
                        except PlaywrightError:
                            await el.scroll_into_view_if_needed(timeout=element_timeout)
                            if (
                                await el.get_attribute("href", timeout=element_timeout)
                                != link
                            ):
                                raise
                            await el.click(force=True, timeout=element_timeout)
                        else:
                            if clicked is False:
                                raise PlaywrightError("Place card changed before click")

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

                place_data: dict[str, Any] = {}
                if preview_blob is not None and preview_blob_matches_link(
                    preview_blob, link
                ):
                    place_data = (
                        extract_place_data(
                            preview_blob=preview_blob, fields=extraction_fields
                        )
                        or {}
                    )

                if is_out_of_range(place_data, coords):
                    processed_links.add(canonical_link)
                    out_of_range_links.add(canonical_link)
                    continue

                verified_dom_link = False
                if (
                    not usable_place(place_data)
                    or any(place_data.get(field) is None for field in requested_fields)
                    or (
                        selected_fields is None
                        and not place_data.get("place_id")
                        and not extract_place_identity_from_url(link)
                    )
                ):
                    card_html = ""
                    try:
                        card_html = await el.evaluate(
                            """(e, href) => {
                                if (e.getAttribute('href') !== href) return '';
                                return e.closest('div.Nv2PK, div[role="article"], div.THOPZb, div.fontBodyMedium')?.outerHTML
                                    || e.parentElement?.parentElement?.outerHTML || e.outerHTML;
                            }""",
                            link,
                        )
                    except PlaywrightError:
                        pass
                    if isinstance(card_html, str) and card_html:
                        dom_data = extract_feed_item_dom(
                            card_html, link=link, coords=coords
                        )
                        if dom_data:
                            verified_dom_link = True
                            if dom_data.keys() - {
                                "link",
                                "place_id",
                                "latitude",
                                "longitude",
                            }:
                                fill_missing(place_data, dom_data)

                if is_out_of_range(place_data, coords):
                    processed_links.add(canonical_link)
                    out_of_range_links.add(canonical_link)
                elif usable_place(place_data) or (
                    verified_dom_link and selected_fields == {"link"}
                ):
                    # Resolve missing coordinates before the row can fill the query quota.
                    if needs_detail(place_data, link, coords):
                        remaining = query_timeout - (time.monotonic() - query_start)
                        if remaining > secondary_rescue_min_time:
                            detail_data, rejected = await rescue_detail(
                                link, rescue_sem, remaining
                            )
                            if rejected:
                                processed_links.add(canonical_link)
                                out_of_range_links.add(canonical_link)
                                continue
                            fill_missing(place_data, detail_data)
                    processed_links.add(canonical_link)
                    if save_place(place_data, link):
                        found_new_in_batch = True
                        logger.info(
                            "  ✅ [SPA %d/%s] Extracted: %s",
                            len(results),
                            str(max_places),
                            place_data.get("name", canonical_link),
                        )
                else:
                    logger.warning(
                        "  ⚠️ Could not extract usable preview or DOM data for: %s",
                        canonical_link,
                    )
                    fallback_rescue_links.setdefault(canonical_link, link)

                await asyncio.sleep(
                    random.uniform(post_item_delay_range[0], post_item_delay_range[1])
                )

            if max_places is not None and len(results) >= max_places:
                break

            if (
                max(0.0, query_timeout - (time.monotonic() - query_start))
                <= min_remaining_time
            ):
                mark_incomplete("Query time budget exhausted")
                logger.warning(
                    "⚠️ Query '%s' reached timeout limit (%.1fs). Returning %d collected places.",
                    query,
                    query_timeout,
                    len(results),
                )
                break

            await _scroll_feed(search_page, active_feed_selector)
            # Human-like post-scroll reading delay
            await asyncio.sleep(
                random.uniform(post_scroll_delay_range[0], post_scroll_delay_range[1])
            )

            is_at_end = await _is_feed_at_end(search_page)
            new_height = await search_page.evaluate(
                "(sel) => document.querySelector(sel)?.scrollHeight || 0",
                active_feed_selector,
            )
            remaining_links = await search_page.locator(
                f'{active_feed_selector} a[href*="/maps/place/"]'
            ).evaluate_all("els => els.map(a => a.getAttribute('href'))")
            pending_keys = {_place_link_key(link) for link in remaining_links if link}
            has_pending = bool(
                pending_keys - processed_links - fallback_rescue_links.keys()
            )

            if (
                not found_new_in_batch
                and new_links_in_batch
                and new_links_in_batch <= out_of_range_links
            ):
                consecutive_out_of_range_scrolls += 1
            else:
                consecutive_out_of_range_scrolls = 0

            # A scroll can reveal more places after either counter reaches its limit.
            if has_pending:
                consecutive_empty_scrolls = 0
                scroll_attempts_no_new = 0
                last_height = new_height
                continue

            if is_at_end:
                logger.debug(
                    "Reached end of results list marker in SPA feed and all links attempted."
                )
                break

            if (
                consecutive_out_of_range_scrolls
                and consecutive_out_of_range_scrolls
                >= max_consecutive_out_of_range_scrolls
            ):
                logger.debug(
                    "Stopping SPA scroll: no new links after %d consecutive batches with only out-of-range items.",
                    consecutive_out_of_range_scrolls,
                )
                break

            consecutive_empty_scrolls += 1
            if consecutive_empty_scrolls >= max_consecutive_empty_scrolls:
                logger.debug(
                    "Stopping SPA scroll: %d consecutive scrolls without new links.",
                    consecutive_empty_scrolls,
                )
                break

            if new_height == last_height and not found_new_in_batch:
                scroll_attempts_no_new += 1
                if scroll_attempts_no_new >= max_scroll_attempts_without_new_links:
                    logger.debug("Stopping SPA scroll due to lack of new items.")
                    break
                await asyncio.sleep(scroll_retry_delay)
            else:
                last_height = new_height
                scroll_attempts_no_new = 0

        def unresolved(canonical: str, link: str) -> bool:
            return (
                canonical not in processed_links
                and extract_place_identity_from_url(link) not in places_by_identity
            )

        rescue_links = {
            key: link
            for key, link in fallback_rescue_links.items()
            if unresolved(key, link)
        }
        remaining = query_timeout - (time.monotonic() - query_start)
        if rescue_links and remaining > secondary_rescue_min_time:
            for key, link in rescue_links.items():
                if max_places is not None and len(results) >= max_places:
                    break
                if not unresolved(key, link):
                    continue
                remaining = query_timeout - (time.monotonic() - query_start)
                if remaining <= secondary_rescue_cutoff:
                    break
                data, rejected = await rescue_detail(link, rescue_sem, remaining)
                if rejected:
                    processed_links.add(key)
                elif usable_place(data):
                    processed_links.add(key)
                    save_place(data, link)

        if max_places is None or len(results) < max_places:
            if any(unresolved(key, link) for key, link in rescue_links.items()):
                mark_incomplete("One or more place links could not be extracted")
            if (
                query_issues is not None
                and not seen_link
                and not is_at_end
                and not await _is_no_results_page(search_page)
            ):
                mark_incomplete("Results feed had no readable place links")

    except Exception as e:  # noqa: BLE001
        logger.error("Error during scrape_query_spa: %s", e)
        mark_incomplete(f"SPA query failed: {type(e).__name__}")
    finally:
        await _safe_close_page(search_page)
        for extra_ctx in created_contexts:
            await _safe_close_context(extra_ctx)

    return results


async def _process_link(
    context: BrowserContext,
    link: str,
    semaphore: asyncio.Semaphore,
    count: int,
    total: int | None,
    fields: Sequence[str] | set[str] | None = None,
    max_retries: int = 2,
    proxy_rotator: ProxyRotator | None = None,
    navigation_timeout: int = 30000,
    captcha_timeout: float = 85.0,
    preview_wait_timeout: float = 4.5,
    main_selector_timeout: int = 2000,
    retry_delay_range: tuple[float, float] = (0.8, 1.5),
    captcha_retry_delay_range: tuple[float, float] = (1.0, 2.0),
    min_valid_fields: int = 3,
    geo_coordinates: Point | None = None,
    lang: str = "en",
    static_cache_dir: Path = Path(".cache") / "static_assets",
    timezone_id: str | None = "UTC",
    range_limit: float | None = None,
    out_of_range: list[bool] | None = None,
    result_collector: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Processes a single place link in multi-page fallback mode:
    - Pure I/O: intercepts rich network payload or collects HTML content
    - Pure extraction: delegates parsing to extractor.py
    - Includes automatic retry and resilient page lifecycle cleanup

    Args:
        context (BrowserContext): Isolated browser context.
        link (str): URL of place to scrape.
        semaphore (asyncio.Semaphore): Concurrency semaphore.
        count (int): Current link index.
        total (int | None): Total number of places when known.
        fields (Sequence[str] | set[str] | None, optional): Specific fields to extract.
        max_retries (int, optional): Max attempts for this link.
        proxy_rotator (ProxyRotator | None, optional): Proxy rotator.
        navigation_timeout (int, optional): Navigation timeout in ms.
        captcha_timeout (float, optional): CAPTCHA solving timeout in seconds.
        preview_wait_timeout (float, optional): Timeout waiting for preview payload in seconds.
        main_selector_timeout (int, optional): Timeout waiting for main content selector in ms.
        retry_delay_range (tuple[float, float], optional): Delay range in seconds before retry attempt.
        captcha_retry_delay_range (tuple[float, float], optional): Delay range in seconds before CAPTCHA retry.
        min_valid_fields (int, optional): Minimum fields required for valid place data.
        geo_coordinates (Point | None, optional): Reference coordinates for dynamic context recreation.
        lang (str, optional): Language code.
        static_cache_dir (Path, optional): Directory to store static assets cache.
        timezone_id (str | None, optional): IANA timezone ID for browser context. Defaults to "UTC".
        range_limit (float, optional): Radius to apply to verified coordinates.
        out_of_range (list[bool], optional): Receives True on a verified radius rejection.
        result_collector (list[dict], optional): Stores a valid row before cleanup.

    Returns:
        dict[str, Any] | None: Extracted place data dictionary, or None on failure.
    """
    active_context = context
    created_contexts: list[BrowserContext] = []
    place_fields = (
        {*fields, "name", "place_id", "latitude", "longitude"}
        if fields is not None
        else None
    )
    link_coords = extract_place_coordinates_from_url(link)
    link_identity = extract_place_identity_from_url(link)

    def place_coordinates(place: dict[str, Any]) -> tuple[float, float] | None:
        try:
            if place.get("latitude") is not None and place.get("longitude") is not None:
                latitude, longitude = (
                    float(place["latitude"]),
                    float(place["longitude"]),
                )
                if -90 <= latitude <= 90 and -180 <= longitude <= 180:
                    return latitude, longitude
        except (TypeError, ValueError, OverflowError):
            pass
        return link_coords

    def finalize(
        place: dict[str, Any], detail_url: str | None = None
    ) -> dict[str, Any] | None:
        coords = place_coordinates(place)
        if coords is None and detail_url:
            coords = extract_place_coordinates_from_url(detail_url)
        if coords is not None:
            if (
                range_limit is not None
                and geo_coordinates is not None
                and calculate_distance(geo_coordinates, coords) > range_limit
            ):
                if out_of_range is not None:
                    out_of_range.append(True)
                if result_collector is not None:
                    result_collector.clear()
                return None
            place["latitude"], place["longitude"] = coords

        if not place.get("place_id"):
            identity = link_identity or (
                extract_place_identity_from_url(detail_url) if detail_url else None
            )
            if identity:
                place["place_id"] = identity
        if fields is None or "link" in fields:
            place["link"] = link
        if result_collector is not None:
            result_collector[:] = [place]
        return place

    def valid_place(place: dict[str, Any] | None) -> bool:
        return bool(
            place
            and (
                place.get("name")
                or place.get("place_id")
                or sum(value is not None for value in place.values())
                >= min_valid_fields
            )
        )

    try:
        async with semaphore:
            for attempt in range(1, max_retries + 1):
                start_time = time.time()
                page: Page | None = None
                try:
                    logger.info(
                        "Processing link [%d/%s] (attempt %d/%d): %s",
                        count,
                        total if total is not None else "?",
                        attempt,
                        max_retries,
                        link,
                    )
                    page = await active_context.new_page()
                    interceptor = _PreviewInterceptor(page, target_link=link)

                    await page.set_extra_http_headers(
                        {
                            "Referer": "https://www.google.com/",
                            "Accept-Language": "en-US,en;q=0.9,vi;q=0.8",
                        }
                    )

                    try:
                        response = await page.goto(
                            link,
                            wait_until="domcontentloaded",
                            timeout=max(1, navigation_timeout),
                        )
                    except PlaywrightTimeoutError:
                        logger.warning("  ❌ Timeout navigating to: %s", link)
                        if attempt < max_retries:
                            await asyncio.sleep(
                                random.uniform(
                                    retry_delay_range[0], retry_delay_range[1]
                                )
                            )
                            continue
                        return None
                    except PlaywrightError as e:
                        logger.error("  ❌ Navigation error for %s: %s", link, e)
                        if attempt < max_retries:
                            await asyncio.sleep(
                                random.uniform(
                                    retry_delay_range[0], retry_delay_range[1]
                                )
                            )
                            continue
                        return None
                    status = getattr(response, "status", None)
                    if (
                        isinstance(status, int)
                        and status >= 400
                        and "sorry/index" not in page.url
                    ):
                        logger.warning("HTTP %d navigating to place: %s", status, link)
                        if attempt < max_retries:
                            continue
                        return None

                    # Wait for preview API response
                    preview_json = await interceptor.wait_for_preview(
                        timeout=preview_wait_timeout
                    )
                    preview_blob = (
                        parse_preview_json(preview_json)
                        if isinstance(preview_json, str)
                        else None
                    )
                    if preview_blob is not None and not preview_blob_matches_link(
                        preview_blob, link
                    ):
                        preview_blob = None
                    preview_data = (
                        extract_place_data(
                            preview_blob=preview_blob, fields=place_fields
                        )
                        if preview_blob is not None
                        else None
                    )

                    if not valid_place(preview_data):
                        preview_data = None
                        try:
                            await page.wait_for_selector(
                                "h1, [role='main']", timeout=main_selector_timeout
                            )
                        except PlaywrightError:
                            pass
                    elif preview_data is not None and (
                        place_coordinates(preview_data) is not None
                        or range_limit is None
                    ):
                        resolved_preview = finalize(preview_data.copy())
                        if resolved_preview is None or (
                            fields is not None
                            and all(
                                resolved_preview.get(key) is not None for key in fields
                            )
                        ):
                            return resolved_preview

                    # CAPTCHA verification
                    is_blocked = False
                    try:
                        captcha_solved = await _handle_captcha_if_present(
                            page,
                            context_label="process_link",
                            timeout=captcha_timeout,
                        )
                    except RecaptchaBlockedError:
                        captcha_solved = False
                        is_blocked = True

                    if (not captcha_solved and "sorry/index" in page.url) or is_blocked:
                        if preview_data is not None:
                            return finalize(preview_data)
                        if attempt == max_retries:
                            return None
                        if proxy_rotator is not None:
                            logger.warning(
                                "CAPTCHA blocked/unsolved in process_link, renewing proxy/circuit and creating fresh context..."
                            )
                            new_proxy = proxy_rotator.renew()
                            browser = active_context.browser
                            if browser is not None:
                                await _safe_close_page(page)
                                page = None

                                coords = geo_coordinates
                                if coords is None:
                                    extracted_coords = extract_coordinates_from_url(
                                        link
                                    )
                                    if extracted_coords:
                                        coords = Point(
                                            extracted_coords[0], extracted_coords[1]
                                        )
                                    else:
                                        coords = Point(21.018785, 105.830415)

                                new_context = await _create_browser_context(
                                    browser=browser,
                                    geo_coordinates=coords,
                                    lang=lang,
                                    proxy=new_proxy,
                                    static_cache_dir=static_cache_dir,
                                    timezone_id=timezone_id,
                                )
                                created_contexts.append(new_context)
                                active_context = new_context

                        await asyncio.sleep(
                            random.uniform(
                                captcha_retry_delay_range[0],
                                captcha_retry_delay_range[1],
                            )
                        )
                        continue

                    page_identity = extract_place_identity_from_url(page.url)
                    if (
                        "/maps/place/" not in page.url
                        or compare_place_url_ids(link, page.url) is False
                    ):
                        logger.warning(
                            "Detail page redirected away from the requested place: %s",
                            link,
                        )
                        if preview_data is not None:
                            return finalize(preview_data)
                        return None

                    try:
                        html_content = await page.content()
                    except PlaywrightError:
                        if preview_data is not None:
                            return finalize(preview_data, page.url)
                        raise
                    place_data = extract_place_data(
                        html_content=html_content,
                        preview_blob=preview_blob,
                        fields=place_fields,
                    )

                    if place_data is not None and valid_place(place_data):
                        elapsed = time.time() - start_time
                        logger.info("  ✅ Extracted: %s in %.2fs", link, elapsed)
                        return finalize(place_data, page.url)
                    if preview_data is not None:
                        return finalize(preview_data, page.url)
                    if (
                        fields is not None
                        and set(fields) == {"link"}
                        and (
                            link_identity
                            or page_identity
                            or _place_link_key(link) == _place_link_key(page.url)
                        )
                    ):
                        return finalize({}, page.url)

                    logger.warning("  ⚠️ Extraction returned None: %s", link)
                    if attempt < max_retries:
                        await asyncio.sleep(
                            random.uniform(retry_delay_range[0], retry_delay_range[1])
                        )
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
                        await asyncio.sleep(
                            random.uniform(retry_delay_range[0], retry_delay_range[1])
                        )
                    else:
                        return None
                finally:
                    await _safe_close_page(page)

            return None
    finally:
        for extra_ctx in created_contexts:
            await _safe_close_context(extra_ctx)


async def _stagger_query_start(
    idx: int,
    query: str,
    stagger_delay: tuple[float, float] | list[float] | float | None,
) -> None:
    """Delay later query launches before they acquire the query semaphore."""
    if stagger_delay is None or idx <= 0:
        return
    if isinstance(stagger_delay, (tuple, list)):
        s_min, s_max = float(stagger_delay[0]), float(stagger_delay[1])
        if not s_max > 0:
            return
        delay = idx * random.uniform(s_min, s_max)
    elif isinstance(stagger_delay, (int, float)) and stagger_delay > 0:
        delay = idx * float(stagger_delay)
    else:
        return
    logger.info("Staggered query %d ('%s') delay %.2fs...", idx, query, delay)
    await asyncio.sleep(delay)


async def _gather_tasks[T](
    tasks: Sequence[asyncio.Task[T]],
) -> list[T | BaseException]:
    """Gather in input order, draining unfinished tasks if the caller is cancelled."""
    try:
        return await asyncio.gather(*tasks, return_exceptions=True)
    except asyncio.CancelledError:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


async def scrape_google_maps(
    queries: set[str],
    geo_coordinates: Point,
    zoom: float,
    proxy: _ProxyInput = None,
    max_places: int = 120,
    lang: str = "en",
    headless: bool = True,
    n_semaphore: int = 8,
    fields: Sequence[str] | set[str] | None = None,
    flatten: bool = False,
    use_spa: bool = True,
    cache_dir: Path | None = None,
    range_limit: float = DEFAULT_RANGE_LIMIT,
    query_timeout: float = 300.0,
    place_timeout: float = 45.0,
    preview_timeout: float = 10000,
    stagger_delay: tuple[float, float] | float = (1.5, 3.5),
    navigation_timeout: int = 30000,
    captcha_timeout: float = 85.0,
    max_captcha_retries: int = 2,
    static_cache_dir: Path = Path(".cache") / "static_assets",
    disk_cache_size: int = 1073741824,
    max_consecutive_empty_scrolls: int = 4,
    max_consecutive_out_of_range_scrolls: int = 3,
    max_scroll_attempts_without_new_links: int = 5,
    watchdog_grace_period: float = 10.0,
    timezone_id: str | None = "UTC",
    *,
    on_query_incomplete: Callable[[str, tuple[str, ...]], None] | None = None,
) -> pl.DataFrame:
    """Scrapes Google Maps for places based on queries.

    Args:
        queries (set[str]): Search queries (e.g. {"cafe", "restaurant"}).
        geo_coordinates (Point): Center coordinates for search.
        zoom (float): Map zoom level.
        proxy (ProxySettings | Sequence[ProxySettings] | str | Sequence[str] | None, optional): Single proxy
            or sequence of proxies for round-robin rotation.
        max_places (int, optional): Maximum valid places to collect per query. Places
            dropped via range_limit (early drop) do not count toward this limit.
        lang (str, optional): Language code for Google Maps.
        headless (bool, optional): Whether to run headless browser.
        n_semaphore (int, optional): Maximum concurrent searches. Fallback detail
            requests have a separate concurrency limit of the same size.
        fields (Sequence[str] | set[str], optional): Selected place fields to extract.
            The original query is always included as a top-level column.
        flatten (bool, optional): Whether to flatten all fields into individual columns.
            If False (default), bundles non-default fields into a 'details' JSON string column,
            keeping 11 common columns at top-level.
        use_spa (bool, optional): Whether to use high-speed SPA navigation.
        cache_dir (Path | None, optional): Persistent Chromium disk cache directory.
            Defaults to None (omits disk cache flags to eliminate multi-context file lock contention).
            Pass a Path to explicitly enable persistent disk cache.
        range_limit (float): Maximum radius distance in meters from geo_coordinates.
            Google Maps local ranking combines Relevance, Distance, and Prominence
            (https://support.google.com/business/answer/7091). Prominent places further away
            may be returned before closer ones, so results are not strictly monotonic by distance.
            range_limit filters out places exceeding this radius (early drop).
        query_timeout (float): Maximum seconds allowed per query before early return.
            In fallback mode, this covers search and feed traversal only.
        place_timeout (float, optional): Maximum seconds allowed to scrape a place in fallback mode.
        preview_timeout (float | int, optional): Maximum timeout in ms (or seconds if < 1000)
            waiting for SPA place preview XHR response.
        stagger_delay (tuple[float, float] | float, optional): Delay range (min, max) in
            seconds to stagger the initial launch of concurrent queries. Set to 0 to disable.
        navigation_timeout (int, optional): Maximum navigation timeout in ms for pages.
        captcha_timeout (float, optional): Maximum timeout in seconds for reCAPTCHA solving.
        max_captcha_retries (int, optional): Maximum proxy rotation retries upon encountering CAPTCHA.
        static_cache_dir (Path, optional): Directory to store static assets cache.
        disk_cache_size (int, optional): Maximum disk cache size in bytes.
        max_consecutive_empty_scrolls (int, optional): Maximum consecutive empty scrolls before stopping search.
        max_consecutive_out_of_range_scrolls (int, optional): Maximum consecutive batches
            with only out-of-range places before stopping. Both modes check that the next
            scroll exposes no unattempted links.
        max_scroll_attempts_without_new_links (int, optional): Maximum scroll attempts with unchanged height before stopping.
        watchdog_grace_period (float, optional): Extra grace period in seconds added to query_timeout
            for the hard watchdog timer.
        timezone_id (str | None, optional): IANA timezone ID for browser context
            (e.g. "UTC", "Asia/Tokyo"). Defaults to "UTC".
        on_query_incomplete (Callable, optional): Called once for each query with
            detected operational failures, with the original query and failure reasons.
            Partial rows from affected queries remain in the returned DataFrame.

    Returns:
        pl.DataFrame: DataFrame containing scraped places data. Places found by
            multiple queries have one row per query, with its original string.

    Raises:
        RuntimeError: If Playwright cannot find the Chromium executable. Install
            it with `uv run playwright install chromium` before scraping.
    """
    results: list[dict[str, Any]] = []
    query_list = list(queries)
    query_issues: dict[str, list[str]] = (
        {query: [] for query in query_list} if on_query_incomplete is not None else {}
    )
    extraction_fields = fields
    if fields is not None and "query" in fields:
        requested_place_fields = [field for field in fields if field != "query"]
        extraction_fields = requested_place_fields or None
    browser = None
    proxy_rotator = ProxyRotator(proxy)

    launch_args = list(LAUNCH_ARGS)
    if cache_dir is not None:
        resolved_cache = cache_dir.resolve()
        resolved_cache.mkdir(parents=True, exist_ok=True)
        launch_args.extend(
            [
                f"--disk-cache-dir={resolved_cache}",
                f"--disk-cache-size={disk_cache_size}",
            ]
        )

    node_options = os.environ.get("NODE_OPTIONS", "")
    if "--no-warnings" not in node_options:
        os.environ["NODE_OPTIONS"] = f"{node_options} --no-warnings".strip()

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
                    await _stagger_query_start(idx, q, stagger_delay)

                    async with query_semaphore:
                        issues = query_issues.get(q)
                        allocated_proxy = proxy_rotator.get()
                        context = await _create_browser_context(
                            browser=browser,
                            geo_coordinates=geo_coordinates,
                            lang=lang,
                            proxy=allocated_proxy,
                            static_cache_dir=static_cache_dir,
                            timezone_id=timezone_id,
                        )
                        collector: list[dict[str, Any]] = []
                        try:
                            coro = _scrape_query_spa(
                                context=context,
                                query=q.replace("_", " "),
                                geo_coordinates=geo_coordinates,
                                zoom=zoom,
                                max_places=max_places,
                                lang=lang,
                                fields=extraction_fields,
                                range_limit=range_limit,
                                proxy_rotator=proxy_rotator,
                                max_captcha_retries=max_captcha_retries,
                                query_timeout=query_timeout,
                                preview_timeout=preview_timeout,
                                results_collector=collector,
                                navigation_timeout=navigation_timeout,
                                captcha_timeout=captcha_timeout,
                                max_consecutive_empty_scrolls=max_consecutive_empty_scrolls,
                                max_consecutive_out_of_range_scrolls=max_consecutive_out_of_range_scrolls,
                                max_scroll_attempts_without_new_links=max_scroll_attempts_without_new_links,
                                static_cache_dir=static_cache_dir,
                                timezone_id=timezone_id,
                                query_issues=issues,
                            )
                            return await asyncio.wait_for(
                                coro,
                                timeout=query_timeout + watchdog_grace_period,
                            )
                        except TimeoutError:
                            if issues is not None:
                                issues.append("hard query deadline reached")
                            logger.warning(
                                "🚨 Hard watchdog timeout for query '%s' after %.1fs. Returning %d rescued places.",
                                q,
                                query_timeout + watchdog_grace_period,
                                len(collector),
                            )
                            return collector
                        except asyncio.CancelledError:
                            logger.debug("run_spa_query cancelled for query '%s'", q)
                            raise
                        except Exception as e:  # noqa: BLE001
                            if issues is not None:
                                issues.append(f"query failed: {type(e).__name__}")
                            logger.error(
                                "❌ Error in run_spa_query for query '%s': %s",
                                q,
                                e,
                            )
                            return collector
                        finally:
                            await _safe_close_context(context)

                spa_tasks = [
                    asyncio.create_task(run_spa_query(i, query))
                    for i, query in enumerate(query_list)
                ]
                list_of_results = await _gather_tasks(spa_tasks)
                for query, res in zip(query_list, list_of_results, strict=True):
                    if isinstance(res, BaseException):
                        issues = query_issues.get(query)
                        if issues is not None:
                            issues.append(f"query failed: {type(res).__name__}")
                        logger.error(
                            "Unexpected exception gathered in SPA query: %s",
                            res,
                        )
                    elif isinstance(res, list):
                        results.extend({**place, "query": query} for place in res)
                logger.info(
                    "✅ Successfully collected %d places via SPA Navigation.",
                    len(results),
                )
            else:
                detail_semaphore = asyncio.Semaphore(n_semaphore)
                detail_tasks: dict[
                    str, asyncio.Task[tuple[dict[str, Any] | None, bool]]
                ] = {}

                async def run_process_link(
                    index: int, link: str
                ) -> tuple[dict[str, Any] | None, bool]:
                    async with detail_semaphore:
                        detail_context = None
                        recovered: list[dict[str, Any]] = []
                        rejected: list[bool] = []
                        try:
                            async with asyncio.timeout(place_timeout):
                                detail_context = await _create_browser_context(
                                    browser=browser,
                                    geo_coordinates=geo_coordinates,
                                    lang=lang,
                                    proxy=proxy_rotator.get(),
                                    static_cache_dir=static_cache_dir,
                                    timezone_id=timezone_id,
                                )
                                place = await _process_link(
                                    detail_context,
                                    link,
                                    asyncio.Semaphore(1),
                                    index,
                                    None,
                                    fields=extraction_fields,
                                    proxy_rotator=proxy_rotator,
                                    navigation_timeout=navigation_timeout,
                                    captcha_timeout=captcha_timeout,
                                    geo_coordinates=geo_coordinates,
                                    lang=lang,
                                    static_cache_dir=static_cache_dir,
                                    timezone_id=timezone_id,
                                    range_limit=range_limit,
                                    out_of_range=rejected,
                                    result_collector=recovered,
                                )
                                return place, bool(rejected)
                        except TimeoutError:
                            logger.warning(
                                "Timeout processing place link after %.1fs: %s",
                                place_timeout,
                                link,
                            )
                            return next(iter(recovered), None), bool(rejected)
                        except Exception as e:  # noqa: BLE001
                            logger.error("Error processing place link %s: %s", link, e)
                            return next(iter(recovered), None), bool(rejected)
                        finally:
                            await _safe_close_context(detail_context)

                async def run_get_urls(idx: int, query: str) -> list[dict[str, Any]]:
                    await _stagger_query_start(idx, query, stagger_delay)
                    places: list[dict[str, Any]] = []
                    identities: dict[str, dict[str, Any]] = {}
                    tried_links: set[str] = set()
                    failed_identities: set[str] = set()
                    issues = query_issues.get(query)

                    async def process_candidates(candidates: list[str]) -> bool:
                        pending = iter(candidates)
                        while max_places is None or len(places) < max_places:
                            remaining = (
                                n_semaphore
                                if max_places is None
                                else min(n_semaphore, max_places - len(places))
                            )
                            batch: list[
                                tuple[
                                    str,
                                    asyncio.Task[tuple[dict[str, Any] | None, bool]],
                                ]
                            ] = []
                            for link in pending:
                                key = _place_link_key(link)
                                if key in tried_links:
                                    continue
                                tried_links.add(key)
                                identity = extract_place_identity_from_url(link) or key
                                if identity in identities:
                                    continue
                                task = detail_tasks.get(key)
                                if task is None:
                                    task = asyncio.create_task(
                                        run_process_link(len(detail_tasks) + 1, link)
                                    )
                                    detail_tasks[key] = task
                                batch.append((link, task))
                                if len(batch) >= remaining:
                                    break
                            if not batch:
                                return False

                            # Keep feed order while independent detail pages load concurrently.
                            for link, task in batch:
                                place, rejected = await asyncio.shield(task)
                                link_identity = extract_place_identity_from_url(
                                    link
                                ) or _place_link_key(link)
                                if not isinstance(place, dict):
                                    if issues is not None and not rejected:
                                        failed_identities.add(link_identity)
                                    continue

                                place_id = place.get("place_id")
                                place_identity = (
                                    place_id
                                    if isinstance(place_id, str) and place_id
                                    else link_identity
                                )
                                failed_identities.discard(link_identity)
                                failed_identities.discard(place_identity)
                                original = identities.get(place_identity)
                                if original is None:
                                    original = identities.get(link_identity)
                                if original is None:
                                    original = {**place, "query": query}
                                    places.append(original)
                                else:
                                    for field, value in place.items():
                                        if (
                                            original.get(field) is None
                                            and value is not None
                                        ):
                                            original[field] = value
                                identities[link_identity] = original
                                identities[place_identity] = original
                        return True

                    async with query_semaphore:
                        context = None
                        links_collector: set[str] = set()
                        links = links_collector
                        watchdog = asyncio.timeout(
                            query_timeout + watchdog_grace_period
                        )
                        loop = asyncio.get_running_loop()

                        async def process_during_search(candidates: list[str]) -> bool:
                            search_deadline = watchdog.when()
                            detail_started = loop.time()
                            watchdog.reschedule(None)
                            try:
                                return await process_candidates(candidates)
                            finally:
                                if search_deadline is not None:
                                    watchdog.reschedule(
                                        search_deadline + loop.time() - detail_started
                                    )

                        try:
                            context = await _create_browser_context(
                                browser=browser,
                                geo_coordinates=geo_coordinates,
                                lang=lang,
                                proxy=proxy_rotator.get(),
                                static_cache_dir=static_cache_dir,
                                timezone_id=timezone_id,
                            )
                            async with watchdog:
                                links = await _get_place_urls(
                                    context=context,
                                    max_places=max_places,
                                    query=query.replace("_", " "),
                                    geo_coordinates=geo_coordinates,
                                    zoom=zoom,
                                    lang=lang,
                                    range_limit=range_limit,
                                    proxy_rotator=proxy_rotator,
                                    query_timeout=query_timeout,
                                    links_collector=links_collector,
                                    navigation_timeout=navigation_timeout,
                                    captcha_timeout=captcha_timeout,
                                    max_consecutive_empty_scrolls=max_consecutive_empty_scrolls,
                                    max_consecutive_out_of_range_scrolls=max_consecutive_out_of_range_scrolls,
                                    max_scroll_attempts_without_new_links=max_scroll_attempts_without_new_links,
                                    process_candidates=process_during_search,
                                    query_issues=issues,
                                )
                        except TimeoutError:
                            logger.warning(
                                "Hard search deadline reached for %r.", query
                            )
                            if issues is not None:
                                issues.append("hard search deadline reached")
                        except Exception as e:  # noqa: BLE001
                            logger.error(
                                "Could not collect place links for %r: %s", query, e
                            )
                            if issues is not None:
                                issues.append(f"search failed: {type(e).__name__}")
                        finally:
                            await _safe_close_context(context)

                    # The collector also retains candidates discovered just before a deadline.
                    await process_candidates(sorted(links))
                    if issues is not None and failed_identities:
                        issues.append(
                            f"could not extract {len(failed_identities)} place detail(s)"
                        )
                    return places

                tasks = [
                    asyncio.create_task(run_get_urls(i, query))
                    for i, query in enumerate(query_list)
                ]
                try:
                    places_by_query = await _gather_tasks(tasks)
                finally:
                    pending_details = [
                        task for task in detail_tasks.values() if not task.done()
                    ]
                    for task in pending_details:
                        task.cancel()
                    await asyncio.gather(*pending_details, return_exceptions=True)

                for query, places in zip(query_list, places_by_query, strict=True):
                    if isinstance(places, BaseException):
                        logger.error(
                            "Unexpected detail error for %r: %s", query, places
                        )
                        issues = query_issues.get(query)
                        if issues is not None:
                            issues.append(f"query failed: {type(places).__name__}")
                    else:
                        results.extend(places)
                logger.info("Successfully collected %d places.", len(results))

        except asyncio.CancelledError:
            logger.info("Scraping cancelled by user/task cancellation.")
            raise
        except PlaywrightTimeoutError:
            logger.error("Playwright timeout error during scraping process.")
            for issues in query_issues.values():
                issues.append("browser operation timed out")
        except Exception as e:
            if (
                browser is None
                and isinstance(e, PlaywrightError)
                and "executable doesn't exist at" in str(e).lower().replace("’", "'")
            ):
                raise RuntimeError(
                    "Chromium for Playwright is not installed. "
                    "Run `uv run playwright install chromium` "
                    "(or `python -m playwright install chromium`) before scraping."
                ) from e
            logger.error("Unexpected error during scraping: %s", e)
            for issues in query_issues.values():
                issues.append(f"scraping failed: {type(e).__name__}")
        finally:
            await _safe_close_browser(browser)

    frame = format_places_dataframe(
        results, flatten=flatten, fields=fields, include_query=True
    )
    if on_query_incomplete is not None:
        for query, issues in query_issues.items():
            if issues:
                on_query_incomplete(query, tuple(dict.fromkeys(issues)))
    return frame
