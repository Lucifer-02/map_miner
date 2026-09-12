import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import polars as pl
from geopy.point import Point
from playwright.async_api import Error as PlaywrightError

from map_miner.scraper import (
    BLOCKED_RESOURCE_TYPES,
    BLOCKED_URL_PATTERNS,
    CONSENT_BUTTON_REGEX,
    DEFAULT_CACHE_DIR,
    DEFAULT_DISK_CACHE_SIZE,
    DEFAULT_PROXY_BYPASS,
    FEED_FALLBACK_SELECTORS,
    PreviewInterceptor,
    ProxyRotator,
    create_browser_context,
    is_preview_response_for_link,
    make_place_url,
    scrape_google_maps,
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


def test_proxy_rotator_empty_and_none():
    rotator_none = ProxyRotator(None)
    assert rotator_none.total == 0
    assert rotator_none.get() is None

    rotator_empty = ProxyRotator([])
    assert rotator_empty.total == 0
    assert rotator_empty.get() is None

    rotator_invalid = ProxyRotator([None, ""])
    assert rotator_invalid.total == 0
    assert rotator_invalid.get() is None


def test_proxy_rotator_single_proxy():
    proxy_dict = {
        "server": "http://gate.decodo.com:10000",
        "username": "user",
        "password": "password",
    }
    rotator = ProxyRotator(proxy_dict)
    assert rotator.total == 1
    assert rotator.get() == proxy_dict
    assert rotator.get() == proxy_dict
    assert rotator.get() == proxy_dict

    rotator_str = ProxyRotator("http://gate.decodo.com:10000")
    assert rotator_str.total == 1
    assert rotator_str.get() == {"server": "http://gate.decodo.com:10000"}


def test_proxy_rotator_list_round_robin():
    proxies = [{"server": f"http://proxy{i}:8080"} for i in range(1, 4)]
    rotator = ProxyRotator(proxies)
    assert rotator.total == 3

    assert rotator.get() == {"server": "http://proxy1:8080"}
    assert rotator.get() == {"server": "http://proxy2:8080"}
    assert rotator.get() == {"server": "http://proxy3:8080"}
    # Round-robin loop back
    assert rotator.get() == {"server": "http://proxy1:8080"}
    assert rotator.get() == {"server": "http://proxy2:8080"}

    # Test with tuple of strings
    rotator_tuple = ProxyRotator(("http://p1:8080", "http://p2:8080"))
    assert rotator_tuple.total == 2
    assert rotator_tuple.get() == {"server": "http://p1:8080"}
    assert rotator_tuple.get() == {"server": "http://p2:8080"}
    assert rotator_tuple.get() == {"server": "http://p1:8080"}


def test_proxy_rotator_with_bypass():
    assert DEFAULT_PROXY_BYPASS == "maps.gstatic.com,*.gstatic.com,fonts.googleapis.com"

    proxy_dict = {
        "server": "http://gate.decodo.com:10000",
        "username": "user",
        "password": "password",
        "bypass": DEFAULT_PROXY_BYPASS,
    }
    rotator = ProxyRotator(proxy_dict)
    assert rotator.total == 1
    selected = rotator.get()
    assert selected == proxy_dict
    assert selected["bypass"] == DEFAULT_PROXY_BYPASS

    proxies = [
        {"server": "http://proxy1:8080", "bypass": DEFAULT_PROXY_BYPASS},
        {"server": "http://proxy2:8080", "bypass": "custom.domain.com"},
    ]
    rotator_multi = ProxyRotator(proxies)
    assert rotator_multi.total == 2
    first = rotator_multi.get()
    assert first["bypass"] == DEFAULT_PROXY_BYPASS
    second = rotator_multi.get()
    assert second["bypass"] == "custom.domain.com"


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
            context, link, semaphore, count, total, fields=None, max_retries=2
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
