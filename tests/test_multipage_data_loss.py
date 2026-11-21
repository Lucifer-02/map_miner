import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

from geopy.point import Point

from map_miner.scraper import (
    _get_place_urls,
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


def test_multipage_reads_initial_batch_and_continues_after_far_progress():
    async def run():
        initial = BASE + "Initial/data=!8m2!3d21.0!4d105.8"
        venue = BASE + "Venue/@0,0,17z"
        batches = [[initial]]
        batches.extend(
            [f"{BASE}Far{index}/data=!8m2!3d0.0!4d0.0"] for index in range(5)
        )
        batches.append([venue])
        page, context = _mock_page("https://www.google.com/maps/search/cafe")
        page.evaluate.return_value = 100
        locator = MagicMock()
        count = 0
        locator.evaluate_all = AsyncMock(
            side_effect=lambda _: batches[min(count, len(batches) - 1)]
        )
        page.locator = MagicMock(return_value=locator)

        async def scroll(*args):
            nonlocal count
            count += 1

        with (
            patch(
                "map_miner.scraper._find_feed_selector", AsyncMock(return_value="feed")
            ),
            patch("map_miner.scraper._handle_captcha_if_present", AsyncMock()),
            patch("map_miner.scraper._scroll_feed", side_effect=scroll),
            patch(
                "map_miner.scraper._is_feed_at_end",
                side_effect=lambda _: count >= len(batches),
            ),
        ):
            result = await _get_place_urls(
                context,
                10,
                "cafe",
                ORIGIN,
                18,
                range_limit=500,
                initial_delay_range=(0, 0),
                max_consecutive_empty_scrolls=2,
                max_consecutive_out_of_range_scrolls=1,
                max_scroll_attempts_without_new_links=1,
            )

        assert result == {initial, venue}
        assert count == len(batches)

    asyncio.run(run())


def test_multipage_interceptor_requires_target_response_and_matching_body():
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


def test_multipage_checks_preview_position_even_when_only_link_requested():
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
                asyncio.Semaphore(1),
                1,
                2,
                geo_coordinates=ORIGIN,
                range_limit=500,
                fields=["link"],
                out_of_range=rejected,
            )
            near = await _process_link(
                context,
                link,
                asyncio.Semaphore(1),
                2,
                2,
                geo_coordinates=ORIGIN,
                range_limit=500,
                fields=["link"],
            )

        assert far is None
        assert rejected == [True]
        assert near is not None and near["link"] == link
        page.content.assert_not_awaited()

    asyncio.run(run())


def test_multipage_checks_place_coordinates_from_final_detail_url():
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
                asyncio.Semaphore(1),
                1,
                1,
                geo_coordinates=ORIGIN,
                range_limit=500,
                fields=["link"],
                out_of_range=rejected,
            )

        assert place is None
        assert rejected == [True]

    asyncio.run(run())


def test_multipage_fills_quota_after_failed_far_and_duplicate_details():
    async def run():
        links = [BASE + str(index) for index in range(7)]
        visited = []
        issues = {}

        async def get_urls(**kwargs):
            assert await kwargs["process_candidates"](links[:2]) is False
            assert await kwargs["process_candidates"](links[2:]) is True
            return set(links)

        async def process_link(context, link, *args, out_of_range, **kwargs):
            index = links.index(link)
            visited.append(index)
            if index == 0:
                return None
            if index == 1:
                out_of_range.append(True)
                return None
            if index in (2, 3):
                return {"name": "Same", "link": link, "place_id": "same"}
            return {"name": "Unique", "link": link, "place_id": "other"}

        with (
            patch("map_miner.scraper.async_playwright", return_value=_mock_browser()),
            patch("map_miner.scraper._create_browser_context", AsyncMock()),
            patch("map_miner.scraper._get_place_urls", side_effect=get_urls),
            patch("map_miner.scraper._process_link", side_effect=process_link),
        ):
            frame = await scrape_google_maps(
                {"court_house"},
                ORIGIN,
                18,
                use_spa=False,
                max_places=2,
                fields=["link"],
                on_query_incomplete=issues.__setitem__,
            )

        assert frame.to_dicts() == [
            {"link": links[2], "query": "court_house"},
            {"link": links[4], "query": "court_house"},
        ]
        assert visited == [0, 1, 2, 3, 4]
        assert issues == {"court_house": ("could not extract 1 place detail(s)",)}

    asyncio.run(run())


def test_multipage_watchdogs_keep_rows_already_extracted_before_cleanup():
    async def run():
        link = BASE + "Cafe"
        issues = {}

        async def get_urls(**kwargs):
            kwargs["links_collector"].add(link)
            await kwargs["process_candidates"]([link])
            await asyncio.sleep(1)

        async def process_link(*args, result_collector, **kwargs):
            result_collector.append({"name": "Saved", "link": link})
            await asyncio.sleep(1)

        with (
            patch("map_miner.scraper.async_playwright", return_value=_mock_browser()),
            patch("map_miner.scraper._create_browser_context", AsyncMock()),
            patch("map_miner.scraper._get_place_urls", side_effect=get_urls),
            patch("map_miner.scraper._process_link", side_effect=process_link),
        ):
            frame = await scrape_google_maps(
                {"cafe"},
                ORIGIN,
                18,
                use_spa=False,
                query_timeout=0.02,
                watchdog_grace_period=0,
                place_timeout=0.06,
                on_query_incomplete=issues.__setitem__,
            )

        assert frame["name"].to_list() == ["Saved"]
        assert issues == {"cafe": ("hard search deadline reached",)}

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
