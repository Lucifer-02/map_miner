import asyncio
import json
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import polars as pl
from geopy.point import Point
from playwright.async_api import Error as PlaywrightError

from map_miner.recaptcha_solver import RecaptchaBlockedError
from map_miner.scraper import (
    BLOCKED_RESOURCE_TYPES,
    BLOCKED_URL_PATTERNS,
    CONSENT_BUTTON_REGEX,
    DEFAULT_CACHE_DIR,
    DEFAULT_CAPTCHA_TIMEOUT,
    DEFAULT_DISK_CACHE_SIZE,
    DEFAULT_PROXY_BYPASS,
    FEED_FALLBACK_SELECTORS,
    LAUNCH_ARGS,
    MAX_CONSECUTIVE_EMPTY_SCROLLS,
    PreviewInterceptor,
    ProxyRotator,
    create_browser_context,
    extract_coordinates_from_url,
    get_place_urls,
    handle_captcha_if_present,
    is_preview_response_for_link,
    make_place_url,
    scrape_google_maps,
    scrape_query_spa,
)


def test_make_place_url():
    point = Point(20.985322, 105.781289)
    url = make_place_url(
        query="cafe hà đông", geo_coordinates=point, zoom=18, lang="vi"
    )
    assert "https://www.google.com/maps/search/" in url
    assert "cafe+h%C3%A0+%C4%91%C3%B4ng" in url
    assert "@20.985322,105.781289,18z" in url
    assert "hl=vi" in url


def test_consent_regex():
    matches = [
        "Reject all",
        "reject all",
        "TỪ CHỐI TẤT CẢ",
        "Từ chối tất cả",
        "Alle ablehnen",
        "Tout refuser",
        "Rechazar todo",
        "Rifiuta tutto",
        "Accept all",
        "Chấp nhận tất cả",
        "I agree",
        "Tôi đồng ý",
    ]
    for text in matches:
        assert CONSENT_BUTTON_REGEX.search(text) is not None

    non_matches = ["Submit", "Search", "Next", "Close"]
    for text in non_matches:
        assert CONSENT_BUTTON_REGEX.search(text) is None


def test_blocked_resources_and_urls():
    assert "image" in BLOCKED_RESOURCE_TYPES
    assert "media" in BLOCKED_RESOURCE_TYPES
    assert "font" in BLOCKED_RESOURCE_TYPES
    assert any("google-analytics" in p for p in BLOCKED_URL_PATTERNS)
    assert any("/maps/vt" in p for p in BLOCKED_URL_PATTERNS)


def test_feed_selectors():
    assert '[role="feed"]' in FEED_FALLBACK_SELECTORS


def test_is_preview_response_for_link_exact_hex():
    canonical_link = "https://www.google.com/maps/place/Cafe+A/data=!4m2!3m1!1s0x3135accc43918e95:0x8be8f32092b12b1d"

    # Matching preview URL with same hex ID
    mock_resp_match = MagicMock()
    mock_resp_match.url = "https://www.google.com/maps/preview/place/Cafe+A/.../data=!1s0x3135accc43918e95:0x8be8f32092b12b1d"
    mock_resp_match.status = 200
    mock_resp_match.ok = True
    assert is_preview_response_for_link(mock_resp_match, canonical_link) is True

    # Delayed response from previous place B with different hex ID (Race condition prevention)
    mock_resp_delayed = MagicMock()
    mock_resp_delayed.url = "https://www.google.com/maps/preview/place/Cafe+B/.../data=!1s0x12345678abcdef01:0x9876543210fedcba"
    mock_resp_delayed.status = 200
    mock_resp_delayed.ok = True
    assert is_preview_response_for_link(mock_resp_delayed, canonical_link) is False

    # Status != 200
    mock_resp_error = MagicMock()
    mock_resp_error.url = mock_resp_match.url
    mock_resp_error.status = 500
    mock_resp_error.ok = False
    assert is_preview_response_for_link(mock_resp_error, canonical_link) is False

    # Non preview URL
    mock_resp_other = MagicMock()
    mock_resp_other.url = "https://www.google.com/maps/search/cafe"
    mock_resp_other.status = 200
    mock_resp_other.ok = True
    assert is_preview_response_for_link(mock_resp_other, canonical_link) is False


def test_is_preview_response_for_link_no_hex():
    canonical_link = "https://www.google.com/maps/place/Simple+Cafe/@21.0,105.7,17z"
    mock_resp = MagicMock()
    mock_resp.url = (
        "https://www.google.com/maps/preview/place/Simple+Cafe/@21.0,105.7,17z"
    )
    mock_resp.status = 200
    mock_resp.ok = True
    assert is_preview_response_for_link(mock_resp, canonical_link) is True


def test_is_preview_response_for_link_percent_encoding():
    # Test lowercase %3a in canonical_link
    canonical_encoded_lower = "https://www.google.com/maps/place/Cafe+A/data=!1s0x3135accc43918e95%3a0x8be8f32092b12b1d"
    mock_resp = MagicMock()
    mock_resp.url = "https://www.google.com/maps/preview/place/Cafe+A/data=!1s0x3135accc43918e95:0x8be8f32092b12b1d"
    mock_resp.status = 200
    mock_resp.ok = True
    assert is_preview_response_for_link(mock_resp, canonical_encoded_lower) is True

    # Test uppercase %3A in response URL
    canonical_unencoded = "https://www.google.com/maps/place/Cafe+A/data=!1s0x3135accc43918e95:0x8be8f32092b12b1d"
    mock_resp_upper = MagicMock()
    mock_resp_upper.url = "https://www.google.com/maps/preview/place/Cafe+A/data=!1s0x3135accc43918e95%3A0x8be8f32092b12b1d"
    mock_resp_upper.status = 200
    mock_resp_upper.ok = True
    assert is_preview_response_for_link(mock_resp_upper, canonical_unencoded) is True

    # Test both canonical and response URL containing percent-encoding (%3a and %3A)
    mock_resp_lower = MagicMock()
    mock_resp_lower.url = "https://www.google.com/maps/preview/place/Cafe+A/data=!1s0x3135accc43918e95%3a0x8be8f32092b12b1d"
    mock_resp_lower.status = 200
    mock_resp_lower.ok = True
    assert (
        is_preview_response_for_link(mock_resp_lower, canonical_encoded_lower) is True
    )


def test_preview_interceptor_validates_structure_not_magic_length():
    async def _run():
        page = MagicMock()
        interceptor = PreviewInterceptor(page)

        # 1. Payload > 1500 characters but NOT valid JSON structure -> must NOT be accepted
        invalid_large_payload = ")]}'\n" + "A" * 2000
        mock_resp_large_invalid = AsyncMock()
        mock_resp_large_invalid.url = "https://www.google.com/maps/preview/place/Test"
        mock_resp_large_invalid.text.return_value = invalid_large_payload

        await interceptor._handle_response(mock_resp_large_invalid)
        assert interceptor.preview_json is None
        assert not interceptor.preview_event.is_set()

        # 2. Payload < 1500 characters but structurally VALID preview JSON -> MUST be accepted
        valid_small_blob = [None] * 20
        valid_small_blob[11] = "Tiny Cafe"
        valid_outer_list = [None, [], None, None, None, None, valid_small_blob]
        valid_small_payload = ")]}'\n" + json.dumps(valid_outer_list)
        assert len(valid_small_payload) < 500  # Well under 1500 chars

        mock_resp_small_valid = AsyncMock()
        mock_resp_small_valid.url = "https://www.google.com/maps/preview/place/Test"
        mock_resp_small_valid.text.return_value = valid_small_payload

        await interceptor._handle_response(mock_resp_small_valid)
        assert interceptor.preview_json == valid_small_payload
        assert interceptor.preview_event.is_set()

        # Reset
        interceptor.reset()
        assert interceptor.preview_json is None
        assert not interceptor.preview_event.is_set()

    asyncio.run(_run())


def test_polars_schema_infer_length_none():
    # 105 rows where first 100 rows have None for a nested struct/dict column,
    # and 101st row has an actual dictionary.
    rows = []
    for i in range(100):
        rows.append(
            {
                "name": f"Place {i}",
                "opening_hours": None,
                "photos": None,
            }
        )
    for i in range(100, 105):
        rows.append(
            {
                "name": f"Place {i}",
                "opening_hours": {"Monday": "8:00 AM - 10:00 PM"},
                "photos": ["https://example.com/photo.jpg"],
            }
        )

    # pl.from_dicts with infer_schema_length=None scans full dataset and succeeds
    df = pl.from_dicts(rows, infer_schema_length=None)
    assert len(df) == 105
    assert "opening_hours" in df.columns
    assert "photos" in df.columns
    assert df["opening_hours"][100] is not None


def test_spa_processed_links_only_on_successful_click():
    """
    Verifies that in scrape_query_spa:
    - If clicking a card fails (PlaywrightError), the link is NOT added to processed_links,
      allowing it to be retried on subsequent scrolls.
    - If clicking succeeds and response is caught, it IS added to processed_links.
    """

    async def _run():
        from map_miner.scraper import scrape_query_spa

        # Setup mocks
        mock_context = AsyncMock()
        mock_page = AsyncMock()
        mock_page.is_closed = MagicMock(return_value=False)
        mock_page.close = AsyncMock()
        mock_context.new_page.return_value = mock_page

        mock_page.url = "https://www.google.com/maps/search/cafe"
        mock_page.content.return_value = "<html></html>"
        mock_page.wait_for_selector.return_value = None

        valid_blob = [None] * 20
        valid_blob[11] = "Recovered Cafe"
        valid_outer = [None, [], None, None, None, None, valid_blob]
        valid_preview = ")]}'\n" + json.dumps(valid_outer)

        mock_resp = AsyncMock()
        mock_resp.url = "https://www.google.com/maps/preview/place/Cafe/data=!1s0x1:0x2"
        mock_resp.status = 200
        mock_resp.ok = True
        mock_resp.text.return_value = valid_preview

        class MockExpectResponse:
            def __init__(self, resp):
                self.value = asyncio.Future()
                self.value.set_result(resp)

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                if exc_val:
                    return False
                return None

        mock_page.expect_response = MagicMock(
            side_effect=lambda pred, timeout: MockExpectResponse(mock_resp)
        )

        # Element 1: click fails
        el_fail = AsyncMock()
        el_fail.get_attribute.return_value = (
            "https://www.google.com/maps/place/Cafe/data=!1s0x1:0x2"
        )
        el_fail.evaluate.side_effect = PlaywrightError("Element obscured")
        el_fail.click.side_effect = PlaywrightError("Element obscured")

        # Element 2: same canonical link, but click succeeds
        el_succeed = AsyncMock()
        el_succeed.get_attribute.return_value = (
            "https://www.google.com/maps/place/Cafe/data=!1s0x1:0x2"
        )
        el_succeed.evaluate.return_value = None

        call_count = 0

        def mock_feed_locator(selector):
            nonlocal call_count
            loc = AsyncMock()
            loc.count.return_value = 0
            loc.first.is_visible.return_value = False
            if 'a[href*="/maps/place/"]' in selector:
                if call_count == 0:
                    call_count += 1
                    loc.all.return_value = [el_fail]
                    loc.evaluate_all.return_value = [
                        "https://www.google.com/maps/place/Cafe/data=!1s0x1:0x2"
                    ]
                else:
                    call_count += 1
                    loc.all.return_value = [el_succeed]
                    loc.evaluate_all.return_value = [
                        "https://www.google.com/maps/place/Cafe/data=!1s0x1:0x2"
                    ]
            return loc

        mock_page.locator = MagicMock(side_effect=mock_feed_locator)

        eval_call = 0

        async def mock_evaluate(script, *args):
            nonlocal eval_call
            eval_call += 1
            return eval_call * 500

        mock_page.evaluate.side_effect = mock_evaluate

        results = await scrape_query_spa(
            context=mock_context,
            query="cafe",
            geo_coordinates=Point(21.0, 105.8),
            zoom=16,
            max_places=1,
        )

        assert len(results) == 1
        assert results[0]["name"] == "Recovered Cafe"

    asyncio.run(_run())


def test_spa_field_filtering_few_fields_without_name():
    """
    Verifies that requesting fewer than 3 fields and without 'name'
    (e.g. fields=['latitude', 'longitude']) is NOT dropped by scrape_query_spa.
    """

    async def _run():
        from map_miner.scraper import scrape_query_spa

        mock_context = AsyncMock()
        mock_page = AsyncMock()
        mock_page.is_closed = MagicMock(return_value=False)
        mock_page.close = AsyncMock()
        mock_context.new_page.return_value = mock_page

        mock_page.url = "https://www.google.com/maps/search/cafe"
        mock_page.content.return_value = "<html></html>"
        mock_page.wait_for_selector.return_value = None

        valid_blob = [None] * 20
        valid_blob[9] = [None, None, 21.0285, 105.8542]
        valid_blob[11] = "Sample Cafe"
        valid_outer = [None, [], None, None, None, None, valid_blob]
        valid_preview = ")]}'\n" + json.dumps(valid_outer)

        mock_resp = AsyncMock()
        mock_resp.url = "https://www.google.com/maps/preview/place/Cafe/data=!1s0x1:0x2"
        mock_resp.status = 200
        mock_resp.ok = True
        mock_resp.text.return_value = valid_preview

        class MockExpectResponse:
            def __init__(self, resp):
                self.value = asyncio.Future()
                self.value.set_result(resp)

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                return None

        mock_page.expect_response = MagicMock(
            side_effect=lambda pred, timeout: MockExpectResponse(mock_resp)
        )

        el = AsyncMock()
        el.get_attribute.return_value = (
            "https://www.google.com/maps/place/Cafe/data=!1s0x1:0x2"
        )
        el.evaluate.return_value = None

        def mock_locator(selector):
            loc = AsyncMock()
            loc.count.return_value = 0
            loc.first.is_visible.return_value = False
            if 'a[href*="/maps/place/"]' in selector:
                loc.all.return_value = [el]
                loc.evaluate_all.return_value = [
                    "https://www.google.com/maps/place/Cafe/data=!1s0x1:0x2"
                ]
            return loc

        mock_page.locator = MagicMock(side_effect=mock_locator)

        mock_page.evaluate.return_value = 500

        results = await scrape_query_spa(
            context=mock_context,
            query="cafe",
            geo_coordinates=Point(21.0, 105.8),
            zoom=16,
            max_places=1,
            fields=["latitude", "longitude"],
        )

        assert len(results) == 1
        assert "latitude" in results[0]
        assert "longitude" in results[0]
        assert results[0]["latitude"] == 21.0285
        assert results[0]["longitude"] == 105.8542
        assert "name" not in results[0]

    asyncio.run(_run())


def test_create_browser_context_with_proxy():
    async def _run():
        browser = AsyncMock()
        mock_context = AsyncMock()
        browser.new_context.return_value = mock_context

        proxy_config = {
            "server": "http://gate.decodo.com:10000",
            "username": "user",
            "password": "pass",
        }
        point = Point(21.018785, 105.830415)
        context = await create_browser_context(
            browser=browser,
            geo_coordinates=point,
            lang="vi",
            proxy=proxy_config,
        )

        assert context == mock_context
        browser.new_context.assert_awaited_once()
        kwargs = browser.new_context.await_args.kwargs
        assert kwargs["proxy"] == proxy_config
        assert kwargs["locale"] == "vi"
        assert kwargs["geolocation"] == {
            "latitude": point.latitude,
            "longitude": point.longitude,
        }
        assert kwargs["permissions"] == ["geolocation"]
        assert "width" in kwargs["viewport"]
        assert "height" in kwargs["viewport"]
        mock_context.add_init_script.assert_awaited_once()
        mock_context.route.assert_awaited_once()

    asyncio.run(_run())


def test_create_browser_context_with_proxy_bypass():
    async def _run():
        browser = AsyncMock()
        mock_context = AsyncMock()
        browser.new_context.return_value = mock_context

        proxy_config = {
            "server": "http://gate.decodo.com:10000",
            "username": "user",
            "password": "pass",
            "bypass": DEFAULT_PROXY_BYPASS,
        }
        context = await create_browser_context(
            browser=browser,
            proxy=proxy_config,
        )

        assert context == mock_context
        browser.new_context.assert_awaited_once()
        kwargs = browser.new_context.await_args.kwargs
        assert kwargs["proxy"] == proxy_config
        assert kwargs["proxy"]["bypass"] == DEFAULT_PROXY_BYPASS

    asyncio.run(_run())


def test_create_browser_context_without_proxy():
    async def _run():
        browser = AsyncMock()
        mock_context = AsyncMock()
        browser.new_context.return_value = mock_context

        context = await create_browser_context(
            browser=browser,
            geo_coordinates=None,
            lang="en",
            proxy=None,
        )

        assert context == mock_context
        kwargs = browser.new_context.await_args.kwargs
        assert "proxy" not in kwargs
        assert "geolocation" not in kwargs
        assert kwargs["locale"] == "en"

    asyncio.run(_run())


def test_scrape_google_maps_context_isolation_and_rotation_spa():
    async def _run():
        from unittest.mock import patch

        created_contexts = []

        class FakeBrowser:
            def __init__(self):
                self.is_connected = MagicMock(return_value=True)
                self.close = AsyncMock()

            async def new_context(self, **kwargs):
                ctx = AsyncMock()
                ctx._kwargs = kwargs
                ctx.close = AsyncMock()
                created_contexts.append(ctx)
                return ctx

        fake_browser = FakeBrowser()
        mock_playwright = AsyncMock()
        mock_playwright.chromium.launch.return_value = fake_browser

        class MockPlaywrightContext:
            async def __aenter__(self):
                return mock_playwright

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                return None

        proxies = [
            {"server": "http://proxy1:8080"},
            {"server": "http://proxy2:8080"},
        ]

        async def mock_spa(context, query, **kwargs):
            return [{"name": f"Place for {query}", "link": f"http://maps/{query}"}]

        with (
            patch(
                "map_miner.scraper.async_playwright",
                return_value=MockPlaywrightContext(),
            ),
            patch("map_miner.scraper.scrape_query_spa", side_effect=mock_spa),
        ):
            df = await scrape_google_maps(
                queries={"cafe", "restaurant", "hospital"},
                geo_coordinates=Point(21.0, 105.8),
                zoom=16,
                proxy=proxies,
                use_spa=True,
                stagger_delay=0,
            )

        assert len(df) == 3
        # Chromium launched without browser-level proxy
        launch_kwargs = mock_playwright.chromium.launch.await_args.kwargs
        assert "proxy" not in launch_kwargs

        # Each query got its own isolated context
        assert len(created_contexts) == 3

        # Check proxy rotation across contexts
        assigned_proxies = [ctx._kwargs.get("proxy") for ctx in created_contexts]
        assert {"server": "http://proxy1:8080"} in assigned_proxies
        assert {"server": "http://proxy2:8080"} in assigned_proxies

        # Zero leaks: all contexts were properly closed
        for ctx in created_contexts:
            ctx.close.assert_awaited_once()

        # Browser closed
        fake_browser.close.assert_awaited_once()

    asyncio.run(_run())


def test_scrape_google_maps_context_isolation_fallback_mode():
    async def _run():
        from unittest.mock import patch

        created_contexts = []

        class FakeBrowser:
            def __init__(self):
                self.is_connected = MagicMock(return_value=True)
                self.close = AsyncMock()

            async def new_context(self, **kwargs):
                ctx = AsyncMock()
                ctx._kwargs = kwargs
                ctx.close = AsyncMock()
                created_contexts.append(ctx)
                return ctx

        fake_browser = FakeBrowser()
        mock_playwright = AsyncMock()
        mock_playwright.chromium.launch.return_value = fake_browser

        class MockPlaywrightContext:
            async def __aenter__(self):
                return mock_playwright

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                return None

        async def mock_get_urls(context, query, **kwargs):
            return {f"http://maps.google.com/place/{query}_1"}

        async def mock_process(
            context, link, semaphore, count, total, fields=None, max_retries=2, **kwargs
        ):
            return {"name": f"Place {link}", "link": link}

        with (
            patch(
                "map_miner.scraper.async_playwright",
                return_value=MockPlaywrightContext(),
            ),
            patch("map_miner.scraper.get_place_urls", side_effect=mock_get_urls),
            patch("map_miner.scraper.process_link", side_effect=mock_process),
        ):
            df = await scrape_google_maps(
                queries={"cafe", "gym"},
                geo_coordinates=Point(21.0, 105.8),
                zoom=16,
                proxy=[
                    {"server": "http://p1:8080"},
                    {"server": "http://p2:8080"},
                ],
                use_spa=False,
                stagger_delay=0,
            )

        assert len(df) == 2
        # 2 queries for get_place_urls + 2 detail tasks for process_link = 4 contexts
        assert len(created_contexts) == 4
        for ctx in created_contexts:
            ctx.close.assert_awaited_once()

    asyncio.run(_run())


def test_scrape_google_maps_context_cleanup_on_error():
    """Verifies that if a query fails, its context is still safely closed (Zero Leaks)."""

    async def _run():
        from unittest.mock import patch

        created_contexts = []

        class FakeBrowser:
            def __init__(self):
                self.is_connected = MagicMock(return_value=True)
                self.close = AsyncMock()

            async def new_context(self, **kwargs):
                ctx = AsyncMock()
                ctx._kwargs = kwargs
                ctx.close = AsyncMock()
                created_contexts.append(ctx)
                return ctx

        fake_browser = FakeBrowser()
        mock_playwright = AsyncMock()
        mock_playwright.chromium.launch.return_value = fake_browser

        class MockPlaywrightContext:
            async def __aenter__(self):
                return mock_playwright

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                return None

        async def mock_spa_fail(context, query, **kwargs):
            raise PlaywrightError("Browser crashed or tab disconnected")

        with (
            patch(
                "map_miner.scraper.async_playwright",
                return_value=MockPlaywrightContext(),
            ),
            patch("map_miner.scraper.scrape_query_spa", side_effect=mock_spa_fail),
        ):
            df = await scrape_google_maps(
                queries={"cafe"},
                geo_coordinates=Point(21.0, 105.8),
                zoom=16,
                proxy={"server": "http://gate:10000"},
                use_spa=True,
            )

        assert len(df) == 0
        assert len(created_contexts) == 1
        # Crucial test: context MUST be closed even when scrape_query_spa crashes
        created_contexts[0].close.assert_awaited_once()

    asyncio.run(_run())


def test_scrape_google_maps_with_cache_dir(tmp_path):
    """Verifies that --disk-cache-dir and --disk-cache-size flags are passed to chromium.launch."""

    async def _run():
        from unittest.mock import patch

        custom_cache = tmp_path / "test_cache"
        assert not custom_cache.exists()

        class FakeBrowser:
            def __init__(self):
                self.is_connected = MagicMock(return_value=True)
                self.close = AsyncMock()

            async def new_context(self, **kwargs):
                ctx = AsyncMock()
                ctx.close = AsyncMock()
                return ctx

        fake_browser = FakeBrowser()
        mock_playwright = AsyncMock()
        mock_playwright.chromium.launch.return_value = fake_browser

        class MockPlaywrightContext:
            async def __aenter__(self):
                return mock_playwright

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                return None

        with (
            patch(
                "map_miner.scraper.async_playwright",
                return_value=MockPlaywrightContext(),
            ),
            patch(
                "map_miner.scraper.scrape_query_spa", return_value=[{"name": "Cafe A"}]
            ),
        ):
            df = await scrape_google_maps(
                queries={"cafe"},
                geo_coordinates=Point(21.0, 105.8),
                zoom=16,
                cache_dir=custom_cache,
            )

        assert len(df) == 1
        assert custom_cache.exists()
        mock_playwright.chromium.launch.assert_awaited_once()
        launch_kwargs = mock_playwright.chromium.launch.call_args.kwargs
        launch_args = launch_kwargs.get("args", [])
        assert any(
            f"--disk-cache-dir={custom_cache.resolve()}" in arg for arg in launch_args
        )
        assert f"--disk-cache-size={DEFAULT_DISK_CACHE_SIZE}" in launch_args

    asyncio.run(_run())


def test_scrape_google_maps_without_cache_dir():
    """Verifies that disk cache flags are omitted when cache_dir is None."""

    async def _run():
        from unittest.mock import patch

        class FakeBrowser:
            def __init__(self):
                self.is_connected = MagicMock(return_value=True)
                self.close = AsyncMock()

            async def new_context(self, **kwargs):
                ctx = AsyncMock()
                ctx.close = AsyncMock()
                return ctx

        fake_browser = FakeBrowser()
        mock_playwright = AsyncMock()
        mock_playwright.chromium.launch.return_value = fake_browser

        class MockPlaywrightContext:
            async def __aenter__(self):
                return mock_playwright

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                return None

        with (
            patch(
                "map_miner.scraper.async_playwright",
                return_value=MockPlaywrightContext(),
            ),
            patch("map_miner.scraper.scrape_query_spa", return_value=[]),
        ):
            await scrape_google_maps(
                queries={"cafe"},
                geo_coordinates=Point(21.0, 105.8),
                zoom=16,
                cache_dir=None,
            )

        mock_playwright.chromium.launch.assert_awaited_once()
        launch_kwargs = mock_playwright.chromium.launch.call_args.kwargs
        launch_args = launch_kwargs.get("args", [])
        assert not any("--disk-cache-dir" in arg for arg in launch_args)
        assert not any("--disk-cache-size" in arg for arg in launch_args)

    asyncio.run(_run())


def test_scrape_google_maps_default_cache_dir():
    """Verifies that default cache_dir is DEFAULT_CACHE_DIR and creates the directory."""

    async def _run():
        from unittest.mock import patch

        class FakeBrowser:
            def __init__(self):
                self.is_connected = MagicMock(return_value=True)
                self.close = AsyncMock()

            async def new_context(self, **kwargs):
                ctx = AsyncMock()
                ctx.close = AsyncMock()
                return ctx

        fake_browser = FakeBrowser()
        mock_playwright = AsyncMock()
        mock_playwright.chromium.launch.return_value = fake_browser

        class MockPlaywrightContext:
            async def __aenter__(self):
                return mock_playwright

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                return None

        with (
            patch(
                "map_miner.scraper.async_playwright",
                return_value=MockPlaywrightContext(),
            ),
            patch("map_miner.scraper.scrape_query_spa", return_value=[]),
        ):
            await scrape_google_maps(
                queries={"cafe"},
                geo_coordinates=Point(21.0, 105.8),
                zoom=16,
            )

        mock_playwright.chromium.launch.assert_awaited_once()
        launch_kwargs = mock_playwright.chromium.launch.call_args.kwargs
        launch_args = launch_kwargs.get("args", [])
        resolved_default = DEFAULT_CACHE_DIR.resolve()
        assert any(f"--disk-cache-dir={resolved_default}" in arg for arg in launch_args)
        assert f"--disk-cache-size={DEFAULT_DISK_CACHE_SIZE}" in launch_args

    asyncio.run(_run())


def test_blocked_url_patterns_includes_telemetry():
    """Verifies that BLOCKED_URL_PATTERNS includes additional telemetry and photometa patterns."""
    assert "client_204" in BLOCKED_URL_PATTERNS
    assert "cspreport" in BLOCKED_URL_PATTERNS
    assert "/maps/photometa" in BLOCKED_URL_PATTERNS


def test_extract_coordinates_from_url():
    """Verifies parsing coordinates from various Google Maps URL patterns and invalid inputs."""
    # 1. Feed / detail link protobuf format (!3d<lat>...!4d<lon>)
    url_feed = "https://www.google.com/maps/place/Cafe+A/data=!4m7!3m6!1s0x3135ac:0x123!8m2!3d21.028511!4d105.854222!16s%2Fg%2F11"
    assert extract_coordinates_from_url(url_feed) == (21.028511, 105.854222)

    url_negative = (
        "https://www.google.com/maps/place/Sydney/data=!3d-33.8688197!4d151.2092955"
    )
    assert extract_coordinates_from_url(url_negative) == (-33.8688197, 151.2092955)

    url_both_negative = (
        "https://www.google.com/maps/place/Lima/data=!3d-12.0464!4d-77.0428"
    )
    assert extract_coordinates_from_url(url_both_negative) == (-12.0464, -77.0428)

    url_integer = "https://www.google.com/maps/place/Zero/data=!3d0!4d0"
    assert extract_coordinates_from_url(url_integer) == (0.0, 0.0)

    # 2. Viewport format (@<lat>,<lon>)
    url_viewport = (
        "https://www.google.com/maps/place/Cafe+B/@20.985322,105.781289,17z/data=..."
    )
    assert extract_coordinates_from_url(url_viewport) == (20.985322, 105.781289)

    url_viewport_neg = (
        "https://www.google.com/maps/place/Melbourne/@-37.8136,144.9631,14z"
    )
    assert extract_coordinates_from_url(url_viewport_neg) == (-37.8136, 144.9631)

    # 3. Preference: !3d!4d takes precedence over @ viewport center
    url_combo = "https://www.google.com/maps/place/Cafe/@20.000,100.000,15z/data=!3d21.028511!4d105.854222"
    assert extract_coordinates_from_url(url_combo) == (21.028511, 105.854222)

    # 4. URL-encoded format
    url_encoded = (
        "https://www.google.com/maps/place/Cafe/data=%213d21.028511%214d105.854222"
    )
    assert extract_coordinates_from_url(url_encoded) == (21.028511, 105.854222)

    # 5. Invalid / missing inputs
    assert (
        extract_coordinates_from_url("https://www.google.com/maps/search/cafe") is None
    )
    assert (
        extract_coordinates_from_url("https://www.google.com/maps/place/Cafe") is None
    )
    assert extract_coordinates_from_url("") is None
    assert extract_coordinates_from_url(cast(str, None)) is None
    assert extract_coordinates_from_url(cast(str, 12345)) is None
    assert (
        extract_coordinates_from_url(
            "https://www.google.com/maps/place/data=!3d95.0!4d200.0"
        )
        is None
    )


def test_spa_early_drop():
    """
    Verifies that places exceeding range_limit are early-dropped before clicking
    and before waiting for XHR preview.
    """

    async def _run():
        mock_context = AsyncMock()
        mock_page = AsyncMock()
        mock_page.is_closed = MagicMock(return_value=False)
        mock_page.close = AsyncMock()
        mock_context.new_page.return_value = mock_page

        mock_page.url = "https://www.google.com/maps/search/cafe"
        mock_page.content.return_value = "<html></html>"
        mock_page.wait_for_selector.return_value = None

        valid_blob = [None] * 20
        valid_blob[11] = "Near Cafe"
        valid_outer = [None, [], None, None, None, None, valid_blob]
        valid_preview = ")]}'\n" + json.dumps(valid_outer)

        mock_resp = AsyncMock()
        mock_resp.url = (
            "https://www.google.com/maps/preview/place/Near+Cafe/data=!1s0x1:0x1"
        )
        mock_resp.status = 200
        mock_resp.ok = True
        mock_resp.text.return_value = valid_preview

        class MockExpectResponse:
            def __init__(self, resp):
                self.value = asyncio.Future()
                self.value.set_result(resp)

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                return None

        mock_page.expect_response = MagicMock(
            side_effect=lambda pred, timeout: MockExpectResponse(mock_resp)
        )

        # Place 1: in range (~150m from (21.0, 105.8))
        el_near = AsyncMock()
        el_near.get_attribute.return_value = "https://www.google.com/maps/place/Near+Cafe/data=!1s0x1:0x1!8m2!3d21.0010!4d105.8010"
        el_near.evaluate.return_value = None

        # Place 2: out of range (~7.5km from (21.0, 105.8))
        el_far = AsyncMock()
        el_far.get_attribute.return_value = "https://www.google.com/maps/place/Far+Cafe/data=!1s0x2:0x2!8m2!3d21.0500!4d105.8500"
        el_far.evaluate.return_value = None

        def mock_feed_locator(selector):
            loc = AsyncMock()
            loc.count.return_value = 0
            loc.first.is_visible.return_value = False
            if 'a[href*="/maps/place/"]' in selector:
                loc.all.return_value = [el_near, el_far]
                loc.evaluate_all.return_value = [
                    "https://www.google.com/maps/place/Near+Cafe/data=!1s0x1:0x1!8m2!3d21.0010!4d105.8010",
                    "https://www.google.com/maps/place/Far+Cafe/data=!1s0x2:0x2!8m2!3d21.0500!4d105.8500",
                ]
            return loc

        mock_page.locator = MagicMock(side_effect=mock_feed_locator)

        from unittest.mock import patch

        with patch("map_miner.scraper.is_feed_at_end", AsyncMock(return_value=True)):
            results = await scrape_query_spa(
                context=mock_context,
                query="cafe",
                geo_coordinates=Point(21.0000, 105.8000),
                zoom=16,
                max_places=10,
                range_limit=500.0,
            )

        assert len(results) == 1
        assert results[0]["name"] == "Near Cafe"

        # Place 1 was clicked
        el_near.evaluate.assert_awaited_once()

        # Place 2 was dropped: evaluate (click) was NOT called
        el_far.evaluate.assert_not_awaited()
        el_far.click.assert_not_awaited()

    asyncio.run(_run())


def test_spa_no_early_exit_on_consecutive_out_of_range():
    """
    Verifies that when consecutive out-of-range places appear in SPA feed,
    they are early-dropped without prematurely halting feed scrolling.
    """

    async def _run():
        mock_context = AsyncMock()
        mock_page = AsyncMock()
        mock_page.is_closed = MagicMock(return_value=False)
        mock_page.close = AsyncMock()
        mock_context.new_page.return_value = mock_page

        mock_page.url = "https://www.google.com/maps/search/cafe"
        mock_page.content.return_value = "<html></html>"
        mock_page.wait_for_selector.return_value = None

        # 4 consecutive places all far out of range (> 500m)
        far_elements = []
        far_urls = []
        for i in range(4):
            el = AsyncMock()
            url = f"https://www.google.com/maps/place/Far{i}/data=!1s0x{i}:0x{i}!8m2!3d21.05{i}0!4d105.85{i}0"
            el.get_attribute.return_value = url
            el.evaluate.return_value = None
            far_elements.append(el)
            far_urls.append(url)

        def mock_feed_locator(selector):
            loc = AsyncMock()
            loc.count.return_value = 0
            loc.first.is_visible.return_value = False
            if 'a[href*="/maps/place/"]' in selector:
                loc.all.return_value = far_elements
                loc.evaluate_all.return_value = far_urls
            return loc

        mock_page.locator = MagicMock(side_effect=mock_feed_locator)

        from unittest.mock import patch

        mock_scroll = AsyncMock()
        with (
            patch("map_miner.scraper.scroll_feed", mock_scroll),
            patch("map_miner.scraper.is_feed_at_end", AsyncMock(return_value=True)),
        ):
            results = await scrape_query_spa(
                context=mock_context,
                query="cafe",
                geo_coordinates=Point(21.0000, 105.8000),
                zoom=16,
                max_places=10,
                range_limit=500.0,
            )

        assert len(results) == 0
        # No element was clicked (all 4 were early dropped)
        for el in far_elements:
            el.evaluate.assert_not_awaited()
            el.click.assert_not_awaited()

        # Feed scroll was called because early exit is disabled (does not halt on consecutive out-of-range)
        assert mock_scroll.call_count >= 1

    asyncio.run(_run())


def test_get_place_urls_early_drop():
    """
    Verifies that get_place_urls drops out-of-range links without halting early
    on consecutive out-of-range items, continuing to scroll until feed end.
    """

    async def _run():
        mock_context = AsyncMock()
        mock_page = AsyncMock()
        mock_page.is_closed = MagicMock(return_value=False)
        mock_page.close = AsyncMock()
        mock_context.new_page.return_value = mock_page

        mock_page.url = "https://www.google.com/maps/search/cafe"
        mock_page.wait_for_selector.return_value = None

        links = [
            "https://www.google.com/maps/place/Near/data=!1s0x1:0x1!8m2!3d21.0010!4d105.8010",
            "https://www.google.com/maps/place/Far1/data=!1s0x2:0x2!8m2!3d21.0500!4d105.8500",
            "https://www.google.com/maps/place/Far2/data=!1s0x3:0x3!8m2!3d21.0510!4d105.8510",
            "https://www.google.com/maps/place/Far3/data=!1s0x4:0x4!8m2!3d21.0520!4d105.8520",
        ]

        def mock_feed_locator(selector):
            loc = AsyncMock()
            loc.count.return_value = 0
            loc.first.is_visible.return_value = False
            if 'a[href*="/maps/place/"]' in selector:
                loc.evaluate_all.return_value = links
            return loc

        mock_page.locator = MagicMock(side_effect=mock_feed_locator)

        from unittest.mock import patch

        mock_scroll = AsyncMock()
        with (
            patch("map_miner.scraper.scroll_feed", mock_scroll),
            patch("map_miner.scraper.is_feed_at_end", AsyncMock(return_value=True)),
        ):
            place_links = await get_place_urls(
                context=mock_context,
                max_places=10,
                query="cafe",
                geo_coordinates=Point(21.0000, 105.8000),
                zoom=16,
                range_limit=500.0,
            )

        assert len(place_links) == 1
        assert (
            "https://www.google.com/maps/place/Near/data=!1s0x1:0x1!8m2!3d21.0010!4d105.8010"
            in place_links
        )
        # Far links were dropped and feed continued scrolling without halting on 3 consecutive out-of-range links
        assert not any("Far" in l for l in place_links)
        assert mock_scroll.call_count == 2

    asyncio.run(_run())


def test_scrape_google_maps_range_limit_default_none_backward_compatible():
    """Verifies that range_limit defaults to None and is passed through transparently."""

    async def _run():
        from unittest.mock import patch

        class FakeBrowser:
            def __init__(self):
                self.is_connected = MagicMock(return_value=True)
                self.close = AsyncMock()

            async def new_context(self, **kwargs):
                ctx = AsyncMock()
                ctx.close = AsyncMock()
                return ctx

        fake_browser = FakeBrowser()
        mock_playwright = AsyncMock()
        mock_playwright.chromium.launch.return_value = fake_browser

        class MockPlaywrightContext:
            async def __aenter__(self):
                return mock_playwright

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                return None

        mock_spa = AsyncMock(return_value=[{"name": "Standard Cafe"}])

        with (
            patch(
                "map_miner.scraper.async_playwright",
                return_value=MockPlaywrightContext(),
            ),
            patch("map_miner.scraper.scrape_query_spa", mock_spa),
        ):
            df = await scrape_google_maps(
                queries={"cafe"},
                geo_coordinates=Point(21.0, 105.8),
                zoom=16,
            )

        assert len(df) == 1
        assert df["name"][0] == "Standard Cafe"
        mock_spa.assert_awaited_once()
        assert mock_spa.call_args.kwargs.get("range_limit") is None

    asyncio.run(_run())


def test_scrape_google_maps_forwards_range_limit():
    """Verifies that scrape_google_maps correctly forwards range_limit in both SPA and fallback modes."""

    async def _run():
        from unittest.mock import patch

        class FakeBrowser:
            def __init__(self):
                self.is_connected = MagicMock(return_value=True)
                self.close = AsyncMock()

            async def new_context(self, **kwargs):
                ctx = AsyncMock()
                ctx.close = AsyncMock()
                return ctx

        fake_browser = FakeBrowser()
        mock_playwright = AsyncMock()
        mock_playwright.chromium.launch.return_value = fake_browser

        class MockPlaywrightContext:
            async def __aenter__(self):
                return mock_playwright

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                return None

        mock_spa = AsyncMock(return_value=[{"name": "SPA Cafe"}])
        mock_urls = AsyncMock(
            return_value={"https://www.google.com/maps/place/Fallback+Cafe"}
        )
        mock_process = AsyncMock(return_value={"name": "Fallback Cafe"})

        # 1. SPA mode with range_limit
        with (
            patch(
                "map_miner.scraper.async_playwright",
                return_value=MockPlaywrightContext(),
            ),
            patch("map_miner.scraper.scrape_query_spa", mock_spa),
        ):
            df_spa = await scrape_google_maps(
                queries={"cafe"},
                geo_coordinates=Point(21.0, 105.8),
                zoom=16,
                use_spa=True,
                range_limit=1500.0,
            )

        assert len(df_spa) == 1
        mock_spa.assert_awaited_once()
        assert mock_spa.call_args.kwargs.get("range_limit") == 1500.0

        # 2. Fallback mode with range_limit
        with (
            patch(
                "map_miner.scraper.async_playwright",
                return_value=MockPlaywrightContext(),
            ),
            patch("map_miner.scraper.get_place_urls", mock_urls),
            patch("map_miner.scraper.process_link", mock_process),
        ):
            df_fallback = await scrape_google_maps(
                queries={"cafe"},
                geo_coordinates=Point(21.0, 105.8),
                zoom=16,
                use_spa=False,
                range_limit=2500.0,
            )

        assert len(df_fallback) == 1
        mock_urls.assert_awaited_once()
        assert mock_urls.call_args.kwargs.get("range_limit") == 2500.0

    asyncio.run(_run())


def test_spa_retry_on_captcha_blocked():
    async def _run():
        mock_browser = MagicMock()
        mock_context_1 = MagicMock()
        mock_context_1.browser = mock_browser
        mock_context_1.close = AsyncMock()

        mock_context_2 = MagicMock()
        mock_context_2.browser = mock_browser
        mock_context_2.close = AsyncMock()

        page_1 = MagicMock()
        page_1.url = "https://www.google.com/sorry/index"
        page_1.goto = AsyncMock()
        page_1.close = AsyncMock()
        page_1.is_closed = MagicMock(return_value=False)
        mock_context_1.new_page = AsyncMock(return_value=page_1)

        page_2 = MagicMock()
        page_2.url = "https://www.google.com/maps/place/Cafe+Test/@21.0,105.8,17z"
        page_2.goto = AsyncMock()
        page_2.close = AsyncMock()
        page_2.is_closed = MagicMock(return_value=False)
        page_2.content = AsyncMock(return_value="<html></html>")
        mock_context_2.new_page = AsyncMock(return_value=page_2)

        rotator = ProxyRotator("socks5://127.0.0.1:9050")
        call_count = 0

        async def mock_handle_captcha(page, context_label=""):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RecaptchaBlockedError("Automated queries detected")
            return True

        with (
            patch(
                "map_miner.scraper.handle_captcha_if_present",
                side_effect=mock_handle_captcha,
            ),
            patch(
                "map_miner.scraper.create_browser_context",
                AsyncMock(return_value=mock_context_2),
            ),
            patch(
                "map_miner.scraper.extract_place_data",
                return_value={"name": "Cafe Test"},
            ),
            patch.object(rotator, "renew", wraps=rotator.renew) as spy_renew,
        ):
            results = await scrape_query_spa(
                context=mock_context_1,
                query="cafe",
                geo_coordinates=Point(21.0, 105.8),
                zoom=16,
                proxy_rotator=rotator,
                max_captcha_retries=2,
            )

            assert len(results) == 1
            assert results[0]["name"] == "Cafe Test"
            assert spy_renew.call_count >= 1
            page_1.close.assert_awaited()
            page_2.close.assert_awaited()
            mock_context_2.close.assert_awaited()

    asyncio.run(_run())


def test_handle_captcha_if_present_timeout():
    """Verifies that handle_captcha_if_present wraps solve_captcha in a 35s timeout,
    and returns False on timeout instead of hanging indefinitely.
    """

    async def _run():
        mock_page = MagicMock()
        mock_page.url = "https://www.google.com/sorry/index"

        with (
            patch("map_miner.scraper.RecaptchaSolver") as mock_solver_cls,
            patch(
                "asyncio.wait_for",
                AsyncMock(side_effect=TimeoutError("Timed out")),
            ) as mock_wait_for,
        ):
            mock_solver = MagicMock()
            mock_coro = MagicMock()
            mock_solver.solve_captcha = MagicMock(return_value=mock_coro)
            mock_solver_cls.return_value = mock_solver

            result = await handle_captcha_if_present(mock_page, context_label="test")
            assert result is False
            mock_wait_for.assert_awaited_once_with(
                mock_coro, timeout=DEFAULT_CAPTCHA_TIMEOUT
            )

    asyncio.run(_run())


def test_handle_captcha_if_present_success():
    """Verifies that handle_captcha_if_present returns True when solver succeeds."""

    async def _run():
        mock_page = MagicMock()
        mock_page.url = "https://www.google.com/sorry/index"

        with patch("map_miner.scraper.RecaptchaSolver") as mock_solver_cls:
            mock_solver = MagicMock()
            mock_solver.solve_captcha = AsyncMock(return_value=True)
            mock_solver_cls.return_value = mock_solver

            result = await handle_captcha_if_present(mock_page, context_label="test")
            assert result is True

    asyncio.run(_run())


def test_scrape_query_spa_timeout_returns_partial_results():
    """Verifies that scrape_query_spa halts when query_timeout is reached,
    returns accumulated partial results, and properly closes the page."""

    async def _run():
        mock_context = AsyncMock()
        mock_page = AsyncMock()
        mock_page.is_closed = MagicMock(return_value=False)
        mock_page.close = AsyncMock()
        mock_context.new_page.return_value = mock_page

        mock_page.url = "https://www.google.com/maps/search/cafe"
        mock_page.content.return_value = "<html></html>"
        mock_page.wait_for_selector.return_value = None

        valid_blob = [None] * 20
        valid_blob[11] = "Cafe 1"
        valid_outer = [None, [], None, None, None, None, valid_blob]
        valid_preview = ")]}'\n" + json.dumps(valid_outer)

        mock_resp = AsyncMock()
        mock_resp.url = (
            "https://www.google.com/maps/preview/place/Cafe1/data=!1s0x1:0x1"
        )
        mock_resp.status = 200
        mock_resp.ok = True
        mock_resp.text.return_value = valid_preview

        class MockExpectResponse:
            def __init__(self, resp):
                self.value = asyncio.Future()
                self.value.set_result(resp)

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                return None

        mock_page.expect_response = MagicMock(
            side_effect=lambda pred, timeout: MockExpectResponse(mock_resp)
        )

        el1 = AsyncMock()
        el1.get_attribute.return_value = (
            "https://www.google.com/maps/place/Cafe1/data=!1s0x1:0x1"
        )
        el1.evaluate.return_value = None

        def mock_feed_locator(selector):
            loc = AsyncMock()
            loc.count.return_value = 0
            loc.first.is_visible.return_value = False
            if 'a[href*="/maps/place/"]' in selector:
                loc.all.return_value = [el1]
                loc.evaluate_all.return_value = [
                    "https://www.google.com/maps/place/Cafe1/data=!1s0x1:0x1"
                ]
            return loc

        mock_page.locator = MagicMock(side_effect=mock_feed_locator)

        current_time = 1000.0

        def fake_monotonic():
            return current_time

        async def mock_scroll(*args, **kwargs):
            nonlocal current_time
            current_time += 500.0

        with (
            patch("map_miner.scraper.asyncio.sleep", AsyncMock()),
            patch("map_miner.scraper.time.monotonic", side_effect=fake_monotonic),
            patch("map_miner.scraper.is_feed_at_end", AsyncMock(return_value=False)),
            patch("map_miner.scraper.scroll_feed", side_effect=mock_scroll),
        ):
            results = await scrape_query_spa(
                context=mock_context,
                query="cafe",
                geo_coordinates=Point(21.0, 105.8),
                zoom=16,
                max_places=10,
                query_timeout=10.0,
            )

        assert len(results) == 1
        assert results[0]["name"] == "Cafe 1"
        mock_page.close.assert_awaited()

    asyncio.run(_run())


def test_get_place_urls_timeout_returns_partial_links():
    """Verifies that get_place_urls halts when query_timeout is reached,
    returns accumulated partial links, and properly closes the page."""

    async def _run():
        mock_context = AsyncMock()
        mock_page = AsyncMock()
        mock_page.is_closed = MagicMock(return_value=False)
        mock_page.close = AsyncMock()
        mock_context.new_page.return_value = mock_page

        mock_page.url = "https://www.google.com/maps/search/cafe"
        mock_page.wait_for_selector.return_value = None

        links = ["https://www.google.com/maps/place/Cafe1/data=!1s0x1:0x1"]

        def mock_feed_locator(selector):
            loc = AsyncMock()
            loc.count.return_value = 0
            loc.first.is_visible.return_value = False
            if 'a[href*="/maps/place/"]' in selector:
                loc.evaluate_all.return_value = links
            return loc

        mock_page.locator = MagicMock(side_effect=mock_feed_locator)

        current_time = 1000.0

        def fake_monotonic():
            return current_time

        async def mock_scroll(*args, **kwargs):
            nonlocal current_time
            current_time += 500.0

        with (
            patch("map_miner.scraper.asyncio.sleep", AsyncMock()),
            patch("map_miner.scraper.time.monotonic", side_effect=fake_monotonic),
            patch("map_miner.scraper.is_feed_at_end", AsyncMock(return_value=False)),
            patch("map_miner.scraper.scroll_feed", side_effect=mock_scroll),
        ):
            place_links = await get_place_urls(
                context=mock_context,
                max_places=10,
                query="cafe",
                geo_coordinates=Point(21.0, 105.8),
                zoom=16,
                query_timeout=10.0,
            )

        assert len(place_links) == 1
        assert "https://www.google.com/maps/place/Cafe1/data=!1s0x1:0x1" in place_links
        mock_page.close.assert_awaited()

    asyncio.run(_run())


def test_consecutive_empty_scrolls_guard_spa():
    """Verifies that scrape_query_spa stops when consecutive_empty_scrolls
    reaches MAX_CONSECUTIVE_EMPTY_SCROLLS even if scrollHeight continues to increase."""

    async def _run():
        mock_context = AsyncMock()
        mock_page = AsyncMock()
        mock_page.is_closed = MagicMock(return_value=False)
        mock_page.close = AsyncMock()
        mock_context.new_page.return_value = mock_page

        mock_page.url = "https://www.google.com/maps/search/cafe"
        mock_page.content.return_value = "<html></html>"
        mock_page.wait_for_selector.return_value = None

        def mock_feed_locator(selector):
            loc = AsyncMock()
            loc.count.return_value = 0
            loc.first.is_visible.return_value = False
            if 'a[href*="/maps/place/"]' in selector:
                loc.all.return_value = []
                loc.evaluate_all.return_value = []
            return loc

        mock_page.locator = MagicMock(side_effect=mock_feed_locator)

        height_counter = 0

        async def mock_evaluate(script, *args):
            nonlocal height_counter
            height_counter += 100
            return height_counter

        mock_page.evaluate = AsyncMock(side_effect=mock_evaluate)

        scroll_count = 0

        async def mock_scroll(page, selector):
            nonlocal scroll_count
            scroll_count += 1

        with (
            patch("map_miner.scraper.asyncio.sleep", AsyncMock()),
            patch("map_miner.scraper.scroll_feed", side_effect=mock_scroll),
            patch("map_miner.scraper.is_feed_at_end", AsyncMock(return_value=False)),
        ):
            results = await scrape_query_spa(
                context=mock_context,
                query="cafe",
                geo_coordinates=Point(21.0, 105.8),
                zoom=16,
                max_places=10,
            )

        assert len(results) == 0
        assert scroll_count == MAX_CONSECUTIVE_EMPTY_SCROLLS
        mock_page.close.assert_awaited()

    asyncio.run(_run())


def test_consecutive_empty_scrolls_guard_get_place_urls():
    """Verifies that get_place_urls stops when consecutive_empty_scrolls
    reaches MAX_CONSECUTIVE_EMPTY_SCROLLS even if scrollHeight continues to increase."""

    async def _run():
        mock_context = AsyncMock()
        mock_page = AsyncMock()
        mock_page.is_closed = MagicMock(return_value=False)
        mock_page.close = AsyncMock()
        mock_context.new_page.return_value = mock_page

        mock_page.url = "https://www.google.com/maps/search/cafe"
        mock_page.wait_for_selector.return_value = None

        def mock_feed_locator(selector):
            loc = AsyncMock()
            loc.count.return_value = 0
            loc.first.is_visible.return_value = False
            if 'a[href*="/maps/place/"]' in selector:
                loc.evaluate_all.return_value = []
            return loc

        mock_page.locator = MagicMock(side_effect=mock_feed_locator)

        height_counter = 0

        async def mock_evaluate(script, *args):
            nonlocal height_counter
            height_counter += 100
            return height_counter

        mock_page.evaluate = AsyncMock(side_effect=mock_evaluate)

        scroll_count = 0

        async def mock_scroll(page, selector):
            nonlocal scroll_count
            scroll_count += 1

        with (
            patch("map_miner.scraper.asyncio.sleep", AsyncMock()),
            patch("map_miner.scraper.scroll_feed", side_effect=mock_scroll),
            patch("map_miner.scraper.is_feed_at_end", AsyncMock(return_value=False)),
        ):
            place_links = await get_place_urls(
                context=mock_context,
                max_places=10,
                query="cafe",
                geo_coordinates=Point(21.0, 105.8),
                zoom=16,
            )

        assert len(place_links) == 0
        assert scroll_count == MAX_CONSECUTIVE_EMPTY_SCROLLS
        mock_page.close.assert_awaited()

    asyncio.run(_run())


def test_scrape_google_maps_spa_fault_isolation():
    """Verifies that in SPA mode, if one query fails with an unhandled exception,
    other concurrent queries proceed and complete successfully, and all contexts are closed."""

    async def _run():
        created_contexts = []

        class FakeBrowser:
            def __init__(self):
                self.is_connected = MagicMock(return_value=True)
                self.close = AsyncMock()

            async def new_context(self, **kwargs):
                ctx = AsyncMock()
                ctx._kwargs = kwargs
                ctx.close = AsyncMock()
                created_contexts.append(ctx)
                return ctx

        fake_browser = FakeBrowser()
        mock_playwright = AsyncMock()
        mock_playwright.chromium.launch.return_value = fake_browser

        class MockPlaywrightContext:
            async def __aenter__(self):
                return mock_playwright

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                return None

        async def mock_spa(context, query, **kwargs):
            if "failing" in query:
                raise RuntimeError("Network disconnected unexpectedly")
            return [{"name": f"Place for {query}", "link": f"http://maps/{query}"}]

        with (
            patch(
                "map_miner.scraper.async_playwright",
                return_value=MockPlaywrightContext(),
            ),
            patch("map_miner.scraper.scrape_query_spa", side_effect=mock_spa),
        ):
            df = await scrape_google_maps(
                queries={"failing_query", "success_query"},
                geo_coordinates=Point(21.0, 105.8),
                zoom=16,
                use_spa=True,
                stagger_delay=0,
            )

        assert len(df) == 1
        assert "success" in df["name"][0]

        # Context isolation: all contexts were properly closed
        assert len(created_contexts) == 2
        for ctx in created_contexts:
            ctx.close.assert_awaited_once()

        fake_browser.close.assert_awaited_once()

    asyncio.run(_run())


def test_scrape_google_maps_fallback_place_timeout():
    """Verifies that in fallback mode, if a place link times out past place_timeout,
    other place links succeed and the timed-out place is safely excluded without crash."""

    async def _run():
        created_contexts = []

        class FakeBrowser:
            def __init__(self):
                self.is_connected = MagicMock(return_value=True)
                self.close = AsyncMock()

            async def new_context(self, **kwargs):
                ctx = AsyncMock()
                ctx._kwargs = kwargs
                ctx.close = AsyncMock()
                created_contexts.append(ctx)
                return ctx

        fake_browser = FakeBrowser()
        mock_playwright = AsyncMock()
        mock_playwright.chromium.launch.return_value = fake_browser

        class MockPlaywrightContext:
            async def __aenter__(self):
                return mock_playwright

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                return None

        async def mock_get_urls(context, query, **kwargs):
            return {"http://maps/hung_place", "http://maps/good_place"}

        async def mock_process(
            context, link, semaphore, count, total, fields=None, max_retries=2, **kwargs
        ):
            if "hung_place" in link:
                await asyncio.sleep(10.0)
                return {"name": "Should not reach", "link": link}
            return {"name": "Good Place", "link": link}

        with (
            patch(
                "map_miner.scraper.async_playwright",
                return_value=MockPlaywrightContext(),
            ),
            patch(
                "map_miner.scraper.get_place_urls",
                side_effect=mock_get_urls,
            ),
            patch("map_miner.scraper.process_link", side_effect=mock_process),
        ):
            df = await scrape_google_maps(
                queries={"cafe"},
                geo_coordinates=Point(21.0, 105.8),
                zoom=16,
                use_spa=False,
                place_timeout=0.05,
            )

        assert len(df) == 1
        assert df["name"][0] == "Good Place"

        # 1 query context + 2 detail contexts = 3 contexts
        assert len(created_contexts) == 3
        for ctx in created_contexts:
            ctx.close.assert_awaited_once()

        fake_browser.close.assert_awaited_once()

    asyncio.run(_run())


def test_scrape_google_maps_watchdog_timeout_spa():
    """Verifies that in SPA mode, if scrape_query_spa hangs,
    the watchdog timeout fires, context is closed, and empty results are returned."""

    async def _run():
        created_contexts = []

        class FakeBrowser:
            def __init__(self):
                self.is_connected = MagicMock(return_value=True)
                self.close = AsyncMock()

            async def new_context(self, **kwargs):
                ctx = AsyncMock()
                ctx._kwargs = kwargs
                ctx.close = AsyncMock()
                created_contexts.append(ctx)
                return ctx

        fake_browser = FakeBrowser()
        mock_playwright = AsyncMock()
        mock_playwright.chromium.launch.return_value = fake_browser

        class MockPlaywrightContext:
            async def __aenter__(self):
                return mock_playwright

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                return None

        async def mock_spa_hang(context, query, **kwargs):
            await asyncio.sleep(100.0)
            return []

        async def mock_wait_for(coro, timeout):
            coro.close()
            raise TimeoutError("Hard watchdog timeout")

        with (
            patch(
                "map_miner.scraper.async_playwright",
                return_value=MockPlaywrightContext(),
            ),
            patch(
                "map_miner.scraper.scrape_query_spa",
                side_effect=mock_spa_hang,
            ),
            patch("asyncio.wait_for", side_effect=mock_wait_for),
        ):
            df = await scrape_google_maps(
                queries={"hanging_query"},
                geo_coordinates=Point(21.0, 105.8),
                zoom=16,
                use_spa=True,
                query_timeout=30.0,
            )

        assert len(df) == 0
        assert len(created_contexts) == 1
        created_contexts[0].close.assert_awaited_once()
        fake_browser.close.assert_awaited_once()

    asyncio.run(_run())


def test_launch_args_webgl_and_stealth():
    """Verifies that LAUNCH_ARGS enables WebGL, removes --disable-gpu, and includes stealth flags."""
    assert "--disable-gpu" not in LAUNCH_ARGS
    assert "--enable-webgl" in LAUNCH_ARGS
    assert "--disable-blink-features=AutomationControlled" in LAUNCH_ARGS


def test_create_browser_context_modern_stealth_and_client_hints():
    """Verifies that create_browser_context sets Chrome 131 UA, Client Hints, and comprehensive stealth script."""

    async def _run():
        browser = AsyncMock()
        mock_context = AsyncMock()
        browser.new_context.return_value = mock_context

        context = await create_browser_context(browser=browser, lang="vi")
        assert context == mock_context

        kwargs = browser.new_context.await_args.kwargs
        assert "Chrome/131.0.0.0" in kwargs["user_agent"]
        assert "extra_http_headers" in kwargs
        headers = kwargs["extra_http_headers"]
        assert '"Google Chrome";v="131"' in headers["sec-ch-ua"]
        assert headers["sec-ch-ua-mobile"] == "?0"
        assert headers["sec-ch-ua-platform"] == '"Windows"'
        assert "vi-VI,vi;q=0.9,en-US;q=0.8,en;q=0.7" in headers["Accept-Language"]

        mock_context.add_init_script.assert_awaited_once()
        script = mock_context.add_init_script.await_args[0][0]
        assert "webdriver" in script
        assert "Win32" in script
        assert "vi-VI" in script
        assert "Chrome PDF Viewer" in script
        assert "hardwareConcurrency" in script
        assert "deviceMemory" in script
        assert "Google Inc. (NVIDIA)" in script
        assert "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060" in script
        assert "SwiftShader" in script
        assert "llvmpipe" in script

    asyncio.run(_run())


def test_handle_captcha_if_present_default_and_custom_timeout():
    """Verifies handle_captcha_if_present timeout behavior with default 85.0s and custom timeouts."""
    assert DEFAULT_CAPTCHA_TIMEOUT == 85.0

    async def _run():
        mock_page = MagicMock()
        mock_page.url = "https://www.google.com/sorry/index"
        mock_page.locator.return_value = MagicMock(count=AsyncMock(return_value=1))

        # Test timeout handling
        with patch(
            "map_miner.scraper.RecaptchaSolver.solve_captcha",
            new_callable=AsyncMock,
        ) as mock_solve:

            async def _hang():
                await asyncio.sleep(10.0)
                return True

            mock_solve.side_effect = _hang

            # Timeout after 0.05s
            res = await handle_captcha_if_present(mock_page, timeout=0.05)
            assert res is False

        # Test successful solve
        with patch(
            "map_miner.scraper.RecaptchaSolver.solve_captcha",
            new_callable=AsyncMock,
            return_value=True,
        ):
            res = await handle_captcha_if_present(mock_page)
            assert res is True

    asyncio.run(_run())


def test_staggered_query_dispatch_spa():
    """Verifies that scrape_google_maps applies staggered startup delays for concurrent queries."""

    async def _run():
        mock_playwright = AsyncMock()
        fake_browser = AsyncMock()
        fake_browser.is_connected.return_value = True
        fake_browser.close = AsyncMock()
        mock_playwright.chromium.launch.return_value = fake_browser

        created_contexts = []

        def mock_new_context(**kwargs):
            ctx = AsyncMock()
            ctx._kwargs = kwargs
            ctx.close = AsyncMock()
            created_contexts.append(ctx)
            return ctx

        fake_browser.new_context = AsyncMock(side_effect=mock_new_context)

        class MockPlaywrightContext:
            async def __aenter__(self):
                return mock_playwright

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                return None

        async def mock_spa(context, query, **kwargs):
            return [{"name": f"Place for {query}", "link": f"http://maps/{query}"}]

        slept_delays = []
        orig_sleep = asyncio.sleep

        async def mock_sleep(d):
            slept_delays.append(d)
            # Yield control immediately without blocking
            await orig_sleep(0)

        with (
            patch(
                "map_miner.scraper.async_playwright",
                return_value=MockPlaywrightContext(),
            ),
            patch("map_miner.scraper.scrape_query_spa", side_effect=mock_spa),
            patch("asyncio.sleep", side_effect=mock_sleep),
        ):
            df = await scrape_google_maps(
                queries={"q1", "q2", "q3"},
                geo_coordinates=Point(21.0, 105.8),
                zoom=16,
                use_spa=True,
                stagger_delay=(2.0, 3.0),
            )

        assert len(df) == 3
        # Query 0 had no sleep; Query 1 had 1 * [2.0, 3.0], Query 2 had 2 * [2.0, 3.0]
        # Check that delays in slept_delays include staggered amounts (> 1.5)
        stagger_sleeps = [d for d in slept_delays if d >= 1.5]
        assert len(stagger_sleeps) == 2

    asyncio.run(_run())


def test_preview_timeout_forwarding_in_scrape_google_maps():
    """Verifies that preview_timeout is forwarded from scrape_google_maps to scrape_query_spa."""

    async def _run():
        from map_miner import DEFAULT_SPA_PREVIEW_TIMEOUT
        from map_miner.scraper import scrape_google_maps

        assert DEFAULT_SPA_PREVIEW_TIMEOUT == 15000

        mock_playwright = AsyncMock()
        fake_browser = AsyncMock()
        fake_browser.is_connected = MagicMock(return_value=True)
        fake_browser.close = AsyncMock()
        mock_playwright.chromium.launch.return_value = fake_browser

        class MockPlaywrightContext:
            async def __aenter__(self):
                return mock_playwright

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                return None

        received_kwargs = {}

        async def mock_spa(context, query, **kwargs):
            received_kwargs.update(kwargs)
            return [{"name": "Test Cafe"}]

        with (
            patch(
                "map_miner.scraper.async_playwright",
                return_value=MockPlaywrightContext(),
            ),
            patch("map_miner.scraper.scrape_query_spa", side_effect=mock_spa),
            patch("asyncio.sleep", AsyncMock()),
        ):
            await scrape_google_maps(
                queries={"test"},
                geo_coordinates=Point(21.0, 105.8),
                zoom=16,
                use_spa=True,
                preview_timeout=25000,
                stagger_delay=None,
            )

        assert received_kwargs.get("preview_timeout") == 25000

    asyncio.run(_run())
