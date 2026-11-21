import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

from geopy.point import Point

from map_miner.scraper import (
    _PreviewInterceptor,
    _process_link,
    scrape_google_maps,
)

ORIGIN = Point(21.0, 105.8)
BASE = "https://www.google.com/maps/place/"


def _mock_browser():
    browser = AsyncMock()
    browser.is_connected = MagicMock(return_value=True)
    manager = AsyncMock()
    manager.__aenter__.return_value.chromium.launch.return_value = browser
    return manager


def _mock_page(url):
    page = AsyncMock()
    page.url = url
    page.is_closed = MagicMock(return_value=False)
    page.goto.return_value = MagicMock(status=200)
    context = AsyncMock()
    context.new_page.return_value = page
    return page, context


def _preview(identity, name, coordinates=None):
    blob: list[object] = [None] * 12
    if coordinates is not None:
        blob[9] = [None, None, *coordinates]
    blob[10] = identity
    blob[11] = name
    return json.dumps([None] * 6 + [blob])


def test_spa_detail_interceptor_requires_target_response_and_matching_body():
    async def run():
        page = MagicMock()
        link = BASE + "Cafe/data=!1s0x1:0x23"
        interceptor = _PreviewInterceptor(page, target_link=link)
        prefix = "https://www.google.com/maps/preview/place?pb=!1s"

        unrelated = MagicMock(url=prefix + "0x1:0x234", status=200, ok=True)
        unrelated.text = AsyncMock(return_value=_preview("0x1:0x234", "Other"))
        await interceptor._handle_response(unrelated)
        unrelated.text.assert_not_awaited()

        same_url = MagicMock(url=prefix + "0x1:0x23", status=200, ok=True)
        payload = _preview("0x1:0x23", "Target")
        same_url.text = AsyncMock(side_effect=[_preview("0x1:0x234", "Other"), payload])
        await interceptor._handle_response(same_url)
        assert not interceptor.preview_event.is_set()
        await interceptor._handle_response(same_url)
        assert await interceptor.wait_for_preview() == payload

    asyncio.run(run())


def test_spa_detail_checks_preview_position_even_when_only_link_requested():
    async def run():
        link = BASE + "Cafe/@0,0,17z/data=!1s0x1:0x23"
        page, context = _mock_page(link)
        interceptor = MagicMock()
        interceptor.wait_for_preview = AsyncMock(
            side_effect=[
                _preview("0x1:0x23", "Cafe", (0.0, 0.0)),
                _preview("0x1:0x23", "Cafe", (21.0, 105.8)),
            ]
        )
        rejected = []

        with patch("map_miner.scraper._PreviewInterceptor", return_value=interceptor):
            far = await _process_link(
                context,
                link,
                geo_coordinates=ORIGIN,
                range_limit=500,
                fields=["link"],
                out_of_range=rejected,
            )
            near = await _process_link(
                context,
                link,
                geo_coordinates=ORIGIN,
                range_limit=500,
                fields=["link"],
                out_of_range=[],
            )

        assert far is None
        assert rejected == [True]
        assert near is not None and near["link"] == link
        page.content.assert_not_awaited()

    asyncio.run(run())


def test_spa_detail_checks_place_coordinates_from_final_detail_url():
    async def run():
        link = BASE + "Cafe/@21,105.8,17z"
        final_url = BASE + "Cafe/data=!1s0x1:0x23!8m2!3d0!4d0"
        page, context = _mock_page(final_url)
        page.content.return_value = "<h1>Cafe</h1>"
        rejected = []
        interceptor = MagicMock()
        interceptor.wait_for_preview = AsyncMock(return_value=None)

        with (
            patch("map_miner.scraper._PreviewInterceptor", return_value=interceptor),
            patch("map_miner.scraper._handle_captcha_if_present", AsyncMock()),
        ):
            place = await _process_link(
                context,
                link,
                geo_coordinates=ORIGIN,
                range_limit=500,
                fields=["link"],
                out_of_range=rejected,
            )

        assert place is None
        assert rejected == [True]

    asyncio.run(run())


def test_spa_reports_only_affected_original_query_and_keeps_partial_row():
    async def run():
        issues = {}

        async def scrape(query, query_issues, **kwargs):
            if query == "court house":
                query_issues.append("deadline reached")
                return [{"name": "Court", "link": BASE + "Court"}]
            return []

        with (
            patch("map_miner.scraper.async_playwright", return_value=_mock_browser()),
            patch("map_miner.scraper._create_browser_context", AsyncMock()),
            patch("map_miner.scraper._scrape_query_spa", side_effect=scrape),
        ):
            frame = await scrape_google_maps(
                {"court_house", "empty"},
                ORIGIN,
                18,
                stagger_delay=0,
                on_query_incomplete=issues.__setitem__,
            )

        assert frame["query"].to_list() == ["court_house"]
        assert issues == {"court_house": ("deadline reached",)}

    asyncio.run(run())
