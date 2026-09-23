import asyncio
import hashlib
import inspect
import json
import logging
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import polars as pl
import pytest
from geopy.point import Point
from playwright.async_api import Error as PlaywrightError

from map_miner.proxy import DEFAULT_PROXY_BYPASS
from map_miner.recaptcha_solver import RecaptchaBlockedError
from map_miner.scraper import (
    BLOCKED_RESOURCE_TYPES,
    BLOCKED_URL_PATTERNS,
    CONSENT_BUTTON_REGEX,
    FEED_FALLBACK_SELECTORS,
    LAUNCH_ARGS,
    ProxyRotator,
    _create_browser_context,
    _get_place_urls,
    _global_route_handler,
    _handle_captcha_if_present,
    _is_no_results_page,
    _PreviewInterceptor,
    _safe_close_context,
    _safe_close_page,
    _scrape_query_spa,
    _scroll_feed,
    extract_coordinates_from_url,
    is_preview_response_for_link,
    make_place_url,
    scrape_google_maps,
)


class MockPlaywrightContext:
    def __init__(self, browser=None, playwright=None):
        if playwright is not None:
            self.mock_playwright = playwright
        else:
            self.mock_playwright = AsyncMock()
        if browser is not None:
            self.mock_playwright.chromium.launch.return_value = browser

    async def __aenter__(self):
        return self.mock_playwright

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return None


def make_fake_browser(contexts: list | None = None) -> AsyncMock:
    browser = AsyncMock()
    browser.is_connected = MagicMock(return_value=True)
    browser.close = AsyncMock()

    async def _new_context(**kwargs):
        ctx = AsyncMock()
        ctx._kwargs = kwargs
        ctx.close = AsyncMock()
        if contexts is not None:
            contexts.append(ctx)
        return ctx

    browser.new_context = AsyncMock(side_effect=_new_context)
    return browser


def make_mock_page(
    url: str = "",
    content: str = "<html></html>",
    is_closed: bool = False,
) -> AsyncMock:
    mock_page = AsyncMock()
    mock_page.url = url
    mock_page.content = AsyncMock(return_value=content)
    mock_page.is_closed = MagicMock(return_value=is_closed)
    mock_page.close = AsyncMock()
    mock_page.wait_for_selector = AsyncMock(return_value=None)
    return mock_page


def make_mock_context(
    page: AsyncMock | None = None,
    browser: AsyncMock | None = None,
) -> AsyncMock:
    ctx = AsyncMock()
    ctx.close = AsyncMock()
    if page is not None:
        ctx.new_page = AsyncMock(return_value=page)
    if browser is not None:
        ctx.browser = browser
    return ctx


def test_make_place_url():
    point = Point(20.985322, 105.781289)
    url = make_place_url(
        query="cafe hà đông", geo_coordinates=point, zoom=18, lang="vi"
    )
    assert "https://www.google.com/maps/search/" in url
    assert "cafe+h%C3%A0+%C4%91%C3%B4ng" in url
    assert "@20.985322,105.781289,18z" in url
    assert "hl=vi" in url


def test_blocked_resources_and_urls():
    assert "image" in BLOCKED_RESOURCE_TYPES
    assert "media" in BLOCKED_RESOURCE_TYPES
    assert "font" in BLOCKED_RESOURCE_TYPES
    assert any("google-analytics" in p for p in BLOCKED_URL_PATTERNS)
    assert any("/maps/vt" in p for p in BLOCKED_URL_PATTERNS)
    assert any("feedback-pa.clients6.google.com" in p for p in BLOCKED_URL_PATTERNS)
    assert any("ogads-pa.clients6.google.com" in p for p in BLOCKED_URL_PATTERNS)
    assert any("/maps/preview/entity" in p for p in BLOCKED_URL_PATTERNS)


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
        interceptor = _PreviewInterceptor(page)

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
        from map_miner.scraper import _scrape_query_spa

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

        results = await _scrape_query_spa(
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
        from map_miner.scraper import _scrape_query_spa

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

        results = await _scrape_query_spa(
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


@pytest.mark.parametrize(
    ("proxy_config", "expected_proxy", "has_bypass"),
    [
        (
            {
                "server": "http://gate.decodo.com:10000",
                "username": "user",
                "password": "pass",
            },
            {
                "server": "http://gate.decodo.com:10000",
                "username": "user",
                "password": "pass",
            },
            False,
        ),
        (
            {
                "server": "http://gate.decodo.com:10000",
                "username": "user",
                "password": "pass",
                "bypass": DEFAULT_PROXY_BYPASS,
            },
            {
                "server": "http://gate.decodo.com:10000",
                "username": "user",
                "password": "pass",
                "bypass": DEFAULT_PROXY_BYPASS,
            },
            True,
        ),
        (
            None,
            None,
            False,
        ),
    ],
)
def test_create_browser_context_proxy_modes(proxy_config, expected_proxy, has_bypass):
    async def _run():
        browser = AsyncMock()
        mock_context = make_mock_context()
        browser.new_context.return_value = mock_context

        point = Point(21.018785, 105.830415)
        lang = "vi" if (proxy_config and not has_bypass) else "en"
        context = await _create_browser_context(
            browser=browser,
            geo_coordinates=point,
            lang=lang,
            proxy=proxy_config,
        )

        assert context == mock_context
        browser.new_context.assert_awaited_once()
        kwargs = browser.new_context.await_args.kwargs
        if expected_proxy is not None:
            assert kwargs["proxy"] == expected_proxy
            if has_bypass:
                assert kwargs["proxy"]["bypass"] == DEFAULT_PROXY_BYPASS
                assert "fonts.gstatic.com" in kwargs["proxy"]["bypass"]
                assert "apis.google.com" in kwargs["proxy"]["bypass"]
                assert "ssl.gstatic.com" in kwargs["proxy"]["bypass"]
        else:
            assert "proxy" not in kwargs

        assert kwargs["geolocation"] == {
            "latitude": point.latitude,
            "longitude": point.longitude,
        }
        assert kwargs["locale"] == lang
        if proxy_config and not has_bypass:
            assert kwargs["permissions"] == ["geolocation"]
            assert "width" in kwargs["viewport"]
            assert "height" in kwargs["viewport"]
            mock_context.add_init_script.assert_awaited_once()
            mock_context.route.assert_awaited_once()

    asyncio.run(_run())


def test_scrape_google_maps_context_isolation_and_rotation_spa():
    async def _run():
        created_contexts = []
        fake_browser = make_fake_browser(created_contexts)
        mock_playwright = AsyncMock()
        mock_playwright.chromium.launch.return_value = fake_browser

        proxies = [
            {"server": "http://proxy1:8080"},
            {"server": "http://proxy2:8080"},
        ]

        async def mock_spa(context, query, **kwargs):
            return [{"name": f"Place for {query}", "link": f"http://maps/{query}"}]

        with (
            patch(
                "map_miner.scraper.async_playwright",
                return_value=MockPlaywrightContext(playwright=mock_playwright),
            ),
            patch("map_miner.scraper._scrape_query_spa", side_effect=mock_spa),
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
        created_contexts = []
        fake_browser = make_fake_browser(created_contexts)
        mock_playwright = AsyncMock()
        mock_playwright.chromium.launch.return_value = fake_browser

        async def mock_get_urls(context, query, **kwargs):
            return {f"http://maps.google.com/place/{query}_1"}

        async def mock_process(
            context, link, semaphore, count, total, fields=None, max_retries=2, **kwargs
        ):
            return {"name": f"Place {link}", "link": link}

        with (
            patch(
                "map_miner.scraper.async_playwright",
                return_value=MockPlaywrightContext(fake_browser),
            ),
            patch("map_miner.scraper._get_place_urls", side_effect=mock_get_urls),
            patch("map_miner.scraper._process_link", side_effect=mock_process),
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
        created_contexts = []
        fake_browser = make_fake_browser(created_contexts)
        mock_playwright = AsyncMock()
        mock_playwright.chromium.launch.return_value = fake_browser

        async def mock_spa_fail(context, query, **kwargs):
            raise PlaywrightError("Browser crashed or tab disconnected")

        with (
            patch(
                "map_miner.scraper.async_playwright",
                return_value=MockPlaywrightContext(fake_browser),
            ),
            patch("map_miner.scraper._scrape_query_spa", side_effect=mock_spa_fail),
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


@pytest.mark.parametrize("cache_mode", ["custom", "none", "default"])
def test_scrape_google_maps_cache_dir_modes(tmp_path, cache_mode):
    """Verifies --disk-cache-dir and --disk-cache-size flags for custom, none, and default cache_dir."""

    async def _run():
        fake_browser = make_fake_browser()
        mock_playwright = AsyncMock()
        mock_playwright.chromium.launch.return_value = fake_browser

        custom_cache = tmp_path / "test_cache"
        with (
            patch(
                "map_miner.scraper.async_playwright",
                return_value=MockPlaywrightContext(playwright=mock_playwright),
            ),
            patch(
                "map_miner.scraper._scrape_query_spa",
                return_value=[{"name": "Cafe A"}] if cache_mode == "custom" else [],
            ),
        ):
            if cache_mode == "custom":
                assert not custom_cache.exists()
                df = await scrape_google_maps(
                    queries={"cafe"},
                    geo_coordinates=Point(21.0, 105.8),
                    zoom=16,
                    cache_dir=custom_cache,
                )
            elif cache_mode == "none":
                df = await scrape_google_maps(
                    queries={"cafe"},
                    geo_coordinates=Point(21.0, 105.8),
                    zoom=16,
                    cache_dir=None,
                )
            else:
                df = await scrape_google_maps(
                    queries={"cafe"},
                    geo_coordinates=Point(21.0, 105.8),
                    zoom=16,
                )

        if cache_mode == "custom":
            assert len(df) == 1
            assert custom_cache.exists()

        mock_playwright.chromium.launch.assert_awaited_once()
        launch_kwargs = mock_playwright.chromium.launch.call_args.kwargs
        launch_args = launch_kwargs.get("args", [])

        if cache_mode == "custom":
            assert any(
                f"--disk-cache-dir={custom_cache.resolve()}" in arg
                for arg in launch_args
            )
            assert "--disk-cache-size=1073741824" in launch_args
        elif cache_mode == "none":
            assert not any("--disk-cache-dir" in arg for arg in launch_args)
            assert not any("--disk-cache-size" in arg for arg in launch_args)
        else:  # "default"
            resolved_default = (Path(".cache") / "chromium_cache").resolve()
            assert any(
                f"--disk-cache-dir={resolved_default}" in arg for arg in launch_args
            )
            assert "--disk-cache-size=1073741824" in launch_args

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

        with patch("map_miner.scraper._is_feed_at_end", AsyncMock(return_value=True)):
            results = await _scrape_query_spa(
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
            patch("map_miner.scraper._scroll_feed", mock_scroll),
            patch("map_miner.scraper._is_feed_at_end", AsyncMock(return_value=True)),
        ):
            results = await _scrape_query_spa(
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
            patch("map_miner.scraper._scroll_feed", mock_scroll),
            patch("map_miner.scraper._is_feed_at_end", AsyncMock(return_value=True)),
        ):
            place_links = await _get_place_urls(
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


def test_scrape_google_maps_range_limit_default_upper_bound():
    """Verifies that range_limit defaults to 10000.0 and is passed through transparently."""

    async def _run():
        fake_browser = make_fake_browser()
        mock_playwright = AsyncMock()
        mock_playwright.chromium.launch.return_value = fake_browser

        mock_spa = AsyncMock(return_value=[{"name": "Standard Cafe"}])

        with (
            patch(
                "map_miner.scraper.async_playwright",
                return_value=MockPlaywrightContext(fake_browser),
            ),
            patch("map_miner.scraper._scrape_query_spa", mock_spa),
        ):
            df = await scrape_google_maps(
                queries={"cafe"},
                geo_coordinates=Point(21.0, 105.8),
                zoom=16,
            )

        assert len(df) == 1
        assert df["name"][0] == "Standard Cafe"
        mock_spa.assert_awaited_once()
        assert mock_spa.call_args.kwargs.get("range_limit") == 10000.0

    asyncio.run(_run())


def test_scrape_google_maps_forwards_range_limit():
    """Verifies that scrape_google_maps correctly forwards range_limit in both SPA and fallback modes."""

    async def _run():
        fake_browser = make_fake_browser()
        mock_playwright = AsyncMock()
        mock_playwright.chromium.launch.return_value = fake_browser

        mock_spa = AsyncMock(return_value=[{"name": "SPA Cafe"}])
        mock_urls = AsyncMock(
            return_value={"https://www.google.com/maps/place/Fallback+Cafe"}
        )
        mock_process = AsyncMock(return_value={"name": "Fallback Cafe"})

        # 1. SPA mode with range_limit
        with (
            patch(
                "map_miner.scraper.async_playwright",
                return_value=MockPlaywrightContext(fake_browser),
            ),
            patch("map_miner.scraper._scrape_query_spa", mock_spa),
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
                return_value=MockPlaywrightContext(fake_browser),
            ),
            patch("map_miner.scraper._get_place_urls", mock_urls),
            patch("map_miner.scraper._process_link", mock_process),
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

        async def mock_handle_captcha(page, context_label="", **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RecaptchaBlockedError("Automated queries detected")
            return True

        with (
            patch(
                "map_miner.scraper._handle_captcha_if_present",
                side_effect=mock_handle_captcha,
            ),
            patch(
                "map_miner.scraper._create_browser_context",
                AsyncMock(return_value=mock_context_2),
            ),
            patch(
                "map_miner.scraper.extract_place_data",
                return_value={"name": "Cafe Test"},
            ),
            patch.object(rotator, "renew", wraps=rotator.renew) as spy_renew,
        ):
            results = await _scrape_query_spa(
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

            result = await _handle_captcha_if_present(mock_page, context_label="test")
            assert result is False
            mock_wait_for.assert_awaited_once_with(mock_coro, timeout=85.0)

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

            result = await _handle_captcha_if_present(mock_page, context_label="test")
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
            patch("map_miner.scraper._is_feed_at_end", AsyncMock(return_value=False)),
            patch("map_miner.scraper._scroll_feed", side_effect=mock_scroll),
        ):
            results = await _scrape_query_spa(
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
            patch("map_miner.scraper._is_feed_at_end", AsyncMock(return_value=False)),
            patch("map_miner.scraper._scroll_feed", side_effect=mock_scroll),
        ):
            place_links = await _get_place_urls(
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


@pytest.mark.parametrize(
    "unprocessed_items",
    [
        [],
        ["https://www.google.com/maps/place/UnprocessableItem"],
    ],
)
def test_consecutive_empty_scrolls_guard_spa(unprocessed_items):
    """Verifies that scrape_query_spa stops when consecutive_empty_scrolls
    reaches 4 consecutive empty scrolls even if scrollHeight continues to increase
    and even if unprocessed items remain."""

    async def _run():
        mock_page = make_mock_page(url="https://www.google.com/maps/search/cafe")
        mock_context = make_mock_context(page=mock_page)

        def mock_feed_locator(selector):
            loc = AsyncMock()
            loc.count.return_value = 0
            loc.first.is_visible.return_value = False
            if 'a[href*="/maps/place/"]' in selector:
                loc.all.return_value = []
                loc.evaluate_all.return_value = unprocessed_items
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
            patch("map_miner.scraper._scroll_feed", side_effect=mock_scroll),
            patch("map_miner.scraper._is_feed_at_end", AsyncMock(return_value=False)),
        ):
            results = await _scrape_query_spa(
                context=mock_context,
                query="cafe",
                geo_coordinates=Point(21.0, 105.8),
                zoom=16,
                max_places=10,
            )

        assert len(results) == 0
        assert scroll_count == 4
        mock_page.close.assert_awaited()

    asyncio.run(_run())


def test_consecutive_empty_scrolls_guard_get_place_urls():
    """Verifies that get_place_urls stops when consecutive_empty_scrolls
    reaches 4 consecutive empty scrolls even if scrollHeight continues to increase."""

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
            patch("map_miner.scraper._scroll_feed", side_effect=mock_scroll),
            patch("map_miner.scraper._is_feed_at_end", AsyncMock(return_value=False)),
        ):
            place_links = await _get_place_urls(
                context=mock_context,
                max_places=10,
                query="cafe",
                geo_coordinates=Point(21.0, 105.8),
                zoom=16,
            )

        assert len(place_links) == 0
        assert scroll_count == 4
        mock_page.close.assert_awaited()

    asyncio.run(_run())


def test_scrape_google_maps_spa_fault_isolation():
    """Verifies that in SPA mode, if one query fails with an unhandled exception,
    other concurrent queries proceed and complete successfully, and all contexts are closed."""

    async def _run():
        created_contexts = []
        fake_browser = make_fake_browser(created_contexts)
        mock_playwright = AsyncMock()
        mock_playwright.chromium.launch.return_value = fake_browser

        async def mock_spa(context, query, **kwargs):
            if "failing" in query:
                raise RuntimeError("Network disconnected unexpectedly")
            return [{"name": f"Place for {query}", "link": f"http://maps/{query}"}]

        with (
            patch(
                "map_miner.scraper.async_playwright",
                return_value=MockPlaywrightContext(fake_browser),
            ),
            patch("map_miner.scraper._scrape_query_spa", side_effect=mock_spa),
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
        fake_browser = make_fake_browser(created_contexts)
        mock_playwright = AsyncMock()
        mock_playwright.chromium.launch.return_value = fake_browser

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
                return_value=MockPlaywrightContext(fake_browser),
            ),
            patch(
                "map_miner.scraper._get_place_urls",
                side_effect=mock_get_urls,
            ),
            patch("map_miner.scraper._process_link", side_effect=mock_process),
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
        fake_browser = make_fake_browser(created_contexts)
        mock_playwright = AsyncMock()
        mock_playwright.chromium.launch.return_value = fake_browser

        async def mock_spa_hang(context, query, **kwargs):
            await asyncio.sleep(100.0)
            return []

        async def mock_wait_for(coro, timeout):
            coro.close()
            raise TimeoutError("Hard watchdog timeout")

        with (
            patch(
                "map_miner.scraper.async_playwright",
                return_value=MockPlaywrightContext(fake_browser),
            ),
            patch(
                "map_miner.scraper._scrape_query_spa",
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

        point = Point(21.018785, 105.830415)
        context = await _create_browser_context(
            browser=browser, geo_coordinates=point, lang="vi"
        )
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
    assert (
        inspect.signature(_handle_captcha_if_present).parameters["timeout"].default
        == 85.0
    )

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
            res = await _handle_captcha_if_present(mock_page, timeout=0.05)
            assert res is False

        # Test successful solve
        with patch(
            "map_miner.scraper.RecaptchaSolver.solve_captcha",
            new_callable=AsyncMock,
            return_value=True,
        ):
            res = await _handle_captcha_if_present(mock_page)
            assert res is True

    asyncio.run(_run())


def test_staggered_query_dispatch_spa():
    """Verifies that scrape_google_maps applies staggered startup delays for concurrent queries."""

    async def _run():
        created_contexts = []
        fake_browser = make_fake_browser(created_contexts)
        mock_playwright = AsyncMock()
        mock_playwright.chromium.launch.return_value = fake_browser

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
                return_value=MockPlaywrightContext(fake_browser),
            ),
            patch("map_miner.scraper._scrape_query_spa", side_effect=mock_spa),
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
        assert (
            inspect.signature(scrape_google_maps).parameters["preview_timeout"].default
            == 10000
        )

        fake_browser = make_fake_browser()
        mock_playwright = AsyncMock()
        mock_playwright.chromium.launch.return_value = fake_browser

        received_kwargs = {}

        async def mock_spa(context, query, **kwargs):
            received_kwargs.update(kwargs)
            return [{"name": "Test Cafe"}]

        with (
            patch(
                "map_miner.scraper.async_playwright",
                return_value=MockPlaywrightContext(fake_browser),
            ),
            patch("map_miner.scraper._scrape_query_spa", side_effect=mock_spa),
            patch("asyncio.sleep", AsyncMock()),
        ):
            await scrape_google_maps(
                queries={"test"},
                geo_coordinates=Point(21.0, 105.8),
                zoom=16,
                use_spa=True,
                preview_timeout=25000,
                stagger_delay=0,
            )

        assert received_kwargs.get("preview_timeout") == 25000

    asyncio.run(_run())


def test_static_route_cache_hit(tmp_path):
    async def _run():
        cache_dir = tmp_path / "static_assets"
        cache_dir.mkdir(parents=True, exist_ok=True)

        url = "https://www.google.com/maps/_/js/k=maps.m.en.js"
        cache_key = hashlib.sha256(url.encode("utf-8")).hexdigest()
        cache_file = cache_dir / cache_key
        cache_file.write_bytes(b"/* cached javascript code */")

        route = AsyncMock()
        route.request.url = url
        route.request.method = "GET"
        route.request.resource_type = "script"
        route.fulfill = AsyncMock()
        route.continue_ = AsyncMock()
        route.abort = AsyncMock()

        await _global_route_handler(route, static_cache_dir=cache_dir)

        route.fulfill.assert_awaited_once()
        assert route.fulfill.await_args is not None
        kwargs = route.fulfill.await_args.kwargs
        assert kwargs["status"] == 200
        assert kwargs["body"] == b"/* cached javascript code */"
        assert (
            kwargs["headers"]["content-type"] == "application/javascript; charset=utf-8"
        )
        assert kwargs["headers"]["x-cache"] == "HIT-ROUTE-CACHE"
        route.continue_.assert_not_called()
        route.abort.assert_not_called()

    asyncio.run(_run())


@pytest.mark.parametrize(
    ("url", "method", "resource_type"),
    [
        (
            "https://www.google.com/maps/preview/place?authuser=0&hl=vi&gl=vn&pb=!1m18!1m12!1m3!1d1",
            "GET",
            "xhr",
        ),
        (
            "https://www.google.com/maps/_/js/k=maps.m.en.js",
            "POST",
            "script",
        ),
    ],
)
def test_static_route_cache_skips(tmp_path, url, method, resource_type):
    """Verifies that static route cache skips preview XHR requests and non-GET requests."""

    async def _run():
        cache_dir = tmp_path / "static_assets"
        cache_dir.mkdir(parents=True, exist_ok=True)

        route = AsyncMock()
        route.request.url = url
        route.request.method = method
        route.request.resource_type = resource_type
        route.fulfill = AsyncMock()
        route.continue_ = AsyncMock()
        route.abort = AsyncMock()

        await _global_route_handler(route, static_cache_dir=cache_dir)

        route.continue_.assert_awaited_once()
        route.fulfill.assert_not_called()
        route.abort.assert_not_called()

    asyncio.run(_run())


def test_static_route_cache_miss_fetches_and_saves(tmp_path):
    async def _run():
        cache_dir = tmp_path / "static_assets"
        cache_dir.mkdir(parents=True, exist_ok=True)

        url = "https://maps.gstatic.com/maps-api-v3/api/js/59/1/intl/vi_ALL/common.js"
        cache_key = hashlib.sha256(url.encode("utf-8")).hexdigest()
        cache_file = cache_dir / cache_key

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.body.return_value = b"console.log('fresh js');"

        route = AsyncMock()
        route.request.url = url
        route.request.method = "GET"
        route.request.resource_type = "script"
        route.fetch = AsyncMock(return_value=mock_resp)
        route.fulfill = AsyncMock()
        route.continue_ = AsyncMock()
        route.abort = AsyncMock()

        await _global_route_handler(route, static_cache_dir=cache_dir)

        route.fetch.assert_awaited_once()
        route.fulfill.assert_awaited_once_with(response=mock_resp)
        assert cache_file.is_file()
        assert cache_file.read_bytes() == b"console.log('fresh js');"

    asyncio.run(_run())


def test_static_route_cache_css_content_type(tmp_path):
    async def _run():
        cache_dir = tmp_path / "static_assets"
        cache_dir.mkdir(parents=True, exist_ok=True)

        url = "https://www.google.com/maps/_/ss/k=maps.m.en.css"
        cache_key = hashlib.sha256(url.encode("utf-8")).hexdigest()
        cache_file = cache_dir / cache_key
        cache_file.write_bytes(b"body { color: red; }")

        route = AsyncMock()
        route.request.url = url
        route.request.method = "GET"
        route.request.resource_type = "stylesheet"
        route.fulfill = AsyncMock()
        route.continue_ = AsyncMock()
        route.abort = AsyncMock()

        await _global_route_handler(route, static_cache_dir=cache_dir)

        route.fulfill.assert_awaited_once()
        assert route.fulfill.await_args is not None
        kwargs = route.fulfill.await_args.kwargs
        assert kwargs["status"] == 200
        assert kwargs["body"] == b"body { color: red; }"
        assert kwargs["headers"]["content-type"] == "text/css; charset=utf-8"
        assert kwargs["headers"]["x-cache"] == "HIT-ROUTE-CACHE"

    asyncio.run(_run())


def test_scroll_feed_uses_first_locator_and_handles_multiple_elements():
    """Verifies that scroll_feed calls .first on the locator before hover,
    preventing Playwright strict mode violation when selector matches multiple elements."""

    async def _run():
        mock_page = AsyncMock()
        mock_locator = MagicMock()
        mock_first = AsyncMock()
        mock_locator.first = mock_first
        mock_page.locator = MagicMock(return_value=mock_locator)
        mock_page.mouse = AsyncMock()
        mock_page.evaluate = AsyncMock()

        with patch("map_miner.scraper.asyncio.sleep", AsyncMock()):
            await _scroll_feed(mock_page, '[role="feed"]')

        mock_page.locator.assert_called_once_with('[role="feed"]')
        mock_first.hover.assert_awaited_once()
        mock_page.mouse.wheel.assert_awaited_once_with(0, 5000)
        mock_page.evaluate.assert_awaited_once()

    asyncio.run(_run())


@pytest.mark.parametrize("mode", ["spa", "fallback"])
def test_early_drop_logged_at_debug_level(caplog, mode):
    """Verifies that early drop messages for places outside range_limit
    are logged at DEBUG level and NOT at INFO level in both SPA and fallback modes."""

    async def _run():
        mock_page = make_mock_page(url="https://www.google.com/maps/search/cafe")
        mock_context = make_mock_context(page=mock_page)

        el_far = AsyncMock()
        el_far.get_attribute.return_value = "https://www.google.com/maps/place/Far+Cafe/data=!1s0x2:0x2!8m2!3d21.0500!4d105.8500"

        def mock_feed_locator(selector):
            loc = AsyncMock()
            loc.count.return_value = 0
            loc.first.is_visible.return_value = False
            if 'a[href*="/maps/place/"]' in selector:
                loc.all.return_value = [el_far]
                loc.evaluate_all.return_value = [
                    "https://www.google.com/maps/place/Far+Cafe/data=!1s0x2:0x2!8m2!3d21.0500!4d105.8500"
                ]
            return loc

        mock_page.locator = MagicMock(side_effect=mock_feed_locator)

        with (
            patch("map_miner.scraper.asyncio.sleep", AsyncMock()),
            patch("map_miner.scraper._scroll_feed", AsyncMock()),
            patch("map_miner.scraper._is_feed_at_end", AsyncMock(return_value=True)),
        ):
            if mode == "spa":
                await _scrape_query_spa(
                    context=mock_context,
                    query="cafe",
                    geo_coordinates=Point(21.0000, 105.8000),
                    zoom=16,
                    max_places=10,
                    range_limit=500.0,
                )
            else:
                await _get_place_urls(
                    context=mock_context,
                    max_places=10,
                    query="cafe",
                    geo_coordinates=Point(21.0000, 105.8000),
                    zoom=16,
                    range_limit=500.0,
                )

    with caplog.at_level(logging.DEBUG):
        asyncio.run(_run())

    debug_drops = [
        rec.message
        for rec in caplog.records
        if rec.levelno == logging.DEBUG and "Early drop" in rec.message
    ]
    info_drops = [
        rec.message
        for rec in caplog.records
        if rec.levelno == logging.INFO and "Early drop" in rec.message
    ]

    assert len(debug_drops) > 0
    assert len(info_drops) == 0


def test_default_spa_preview_timeout_value():
    """Verifies that preview_timeout default is set to 10000ms."""
    assert (
        inspect.signature(scrape_google_maps).parameters["preview_timeout"].default
        == 10000
    )


@pytest.mark.parametrize(
    ("matching_selector", "expected"),
    [
        ("div.Q27duf", True),
        ('text="Google Maps can\'t find"', True),
        ('text="Không tìm thấy kết quả"', True),
        (None, False),
    ],
)
def test_is_no_results_page(matching_selector, expected):
    """Verifies is_no_results_page detects no-result indicators or returns False when none match."""

    async def _run():
        mock_page = AsyncMock()
        mock_loc = AsyncMock()
        mock_loc.count.return_value = 1 if expected else 0
        mock_loc.first.is_visible.return_value = expected

        def mock_locator(sel):
            if matching_selector and matching_selector in sel:
                return mock_loc
            other_loc = AsyncMock()
            other_loc.count.return_value = 0
            other_loc.first.is_visible.return_value = False
            return other_loc

        mock_page.locator = MagicMock(side_effect=mock_locator)
        assert await _is_no_results_page(mock_page) is expected

    asyncio.run(_run())


@pytest.mark.parametrize(
    ("scraper_fn", "expected_result"),
    [
        (_scrape_query_spa, []),
        (_get_place_urls, set()),
    ],
)
def test_no_results_logs_info(caplog, scraper_fn, expected_result):
    """Verifies that scraper logs INFO and returns empty result without ERROR when no results are found."""

    async def _run():
        mock_page = make_mock_page(
            url="https://www.google.com/maps/search/nonexistent_place"
        )
        mock_context = make_mock_context(page=mock_page)

        with (
            patch("map_miner.scraper.asyncio.sleep", AsyncMock()),
            patch("map_miner.scraper._pass_consent", AsyncMock()),
            patch(
                "map_miner.scraper._handle_captcha_if_present",
                AsyncMock(return_value=False),
            ),
            patch(
                "map_miner.scraper._find_feed_selector", AsyncMock(return_value=None)
            ),
            patch(
                "map_miner.scraper._is_no_results_page", AsyncMock(return_value=True)
            ),
        ):
            if scraper_fn is _scrape_query_spa:
                res = await scraper_fn(
                    context=mock_context,
                    query="nonexistent_place",
                    geo_coordinates=Point(21.0, 105.8),
                    zoom=16,
                )
            else:
                res = await scraper_fn(
                    context=mock_context,
                    max_places=10,
                    query="nonexistent_place",
                    geo_coordinates=Point(21.0, 105.8),
                    zoom=16,
                )
            assert res == expected_result

    with caplog.at_level(logging.INFO):
        asyncio.run(_run())

    info_msgs = [rec.message for rec in caplog.records if rec.levelno == logging.INFO]
    error_msgs = [rec.message for rec in caplog.records if rec.levelno == logging.ERROR]

    assert any(
        "No results found for query 'nonexistent_place'." in msg for msg in info_msgs
    )
    assert not any("Could not find results feed selector" in msg for msg in error_msgs)


@pytest.mark.parametrize("use_spa", [True, False])
def test_scrape_google_maps_cancellation_graceful_shutdown(use_spa):
    """Verifies that when scrape_google_maps is cancelled in either SPA or fallback mode,
    sub-tasks are cancelled, the browser is safely closed, and CancelledError is re-raised."""

    async def _run():
        fake_browser = make_fake_browser()
        mock_context = make_mock_context()

        async def slow_query(*args, **kwargs):
            await asyncio.sleep(100.0)
            return [] if use_spa else set()

        patch_target = (
            "map_miner.scraper._scrape_query_spa"
            if use_spa
            else "map_miner.scraper._get_place_urls"
        )

        with (
            patch(
                "map_miner.scraper.async_playwright",
                return_value=MockPlaywrightContext(fake_browser),
            ),
            patch(
                "map_miner.scraper._create_browser_context",
                AsyncMock(return_value=mock_context),
            ),
            patch(patch_target, side_effect=slow_query),
        ):
            task = asyncio.create_task(
                scrape_google_maps(
                    queries={"q1", "q2"} if use_spa else {"q1"},
                    geo_coordinates=Point(21.0, 105.8),
                    zoom=16,
                    use_spa=use_spa,
                    stagger_delay=0,
                )
            )
            await asyncio.sleep(0.05)
            task.cancel()
            cancelled = False
            try:
                await task
            except asyncio.CancelledError:
                cancelled = True

            assert cancelled is True

        fake_browser.close.assert_awaited()
        assert mock_context.close.call_count >= 1

    asyncio.run(_run())


def test_run_spa_query_rescues_results_on_watchdog_timeout():
    """Verifies that when watchdog timeout fires, accumulated places in collector
    are rescued and returned in the DataFrame instead of being discarded."""

    async def _run():
        created_contexts = []
        fake_browser = make_fake_browser(created_contexts)

        rescued_place = {
            "name": "Rescued Cafe",
            "place_id": "res_1",
            "link": "https://maps.google.com/1",
        }

        async def mock_spa_partial(context, query, results_collector=None, **kwargs):
            if results_collector is not None:
                results_collector.append(rescued_place)
            await asyncio.sleep(100.0)
            return results_collector or []

        async def mock_wait_for(coro, timeout):
            task = asyncio.create_task(coro)
            await asyncio.sleep(0.01)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            raise TimeoutError("Hard watchdog timeout")

        with (
            patch(
                "map_miner.scraper.async_playwright",
                return_value=MockPlaywrightContext(fake_browser),
            ),
            patch(
                "map_miner.scraper._scrape_query_spa",
                side_effect=mock_spa_partial,
            ),
            patch("asyncio.wait_for", side_effect=mock_wait_for),
        ):
            df = await scrape_google_maps(
                queries={"cafe"},
                geo_coordinates=Point(21.0, 105.8),
                zoom=16,
                use_spa=True,
                query_timeout=30.0,
            )

        assert len(df) == 1
        assert df["name"][0] == "Rescued Cafe"
        assert len(created_contexts) == 1
        created_contexts[0].close.assert_awaited()
        fake_browser.close.assert_awaited()

    asyncio.run(_run())


def test_scrape_query_spa_timeout_in_element_loop():
    """Verifies that scrape_query_spa halts immediately inside the element loop
    when approaching the deadline (remaining_time <= 2.0s) and returns partial results."""

    async def _run():
        mock_context = AsyncMock()
        mock_page = make_mock_page(url="https://www.google.com/maps/search/cafe")
        mock_context.new_page.return_value = mock_page

        valid_blob = [None] * 20
        valid_blob[11] = "Cafe 1"
        valid_outer = [None, [], None, None, None, None, valid_blob]
        valid_preview = ")]}'\n" + json.dumps(valid_outer)

        mock_resp = AsyncMock()
        mock_resp.url = "https://www.google.com/maps/preview/place/Cafe1"
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

        el2 = AsyncMock()
        el2.get_attribute.return_value = (
            "https://www.google.com/maps/place/Cafe2/data=!1s0x2:0x2"
        )
        el2.evaluate.return_value = None

        def mock_feed_locator(selector):
            loc = AsyncMock()
            loc.count.return_value = 0
            loc.first.is_visible.return_value = False
            if 'a[href*="/maps/place/"]' in selector:
                loc.all.return_value = [el1, el2]
                loc.evaluate_all.return_value = [
                    "https://www.google.com/maps/place/Cafe1",
                    "https://www.google.com/maps/place/Cafe2",
                ]
            return loc

        mock_page.locator = MagicMock(side_effect=mock_feed_locator)

        current_time = 0.0

        def fake_monotonic():
            return current_time

        async def mock_el1_eval(*args, **kwargs):
            nonlocal current_time
            current_time = 9.0

        el1.evaluate.side_effect = mock_el1_eval

        mock_scroll = AsyncMock()
        with (
            patch("map_miner.scraper.asyncio.sleep", AsyncMock()),
            patch("map_miner.scraper.time.monotonic", side_effect=fake_monotonic),
            patch(
                "map_miner.scraper._is_feed_at_end",
                AsyncMock(return_value=False),
            ),
            patch("map_miner.scraper._scroll_feed", mock_scroll),
        ):
            results = await _scrape_query_spa(
                context=mock_context,
                query="cafe",
                geo_coordinates=Point(21.0, 105.8),
                zoom=16,
                max_places=10,
                query_timeout=10.0,
            )

        assert len(results) == 1
        assert results[0]["name"] == "Cafe 1"
        el2.evaluate.assert_not_awaited()
        mock_scroll.assert_not_awaited()

    asyncio.run(_run())


def test_scrape_query_spa_early_stop_out_of_range():
    """Verifies that scrape_query_spa halts early after 3 consecutive scrolls where all new items are out of range."""

    async def _run():
        mock_context = AsyncMock()
        mock_page = make_mock_page(url="https://www.google.com/maps/search/courthouse")
        mock_context.new_page.return_value = mock_page

        scroll_idx = 0

        def mock_feed_locator(selector):
            nonlocal scroll_idx
            loc = AsyncMock()
            loc.count.return_value = 0
            loc.first.is_visible.return_value = False
            if 'a[href*="/maps/place/"]' in selector:
                el = AsyncMock()
                url = f"https://www.google.com/maps/place/FarCourt{scroll_idx}/data=!1s0x{scroll_idx}:0x{scroll_idx}!8m2!3d21.02!4d105.8"
                el.get_attribute.return_value = url
                el.evaluate.return_value = None
                loc.all.return_value = [el]
                loc.evaluate_all.return_value = [url]
            return loc

        mock_page.locator = MagicMock(side_effect=mock_feed_locator)

        height_counter = 0

        async def mock_eval(script, *args):
            nonlocal height_counter
            height_counter += 200
            return height_counter

        mock_page.evaluate = AsyncMock(side_effect=mock_eval)

        scroll_count = 0

        async def mock_scroll(page, selector):
            nonlocal scroll_count, scroll_idx
            scroll_count += 1
            scroll_idx += 1

        with (
            patch("map_miner.scraper.asyncio.sleep", AsyncMock()),
            patch("map_miner.scraper._scroll_feed", side_effect=mock_scroll),
            patch(
                "map_miner.scraper._is_feed_at_end",
                AsyncMock(return_value=False),
            ),
        ):
            results = await _scrape_query_spa(
                context=mock_context,
                query="courthouse",
                geo_coordinates=Point(21.0, 105.8),
                zoom=16,
                max_places=10,
                range_limit=500.0,
            )

        assert len(results) == 0
        assert scroll_count == 3

    asyncio.run(_run())


def test_spa_post_extraction_range_filter():
    """Verifies that a place without URL coordinates is filtered out post-extraction
    if its extracted coordinates exceed range_limit."""

    async def _run():
        mock_context = AsyncMock()
        mock_page = make_mock_page(url="https://www.google.com/maps/search/cafe")
        mock_context.new_page.return_value = mock_page

        url = "https://www.google.com/maps/place/DistantCafe/data=!1s0x123:0x456"
        el = AsyncMock()
        el.get_attribute.return_value = url
        el.evaluate.return_value = None

        def mock_feed_locator(selector):
            loc = AsyncMock()
            loc.count.return_value = 0
            loc.first.is_visible.return_value = False
            if 'a[href*="/maps/place/"]' in selector:
                loc.all.return_value = [el]
                loc.evaluate_all.return_value = [url]
            return loc

        mock_page.locator = MagicMock(side_effect=mock_feed_locator)

        # Preview JSON with coordinates ~110km away (22.0, 106.0 vs 21.0, 105.8)
        valid_blob = [None] * 20
        valid_blob[11] = "Distant Cafe"
        valid_blob[9] = [None, None, 22.0, 106.0]
        valid_outer = [None, [], None, None, None, None, valid_blob]
        valid_preview = ")]}'\n" + json.dumps(valid_outer)

        mock_resp = AsyncMock()
        mock_resp.url = "https://www.google.com/maps/preview/place/DistantCafe"
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

        with (
            patch("map_miner.scraper.asyncio.sleep", AsyncMock()),
            patch("map_miner.scraper._is_feed_at_end", AsyncMock(return_value=True)),
            patch("map_miner.scraper._scroll_feed", AsyncMock()),
        ):
            results = await _scrape_query_spa(
                context=mock_context,
                query="cafe",
                geo_coordinates=Point(21.0, 105.8),
                zoom=16,
                max_places=10,
                range_limit=500.0,
            )

        assert len(results) == 0

    asyncio.run(_run())


def test_safe_close_context_cancellation():
    """Verifies that safe_close_context properly unroutes and awaits context.close()
    even when the enclosing task is cancelled."""

    async def _run():
        mock_context = AsyncMock()
        mock_context.unroute = AsyncMock()
        mock_context.close = AsyncMock()

        async def worker():
            try:
                await asyncio.sleep(100.0)
            finally:
                await _safe_close_context(mock_context)

        task = asyncio.create_task(worker())
        await asyncio.sleep(0.01)
        task.cancel()

        cancelled = False
        try:
            await task
        except asyncio.CancelledError:
            cancelled = True

        assert cancelled is True
        mock_context.unroute.assert_awaited_once_with("**/*")
        mock_context.close.assert_awaited_once()

    asyncio.run(_run())


def test_global_route_handler_cancelled_no_error():
    """Verifies that global_route_handler cleanly handles cancellation and PlaywrightError
    without attempting recursive route.continue_() calls."""

    async def _run():
        mock_route = AsyncMock()
        mock_route.request = MagicMock()
        mock_route.request.url = "https://maps.googleapis.com/maps/api/js"
        mock_route.request.resource_type = "image"
        mock_route.abort.side_effect = asyncio.CancelledError("Task cancelled")

        # Must not raise CancelledError or PlaywrightError
        await _global_route_handler(mock_route)

        mock_route.continue_.assert_not_awaited()

    asyncio.run(_run())


def test_safe_close_page_cancellation():
    """Verifies that safe_close_page properly closes the page even when enclosing task is cancelled."""

    async def _run():
        mock_page = AsyncMock()
        mock_page.is_closed = MagicMock(return_value=False)
        mock_page.close = AsyncMock()

        async def worker():
            try:
                await asyncio.sleep(100.0)
            finally:
                await _safe_close_page(mock_page)

        task = asyncio.create_task(worker())
        await asyncio.sleep(0.01)
        task.cancel()

        cancelled = False
        try:
            await task
        except asyncio.CancelledError:
            cancelled = True

        assert cancelled is True
        mock_page.close.assert_awaited_once()

    asyncio.run(_run())


def test_scraper_default_constants():
    """Kiểm tra các giá trị tham số mặc định của scrape_google_maps."""
    sig = inspect.signature(scrape_google_maps)
    params = sig.parameters

    assert params["navigation_timeout"].default == 30000
    assert params["query_timeout"].default == 300.0
    assert params["place_timeout"].default == 45.0
    assert params["captcha_timeout"].default == 85.0
    assert params["preview_timeout"].default == 10000
    assert params["range_limit"].default == 10000.0
    assert params["max_captcha_retries"].default == 2
    assert params["stagger_delay"].default == (1.5, 3.5)
    assert params["cache_dir"].default == Path(".cache") / "chromium_cache"
    assert params["static_cache_dir"].default == Path(".cache") / "static_assets"
    assert params["disk_cache_size"].default == 1073741824
    assert params["max_consecutive_empty_scrolls"].default == 4
    assert params["max_consecutive_out_of_range_scrolls"].default == 3
    assert params["max_scroll_attempts_without_new_links"].default == 5


def test_scrape_query_spa_fallback_rescue_from_dom():
    """Kiểm tra Fallback Rescue cứu được dữ liệu từ feed card DOM khi preview XHR bị timeout/lỗi."""

    async def _run():
        mock_page = make_mock_page(url="https://www.google.com/maps/search/cafe")
        mock_context = make_mock_context(page=mock_page)

        el = AsyncMock()
        url = "https://www.google.com/maps/place/RescuedCafe/data=!1s0x31752:0x7c963!8m2!3d21.01!4d105.81"
        el.get_attribute.return_value = url

        # When el.evaluate is called for card outerHTML, return sample feed card HTML
        card_html = """
        <div class="Nv2PK">
            <div class="qBF1Pd">Rescued Cafe</div>
            <span class="MW4etd">4.7</span>
            <span class="UY7F9">(89)</span>
            <div class="W4Efsd"><span>Quán cà phê</span> · <span>456 Tran Hung Dao</span></div>
        </div>
        """

        async def mock_el_eval(script, *args):
            if "closest" in script:
                return card_html
            return None

        el.evaluate = AsyncMock(side_effect=mock_el_eval)

        def mock_feed_locator(selector):
            loc = AsyncMock()
            loc.count.return_value = 0
            loc.first.is_visible.return_value = False
            if 'a[href*="/maps/place/"]' in selector:
                loc.all.return_value = [el]
                loc.evaluate_all.return_value = [url]
            return loc

        mock_page.locator = MagicMock(side_effect=mock_feed_locator)
        mock_page.evaluate = AsyncMock(return_value=1000)

        # Mock expect_response to fail with timeout
        class MockFailingExpectResponse:
            async def __aenter__(self):
                from playwright.async_api import (
                    TimeoutError as PlaywrightTimeoutError,
                )

                raise PlaywrightTimeoutError("Timeout waiting for preview")

            async def __aexit__(self, *args):
                return None

        mock_page.expect_response = MagicMock(return_value=MockFailingExpectResponse())

        with (
            patch("map_miner.scraper.asyncio.sleep", AsyncMock()),
            patch("map_miner.scraper._scroll_feed", AsyncMock()),
            patch("map_miner.scraper._is_feed_at_end", AsyncMock(return_value=True)),
        ):
            results = await _scrape_query_spa(
                context=mock_context,
                query="cafe",
                geo_coordinates=Point(21.0, 105.8),
                zoom=16,
                max_places=1,
                range_limit=5000.0,
            )

        assert len(results) == 1
        assert results[0].get("name") == "Rescued Cafe"
        assert results[0].get("rating") == 4.7
        assert results[0].get("reviews_count") == 89
        assert "Quán cà phê" in results[0].get("categories", [])

    asyncio.run(_run())


def test_scrape_google_maps_forwards_all_custom_parameters_spa(tmp_path):
    """Verifies that scrape_google_maps forwards all 14 custom parameters down
    to Chromium launch args, _create_browser_context, and _scrape_query_spa in SPA mode."""

    async def _run():
        fake_browser = make_fake_browser()
        mock_context = make_mock_context()
        custom_cache_dir = tmp_path / "custom_chromium_cache"
        custom_static_cache_dir = tmp_path / "custom_static_cache"
        mock_playwright_ctx = MockPlaywrightContext(fake_browser)

        mock_spa = AsyncMock(
            return_value=[{"name": "Custom Cafe", "link": "https://maps.google.com/1"}]
        )
        mock_create_ctx = AsyncMock(return_value=mock_context)

        with (
            patch(
                "map_miner.scraper.async_playwright",
                return_value=mock_playwright_ctx,
            ),
            patch("map_miner.scraper._create_browser_context", mock_create_ctx),
            patch("map_miner.scraper._scrape_query_spa", mock_spa),
        ):
            df = await scrape_google_maps(
                queries={"custom_cafe"},
                geo_coordinates=Point(21.0, 105.8),
                zoom=15,
                use_spa=True,
                stagger_delay=0,
                cache_dir=custom_cache_dir,
                range_limit=7777.0,
                query_timeout=120.0,
                place_timeout=50.0,
                preview_timeout=4500,
                navigation_timeout=15000,
                captcha_timeout=45.0,
                max_captcha_retries=5,
                static_cache_dir=custom_static_cache_dir,
                disk_cache_size=50000000,
                max_consecutive_empty_scrolls=2,
                max_consecutive_out_of_range_scrolls=1,
                max_scroll_attempts_without_new_links=3,
            )

        assert len(df) == 1
        assert df["name"][0] == "Custom Cafe"

        # Check Chromium launch args
        launch_args = mock_playwright_ctx.mock_playwright.chromium.launch.call_args[1][
            "args"
        ]
        assert f"--disk-cache-dir={custom_cache_dir.resolve()}" in launch_args
        assert "--disk-cache-size=50000000" in launch_args

        # Check _create_browser_context arguments
        assert mock_create_ctx.call_count >= 1
        assert (
            mock_create_ctx.call_args.kwargs["static_cache_dir"]
            == custom_static_cache_dir
        )

        # Check _scrape_query_spa arguments
        mock_spa.assert_awaited_once()
        spa_kwargs = mock_spa.call_args.kwargs
        assert spa_kwargs["range_limit"] == 7777.0
        assert spa_kwargs["query_timeout"] == 120.0
        assert spa_kwargs["preview_timeout"] == 4500
        assert spa_kwargs["navigation_timeout"] == 15000
        assert spa_kwargs["captcha_timeout"] == 45.0
        assert spa_kwargs["max_captcha_retries"] == 5
        assert spa_kwargs["static_cache_dir"] == custom_static_cache_dir
        assert spa_kwargs["max_consecutive_empty_scrolls"] == 2
        assert spa_kwargs["max_consecutive_out_of_range_scrolls"] == 1
        assert spa_kwargs["max_scroll_attempts_without_new_links"] == 3

    asyncio.run(_run())


def test_scrape_google_maps_forwards_all_custom_parameters_fallback(tmp_path):
    """Verifies that scrape_google_maps forwards all custom parameters down
    to _get_place_urls and _process_link in multi-page fallback mode."""

    async def _run():
        fake_browser = make_fake_browser()
        mock_context = make_mock_context()
        custom_cache_dir = tmp_path / "custom_chromium_cache_fallback"
        custom_static_cache_dir = tmp_path / "custom_static_cache_fallback"
        mock_playwright_ctx = MockPlaywrightContext(fake_browser)

        mock_get_urls = AsyncMock(return_value={"https://maps.google.com/place_1"})
        mock_process = AsyncMock(
            return_value={
                "name": "Fallback Cafe",
                "link": "https://maps.google.com/place_1",
            }
        )
        mock_create_ctx = AsyncMock(return_value=mock_context)

        with (
            patch(
                "map_miner.scraper.async_playwright",
                return_value=mock_playwright_ctx,
            ),
            patch("map_miner.scraper._create_browser_context", mock_create_ctx),
            patch("map_miner.scraper._get_place_urls", mock_get_urls),
            patch("map_miner.scraper._process_link", mock_process),
        ):
            df = await scrape_google_maps(
                queries={"fallback_cafe"},
                geo_coordinates=Point(21.0, 105.8),
                zoom=15,
                use_spa=False,
                stagger_delay=0,
                cache_dir=custom_cache_dir,
                range_limit=8888.0,
                query_timeout=140.0,
                place_timeout=55.0,
                preview_timeout=3500,
                navigation_timeout=16000,
                captcha_timeout=48.0,
                max_captcha_retries=3,
                static_cache_dir=custom_static_cache_dir,
                disk_cache_size=60000000,
                max_consecutive_empty_scrolls=2,
                max_consecutive_out_of_range_scrolls=1,
                max_scroll_attempts_without_new_links=3,
            )

        assert len(df) == 1
        assert df["name"][0] == "Fallback Cafe"

        # Check Chromium launch args
        launch_args = mock_playwright_ctx.mock_playwright.chromium.launch.call_args[1][
            "args"
        ]
        assert f"--disk-cache-dir={custom_cache_dir.resolve()}" in launch_args
        assert "--disk-cache-size=60000000" in launch_args

        # Check _create_browser_context received static_cache_dir
        assert mock_create_ctx.call_count >= 1
        for call in mock_create_ctx.call_args_list:
            assert call.kwargs["static_cache_dir"] == custom_static_cache_dir

        # Check _get_place_urls arguments
        mock_get_urls.assert_awaited_once()
        urls_kwargs = mock_get_urls.call_args.kwargs
        assert urls_kwargs["range_limit"] == 8888.0
        assert urls_kwargs["query_timeout"] == 140.0
        assert urls_kwargs["navigation_timeout"] == 16000
        assert urls_kwargs["captcha_timeout"] == 48.0
        assert urls_kwargs["max_consecutive_empty_scrolls"] == 2
        assert urls_kwargs["max_consecutive_out_of_range_scrolls"] == 1
        assert urls_kwargs["max_scroll_attempts_without_new_links"] == 3

        # Check _process_link arguments
        mock_process.assert_awaited_once()
        process_kwargs = mock_process.call_args.kwargs
        assert process_kwargs["navigation_timeout"] == 16000
        assert process_kwargs["captcha_timeout"] == 48.0

    asyncio.run(_run())


def test_scrape_query_spa_custom_guardrails():
    """Verifies that _scrape_query_spa respects custom navigation_timeout,
    captcha_timeout, max_consecutive_empty_scrolls, and max_consecutive_out_of_range_scrolls."""

    async def _run():
        mock_context = make_mock_context()
        mock_page = make_mock_page(url="https://www.google.com/maps/search/cafe")
        mock_context.new_page = AsyncMock(return_value=mock_page)

        # 1. Test custom navigation_timeout and captcha_timeout
        mock_handle_captcha = AsyncMock(return_value=False)
        with (
            patch("map_miner.scraper.asyncio.sleep", AsyncMock()),
            patch("map_miner.scraper._pass_consent", AsyncMock()),
            patch(
                "map_miner.scraper._handle_captcha_if_present",
                mock_handle_captcha,
            ),
            patch(
                "map_miner.scraper._find_feed_selector",
                AsyncMock(return_value=None),
            ),
            patch(
                "map_miner.scraper._is_no_results_page",
                AsyncMock(return_value=True),
            ),
        ):
            await _scrape_query_spa(
                context=mock_context,
                query="cafe",
                geo_coordinates=Point(21.0, 105.8),
                zoom=16,
                navigation_timeout=12345,
                captcha_timeout=67.0,
            )

        assert mock_page.goto.call_args[1]["timeout"] == 12345
        assert mock_handle_captcha.call_args[1]["timeout"] == 67.0

        # 2. Test max_consecutive_empty_scrolls=2 stops early after 2 scrolls
        mock_scroll = AsyncMock()
        mock_locator = MagicMock()
        mock_locator.all = AsyncMock(return_value=[])
        mock_locator.evaluate_all = AsyncMock(return_value=[])
        mock_page.locator = MagicMock(return_value=mock_locator)
        # Varying heights so scroll height change check does not trigger
        mock_page.evaluate = AsyncMock(side_effect=[100, 200, 300, 400, 500])

        with (
            patch("map_miner.scraper.asyncio.sleep", AsyncMock()),
            patch("map_miner.scraper._pass_consent", AsyncMock()),
            patch(
                "map_miner.scraper._handle_captcha_if_present",
                AsyncMock(return_value=False),
            ),
            patch(
                "map_miner.scraper._find_feed_selector",
                AsyncMock(return_value='[role="feed"]'),
            ),
            patch(
                "map_miner.scraper._is_feed_at_end",
                AsyncMock(return_value=False),
            ),
            patch("map_miner.scraper._scroll_feed", mock_scroll),
        ):
            res_empty = await _scrape_query_spa(
                context=mock_context,
                query="cafe",
                geo_coordinates=Point(21.0, 105.8),
                zoom=16,
                max_places=10,
                max_consecutive_empty_scrolls=2,
            )

        assert res_empty == []
        assert mock_scroll.call_count == 2

        # 3. Test max_consecutive_out_of_range_scrolls=2 stops early when links are out of range
        mock_scroll_oor = AsyncMock()
        oor_counter = 0

        async def mock_get_oor_attr(*args, **kwargs):
            nonlocal oor_counter
            oor_counter += 1
            return f"https://www.google.com/maps/place/Far{oor_counter}/@0.0,0.0,17z/data={oor_counter}"

        out_of_range_el = AsyncMock()
        out_of_range_el.get_attribute = AsyncMock(side_effect=mock_get_oor_attr)
        mock_locator_oor = MagicMock()
        mock_locator_oor.all = AsyncMock(return_value=[out_of_range_el])
        mock_locator_oor.evaluate_all = AsyncMock(
            return_value=["https://www.google.com/maps/place/Far/@0.0,0.0,17z/data=123"]
        )
        mock_page.locator = MagicMock(return_value=mock_locator_oor)
        mock_page.evaluate = AsyncMock(side_effect=[100, 200, 300, 400, 500])

        with (
            patch("map_miner.scraper.asyncio.sleep", AsyncMock()),
            patch("map_miner.scraper._pass_consent", AsyncMock()),
            patch(
                "map_miner.scraper._handle_captcha_if_present",
                AsyncMock(return_value=False),
            ),
            patch(
                "map_miner.scraper._find_feed_selector",
                AsyncMock(return_value='[role="feed"]'),
            ),
            patch(
                "map_miner.scraper._is_feed_at_end",
                AsyncMock(return_value=False),
            ),
            patch("map_miner.scraper._scroll_feed", mock_scroll_oor),
        ):
            res_oor = await _scrape_query_spa(
                context=mock_context,
                query="cafe",
                geo_coordinates=Point(21.0, 105.8),
                zoom=16,
                range_limit=500.0,
                max_places=10,
                max_consecutive_out_of_range_scrolls=2,
            )

        assert res_oor == []
        assert mock_scroll_oor.call_count == 2

    asyncio.run(_run())


def test_get_place_urls_custom_guardrails():
    """Verifies that _get_place_urls respects custom navigation_timeout,
    captcha_timeout, max_consecutive_empty_scrolls, and max_consecutive_out_of_range_scrolls."""

    async def _run():
        mock_context = make_mock_context()
        mock_page = make_mock_page(url="https://www.google.com/maps/search/cafe")
        mock_context.new_page = AsyncMock(return_value=mock_page)

        # 1. Test custom navigation_timeout and captcha_timeout
        mock_handle_captcha = AsyncMock(return_value=False)
        with (
            patch("map_miner.scraper.asyncio.sleep", AsyncMock()),
            patch("map_miner.scraper._pass_consent", AsyncMock()),
            patch(
                "map_miner.scraper._handle_captcha_if_present",
                mock_handle_captcha,
            ),
            patch(
                "map_miner.scraper._find_feed_selector",
                AsyncMock(return_value=None),
            ),
            patch(
                "map_miner.scraper._is_no_results_page",
                AsyncMock(return_value=True),
            ),
        ):
            await _get_place_urls(
                context=mock_context,
                max_places=10,
                query="cafe",
                geo_coordinates=Point(21.0, 105.8),
                zoom=16,
                navigation_timeout=22222,
                captcha_timeout=66.0,
            )

        assert mock_page.goto.call_args[1]["timeout"] == 22222
        assert mock_handle_captcha.call_args[1]["timeout"] == 66.0

        # 2. Test max_consecutive_empty_scrolls=2 stops early
        mock_scroll = AsyncMock()
        mock_locator = MagicMock()
        mock_locator.evaluate_all = AsyncMock(return_value=[])
        mock_page.locator = MagicMock(return_value=mock_locator)
        mock_page.evaluate = AsyncMock(side_effect=[100, 200, 300, 400, 500])

        with (
            patch("map_miner.scraper.asyncio.sleep", AsyncMock()),
            patch("map_miner.scraper._pass_consent", AsyncMock()),
            patch(
                "map_miner.scraper._handle_captcha_if_present",
                AsyncMock(return_value=False),
            ),
            patch(
                "map_miner.scraper._find_feed_selector",
                AsyncMock(return_value='[role="feed"]'),
            ),
            patch(
                "map_miner.scraper._is_feed_at_end",
                AsyncMock(return_value=False),
            ),
            patch("map_miner.scraper._scroll_feed", mock_scroll),
        ):
            urls = await _get_place_urls(
                context=mock_context,
                max_places=10,
                query="cafe",
                geo_coordinates=Point(21.0, 105.8),
                zoom=16,
                max_consecutive_empty_scrolls=2,
            )

        assert urls == set()
        assert mock_scroll.call_count == 2

        # 3. Test max_consecutive_out_of_range_scrolls=2 stops early
        mock_scroll_oor = AsyncMock()
        oor_count = 0

        def get_oor_links(*args, **kwargs):
            nonlocal oor_count
            oor_count += 1
            return [
                f"https://www.google.com/maps/place/Far{oor_count}/@0.0,0.0,17z/data={oor_count}"
            ]

        mock_locator_oor = MagicMock()
        mock_locator_oor.evaluate_all = AsyncMock(side_effect=get_oor_links)
        mock_page.locator = MagicMock(return_value=mock_locator_oor)
        mock_page.evaluate = AsyncMock(side_effect=[100, 200, 300, 400, 500])

        with (
            patch("map_miner.scraper.asyncio.sleep", AsyncMock()),
            patch("map_miner.scraper._pass_consent", AsyncMock()),
            patch(
                "map_miner.scraper._handle_captcha_if_present",
                AsyncMock(return_value=False),
            ),
            patch(
                "map_miner.scraper._find_feed_selector",
                AsyncMock(return_value='[role="feed"]'),
            ),
            patch(
                "map_miner.scraper._is_feed_at_end",
                AsyncMock(return_value=False),
            ),
            patch("map_miner.scraper._scroll_feed", mock_scroll_oor),
        ):
            urls_oor = await _get_place_urls(
                context=mock_context,
                max_places=10,
                query="cafe",
                geo_coordinates=Point(21.0, 105.8),
                zoom=16,
                range_limit=500.0,
                max_consecutive_out_of_range_scrolls=2,
            )

        assert urls_oor == set()
        assert mock_scroll_oor.call_count == 2

    asyncio.run(_run())


def test_create_browser_context_custom_static_cache_dir(tmp_path):
    """Verifies that _create_browser_context routes requests using custom static_cache_dir."""

    async def _run():
        custom_dir = tmp_path / "custom_static_assets"
        fake_browser = make_fake_browser()

        context = await _create_browser_context(
            browser=fake_browser,
            geo_coordinates=Point(21.0, 105.8),
            static_cache_dir=custom_dir,
        )

        mock_ctx = cast(Any, context)
        assert mock_ctx.route.call_count >= 1
        route_pattern, registered_handler = mock_ctx.route.call_args[0]
        assert route_pattern == "**/*"

        # Simulate route handler execution with static asset
        test_url = "https://maps.gstatic.com/tactile/omnibox/cleardot.png"
        mock_req = MagicMock()
        mock_req.url = test_url
        mock_req.method = "GET"
        mock_req.resource_type = "other"

        mock_route = AsyncMock()
        mock_route.request = mock_req
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.body = AsyncMock(return_value=b"custom_cached_image_bytes")
        mock_route.fetch = AsyncMock(return_value=mock_response)
        mock_route.fulfill = AsyncMock()

        await registered_handler(mock_route)

        # Verify cached asset was written into custom_dir
        cache_key = hashlib.sha256(test_url.encode("utf-8")).hexdigest()
        saved_file = custom_dir / cache_key
        assert saved_file.is_file()
        assert saved_file.read_bytes() == b"custom_cached_image_bytes"

        # Verify second call fulfills from custom_dir cache
        mock_route_2 = AsyncMock()
        mock_route_2.request = mock_req
        mock_route_2.fulfill = AsyncMock()

        await registered_handler(mock_route_2)

        mock_route_2.fulfill.assert_awaited_once()
        fulfill_kwargs = mock_route_2.fulfill.call_args.kwargs
        assert fulfill_kwargs["body"] == b"custom_cached_image_bytes"
        assert fulfill_kwargs["headers"]["x-cache"] == "HIT-ROUTE-CACHE"

    asyncio.run(_run())
