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
    FEED_FALLBACK_SELECTORS,
    PreviewInterceptor,
    is_preview_response_for_link,
    make_place_url,
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
