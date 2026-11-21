import asyncio
import json
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from geopy.point import Point
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from map_miner.scraper import _scrape_query_spa

SEARCH_URL = "https://www.google.com/maps/search/cafe"
NEAR = (21.0, 105.8)
FAR = (22.0, 106.0)
CAFE = "/maps/place/Cafe/data=!1s0x1:0x2!8m2!3d21.0!4d105.8"


@dataclass
class Card:
    link: str
    preview: str | None = None
    html: str = ""
    changed_before_click: bool = False


def preview(
    name: str | None = None,
    place_id: str | None = None,
    coords: tuple[float, float] | None = None,
    rating: float | None = None,
) -> str:
    blob: list[Any] = [None] * 12
    blob[4] = [None] * 7 + [rating]
    if coords is not None:
        blob[9] = [None, None, *coords]
    blob[10] = place_id
    blob[11] = name
    return json.dumps([None] * 6 + [blob])


def make_context(cards: list[Card]) -> tuple[AsyncMock, AsyncMock]:
    page = AsyncMock()
    page.url = SEARCH_URL
    page.is_closed = MagicMock(return_value=False)
    page.evaluate.return_value = 1000
    context = AsyncMock()
    context.new_page.return_value = page

    def locator(card: Card) -> AsyncMock:
        item = AsyncMock()
        item.get_attribute.return_value = card.link
        item.evaluate.side_effect = lambda script, _: (
            card.html if "closest" in script else not card.changed_before_click
        )
        return item

    feed = AsyncMock()
    feed.all.return_value = [locator(card) for card in cards]
    feed.evaluate_all.return_value = [card.link for card in cards]
    page.locator = MagicMock(return_value=feed)
    responses = iter(cards)

    def expect_response(*args: Any, **kwargs: Any) -> AsyncMock:
        card = next(responses)
        context_manager = AsyncMock()
        if card.preview is None:
            context_manager.__aenter__.side_effect = PlaywrightTimeoutError(
                "Preview timed out"
            )
        else:
            response = AsyncMock()
            response.text.return_value = card.preview
            future = asyncio.get_running_loop().create_future()
            future.set_result(response)
            context_manager.__aenter__.return_value = MagicMock(value=future)
        return context_manager

    page.expect_response = MagicMock(side_effect=expect_response)
    return context, page


async def scrape(
    cards: list[Card], detail: AsyncMock | None = None, **kwargs: Any
) -> list[dict[str, Any]]:
    context, _ = make_context(cards)
    with (
        patch("map_miner.scraper._handle_captcha_if_present", AsyncMock()),
        patch(
            "map_miner.scraper._find_feed_selector",
            AsyncMock(return_value='[role="feed"]'),
        ),
        patch("map_miner.scraper._is_feed_at_end", AsyncMock(return_value=True)),
        patch("map_miner.scraper._scroll_feed", AsyncMock()),
        patch(
            "map_miner.scraper._process_link", detail or AsyncMock(return_value=None)
        ),
    ):
        return await _scrape_query_spa(
            context,
            "cafe",
            Point(*NEAR),
            16,
            initial_delay_range=(0, 0),
            pre_click_delay_range=(0, 0),
            post_item_delay_range=(0, 0),
            post_scroll_delay_range=(0, 0),
            **kwargs,
        )


def test_parseable_incomplete_preview_merges_usable_dom_fields():
    card = Card(
        CAFE,
        preview(rating=4.8),
        f'<a href="{CAFE}"></a><h3>Recovered Cafe</h3>',
    )
    rows = asyncio.run(scrape([card]))
    assert [(row["name"], row["rating"]) for row in rows] == [("Recovered Cafe", 4.8)]


def test_verified_url_supplies_missing_place_identity_and_coordinates():
    detail = AsyncMock(return_value=None)
    rows = asyncio.run(
        scrape(
            [Card(CAFE, preview(name="Cafe"))],
            detail=detail,
            fields=["place_id", "latitude", "longitude"],
        )
    )
    assert rows == [{"place_id": "0x1:0x2", "latitude": 21.0, "longitude": 105.8}]
    detail.assert_not_awaited()


@pytest.mark.parametrize(
    ("fields", "payload", "expected"),
    [
        (["link"], preview(name="Cafe"), {"link": CAFE}),
        (["rating"], None, {"rating": 4.7}),
    ],
)
def test_field_selection_does_not_prevent_validation_or_dom_rescue(
    fields, payload, expected
):
    card = Card(
        CAFE,
        payload,
        f'<a href="{CAFE}"></a><h3>Cafe</h3><span class="MW4etd">4.7</span>',
    )
    assert asyncio.run(scrape([card], fields=fields)) == [expected]


def test_viewport_and_field_selection_do_not_change_radius_filter():
    cards = [
        Card("/maps/place/Far", preview("Far", coords=FAR)),
        Card("/maps/place/Near/@22.0,106.0,18z", preview("Near", coords=NEAR)),
    ]
    assert asyncio.run(scrape(cards, fields=["name"], max_places=1)) == [
        {"name": "Near"}
    ]


def test_range_verified_by_detail_before_candidate_fills_quota():
    async def detail(**kwargs: Any) -> None:
        assert kwargs["link"] == "https://www.google.com/maps/place/Unknown"
        kwargs["out_of_range"].append(True)

    cards = [
        Card("/maps/place/Unknown", preview("Unknown")),
        Card(CAFE, preview("Near", coords=NEAR)),
    ]
    assert asyncio.run(
        scrape(
            cards, detail=AsyncMock(side_effect=detail), fields=["name"], max_places=1
        )
    ) == [{"name": "Near"}]


def test_url_aliases_merge_metadata_before_filling_quota():
    alias = CAFE.replace("/Cafe/", "/Cafe-alias/")
    next_link = CAFE.replace("0x1:0x2", "0x3:0x4")
    cards = [
        Card(CAFE, preview("Cafe", "0x1:0x2", NEAR)),
        Card(alias, preview("Cafe", "0x1:0x2", NEAR, rating=4.5)),
        Card(next_link, preview("Next", "0x3:0x4", NEAR)),
    ]
    rows = asyncio.run(scrape(cards, max_places=2))
    assert [(row["place_id"], row["name"]) for row in rows] == [
        ("0x1:0x2", "Cafe"),
        ("0x3:0x4", "Next"),
    ]
    assert rows[0]["rating"] == 4.5


@pytest.mark.parametrize("changed_before_click", [False, True])
def test_mismatched_preview_and_recycled_card_use_detail(changed_before_click):
    wrong_link = CAFE.replace("0x1:0x2", "0x1:0x234")
    response_id = "0x1:0x2" if changed_before_click else "0x1:0x234"
    card = Card(
        CAFE,
        preview("Unverified place", response_id, NEAR),
        f'<a href="{wrong_link}"></a><h3>Wrong place</h3>',
        changed_before_click,
    )
    detail = AsyncMock(return_value={"name": "Verified"})
    issues: list[str] = []
    assert asyncio.run(
        scrape([card], detail=detail, fields=["name"], query_issues=issues)
    ) == [{"name": "Verified"}]
    detail.assert_awaited_once()
    assert issues == []


def test_unresolved_candidate_reports_incomplete_query():
    issues: list[str] = []
    assert asyncio.run(scrape([Card(CAFE, preview())], query_issues=issues)) == []
    assert issues == ["One or more place links could not be extracted"]


def test_late_single_place_redirect_is_extracted():
    context, page = make_context([])
    page.content.return_value = "<h1>Late result</h1>"

    async def missing_feed(_: object) -> None:
        page.url = f"https://www.google.com{CAFE}"

    with (
        patch("map_miner.scraper._handle_captcha_if_present", AsyncMock()),
        patch("map_miner.scraper._find_feed_selector", missing_feed),
    ):
        rows = asyncio.run(
            _scrape_query_spa(
                context, "cafe", Point(*NEAR), 16, initial_delay_range=(0, 0)
            )
        )
    assert [(row["name"], row["link"]) for row in rows] == [
        ("Late result", f"https://www.google.com{CAFE}")
    ]
