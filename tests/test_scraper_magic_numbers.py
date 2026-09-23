"""Tests verifying that all magic numbers in scraper.py have been replaced
with default parameter values in function signatures, and can be overridden cleanly.
"""

import ast
import asyncio
import inspect
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from geopy.point import Point

from map_miner.scraper import (
    _create_browser_context,
    _find_feed_selector,
    _get_place_urls,
    _pass_consent,
    _process_link,
    _scrape_query_spa,
    _scroll_feed,
    scrape_google_maps,
)


def test_default_parameters_exist_and_match() -> None:
    """Verifies that all operational control parameters exist in function signatures
    with their required default values."""
    # 1. _create_browser_context
    sig_cbc = inspect.signature(_create_browser_context)
    assert sig_cbc.parameters["viewport_width"].default == 1920
    assert sig_cbc.parameters["viewport_height"].default == 1080
    assert sig_cbc.parameters["viewport_variance"].default == 50

    # 2. _pass_consent
    sig_pc = inspect.signature(_pass_consent)
    assert sig_pc.parameters["dismiss_delay"].default == 1.0

    # 3. _find_feed_selector
    sig_ffs = inspect.signature(_find_feed_selector)
    assert sig_ffs.parameters["timeout"].default == 15000

    # 4. _scroll_feed
    sig_sf = inspect.signature(_scroll_feed)
    assert sig_sf.parameters["scroll_delta_y"].default == 5000
    assert sig_sf.parameters["scroll_delay_range"].default == (1.0, 1.6)

    # 5. _get_place_urls
    sig_gpu = inspect.signature(_get_place_urls)
    assert sig_gpu.parameters["initial_delay_range"].default == (1.0, 2.5)
    assert sig_gpu.parameters["min_remaining_time"].default == 2.0
    assert sig_gpu.parameters["scroll_retry_delay"].default == 1.5

    # 6. _scrape_query_spa
    sig_sqs = inspect.signature(_scrape_query_spa)
    assert sig_sqs.parameters["initial_delay_range"].default == (1.0, 2.0)
    assert sig_sqs.parameters["min_remaining_time"].default == 2.0
    assert sig_sqs.parameters["element_timeout"].default == 1000
    assert sig_sqs.parameters["pre_click_delay_range"].default == (0.3, 0.8)
    assert sig_sqs.parameters["min_preview_timeout_ms"].default == 1000
    assert sig_sqs.parameters["timeout_safety_margin"].default == 0.5
    assert sig_sqs.parameters["post_item_delay_range"].default == (0.15, 0.35)
    assert sig_sqs.parameters["post_scroll_delay_range"].default == (1.2, 2.2)
    assert sig_sqs.parameters["scroll_retry_delay"].default == 1.5
    assert sig_sqs.parameters["secondary_rescue_min_time"].default == 5.0
    assert sig_sqs.parameters["secondary_rescue_concurrency"].default == 4
    assert sig_sqs.parameters["secondary_rescue_cutoff"].default == 3.0
    assert sig_sqs.parameters["min_valid_fields"].default == 3

    # 7. _process_link
    sig_pl = inspect.signature(_process_link)
    assert sig_pl.parameters["preview_wait_timeout"].default == 4.5
    assert sig_pl.parameters["main_selector_timeout"].default == 2000
    assert sig_pl.parameters["retry_delay_range"].default == (0.8, 1.5)
    assert sig_pl.parameters["captcha_retry_delay_range"].default == (1.0, 2.0)
    assert sig_pl.parameters["min_valid_fields"].default == 3

    # 8. scrape_google_maps
    sig_sgm = inspect.signature(scrape_google_maps)
    assert sig_sgm.parameters["watchdog_grace_period"].default == 10.0


def test_override_create_browser_context_params() -> None:
    """Tests overriding viewport parameters in _create_browser_context."""

    async def _run() -> None:
        mock_browser = AsyncMock()
        mock_context = AsyncMock()
        mock_browser.new_context.return_value = mock_context

        # With zero variance, the viewport dimensions must match exactly
        await _create_browser_context(
            browser=mock_browser,
            geo_coordinates=Point(10.0, 106.0),
            viewport_width=1280,
            viewport_height=720,
            viewport_variance=0,
        )

        call_kwargs = mock_browser.new_context.call_args[1]
        assert call_kwargs["viewport"] == {"width": 1280, "height": 720}

    asyncio.run(_run())


def test_override_pass_consent_delay() -> None:
    """Tests overriding dismiss_delay in _pass_consent."""

    async def _run() -> None:
        mock_page = AsyncMock()
        mock_button = MagicMock()
        mock_button.count = AsyncMock(return_value=1)
        mock_first = AsyncMock()
        mock_first.is_visible = AsyncMock(return_value=True)
        mock_first.click = AsyncMock()
        mock_button.first = mock_first
        mock_page.get_by_role = MagicMock(return_value=mock_button)

        with patch(
            "map_miner.scraper.asyncio.sleep", new_callable=AsyncMock
        ) as mock_sleep:
            res = await _pass_consent(mock_page, dismiss_delay=0.25)
            assert res is True
            mock_sleep.assert_awaited_once_with(0.25)

    asyncio.run(_run())


def test_override_scroll_feed_params() -> None:
    """Tests overriding scroll_delta_y and scroll_delay_range in _scroll_feed."""

    async def _run() -> None:
        mock_page = AsyncMock()
        mock_locator = MagicMock()
        mock_first = AsyncMock()
        mock_first.hover = AsyncMock()
        mock_locator.first = mock_first
        mock_page.locator = MagicMock(return_value=mock_locator)
        mock_page.evaluate = AsyncMock()

        with patch(
            "map_miner.scraper.asyncio.sleep", new_callable=AsyncMock
        ) as mock_sleep:
            await _scroll_feed(
                page=mock_page,
                feed_selector='[role="feed"]',
                scroll_delta_y=2500,
                scroll_delay_range=(0.1, 0.1),
            )
            mock_page.mouse.wheel.assert_awaited_once_with(0, 2500)
            mock_sleep.assert_awaited_once_with(0.1)

    asyncio.run(_run())


def test_no_magic_numbers_in_ast() -> None:
    """AST validation test ensuring no banned magic numbers appear inline
    in the bodies of the modified scraper functions.
    """
    scraper_path = Path("src/map_miner/scraper.py")
    assert scraper_path.exists(), "src/map_miner/scraper.py must exist"

    with scraper_path.open() as f:
        tree = ast.parse(f.read())

    # Map of banned magic numbers per function
    banned_per_function: dict[str, set[int | float]] = {
        "_create_browser_context": {1920, 1080, 50},
        "_pass_consent": {1.0},
        "_scroll_feed": {5000, 1.6},
        "_get_place_urls": {2.5, 1.5},
        "_scrape_query_spa": {
            0.3,
            0.8,
            0.5,
            0.15,
            0.35,
            1.2,
            2.2,
            1.5,
            5.0,
            3.0,
        },
        "_process_link": {4.5, 2000, 0.8, 1.5},
        "run_spa_query": {10.0},
        "run_get_urls": {10.0},
    }

    class BodyNumberCollector(ast.NodeVisitor):
        def __init__(self) -> None:
            self.current_func: str | None = None
            self.violations: list[str] = []

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            old = self.current_func
            self.current_func = node.name
            for item in node.body:
                self.visit(item)
            self.current_func = old

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            old = self.current_func
            self.current_func = node.name
            for item in node.body:
                self.visit(item)
            self.current_func = old

        def visit_Constant(self, node: ast.Constant) -> None:
            if (
                isinstance(node.value, (int, float))
                and not isinstance(node.value, bool)
                and self.current_func in banned_per_function
            ):
                banned_set = banned_per_function[self.current_func]
                if node.value in banned_set:
                    self.violations.append(
                        f"Function '{self.current_func}' line {node.lineno} "
                        f"has forbidden magic number: {node.value}"
                    )

    collector = BodyNumberCollector()
    collector.visit(tree)

    assert not collector.violations, "Found magic numbers in scraper.py:\n" + "\n".join(
        collector.violations
    )
