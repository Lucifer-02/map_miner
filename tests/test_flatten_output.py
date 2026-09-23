import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import polars as pl
import pytest
from geopy.point import Point

from map_miner.extractor import (
    DEFAULT_FLATTEN_COLUMNS,
    REQUIRED_COLUMNS,
    format_places_dataframe,
)
from map_miner.scraper import (
    scrape_google_maps,
)


@pytest.fixture
def sample_places_data() -> list[dict]:
    """Sample places data including 11 default common columns and various detail fields."""
    return [
        {
            "name": "Cà Phê Trứng Hà Nội",
            "place_id": "ChIJ_sample_01",
            "latitude": 21.028511,
            "longitude": 105.854444,
            "address": "77 Hàng Gai, Hoàn Kiếm, Hà Nội",
            "link": "https://www.google.com/maps/place/?q=place_id:ChIJ_sample_01",
            "categories": ["Quán cà phê", "Điểm du lịch"],
            "rating": 4.8,
            "reviews_count": 1250,
            "plus_code": "XQMM+PF Hoan Kiem, Hanoi, Vietnam",
            "city": "Hà Nội",
            "phone": "+84 24 3828 5004",
            "website": "https://caphetrunghanoi.vn",
            "opening_hours": {"Monday": "07:00 - 22:00", "Tuesday": "07:00 - 22:00"},
            "open_status": "Đang mở cửa",
            "price_level": "₫30,000–60,000",
            "photos_count": 85,
            "is_claimed": True,
            "null_field": None,  # Should be omitted from details
        },
        {
            "name": "Phở Gia Truyền Bát Đàn",
            "place_id": "ChIJ_sample_02",
            "latitude": 21.033333,
            "longitude": 105.845555,
            "address": "49 Bát Đàn, Cửa Đông, Hoàn Kiếm, Hà Nội",
            "link": "https://www.google.com/maps/place/?q=place_id:ChIJ_sample_02",
            "categories": ["Nhà hàng phở"],
            "rating": 4.5,
            "reviews_count": 3200,
            "plus_code": "XQMM+88 Hoan Kiem, Hanoi, Vietnam",
            "city": "Hà Nội",
            "phone": None,  # Null value
            "website": None,
            "opening_hours": {"Monday": "06:00 - 10:00, 18:00 - 20:30"},
        },
    ]


def test_format_places_dataframe_default_flatten_false(sample_places_data):
    """Verifies that default flatten=False outputs 11 default columns + 'details' JSON string.

    Also tests that details is valid JSON, retains Vietnamese unicode, and omits None values.
    """
    df = format_places_dataframe(sample_places_data, flatten=False)

    assert isinstance(df, pl.DataFrame)
    assert len(df) == 2

    # Expected top-level columns: exactly 11 default columns + 'details'
    expected_cols = list(DEFAULT_FLATTEN_COLUMNS) + ["details"]
    assert df.columns == expected_cols

    # Check top-level values
    assert df["name"][0] == "Cà Phê Trứng Hà Nội"
    assert df["place_id"][0] == "ChIJ_sample_01"
    assert df["latitude"][0] == pytest.approx(21.028511)
    assert df["longitude"][0] == pytest.approx(105.854444)
    assert df["address"][0] == "77 Hàng Gai, Hoàn Kiếm, Hà Nội"
    assert (
        df["link"][0] == "https://www.google.com/maps/place/?q=place_id:ChIJ_sample_01"
    )
    assert df["categories"][0].to_list() == ["Quán cà phê", "Điểm du lịch"]
    assert df["rating"][0] == pytest.approx(4.8)
    assert df["reviews_count"][0] == 1250
    assert df["plus_code"][0] == "XQMM+PF Hoan Kiem, Hanoi, Vietnam"
    assert df["city"][0] == "Hà Nội"

    # Check details JSON column for row 0
    raw_details_0 = df["details"][0]
    assert isinstance(raw_details_0, str)
    # Verify unicode preservation (ensure_ascii=False)
    assert "\\u" not in raw_details_0
    assert "https://caphetrunghanoi.vn" in raw_details_0

    parsed_0 = json.loads(raw_details_0)
    assert parsed_0["phone"] == "+84 24 3828 5004"
    assert parsed_0["website"] == "https://caphetrunghanoi.vn"
    assert parsed_0["opening_hours"] == {
        "Monday": "07:00 - 22:00",
        "Tuesday": "07:00 - 22:00",
    }
    assert parsed_0["open_status"] == "Đang mở cửa"
    assert parsed_0["price_level"] == "₫30,000–60,000"
    assert parsed_0["photos_count"] == 85
    assert parsed_0["is_claimed"] is True
    # Verify None fields are excluded
    assert "null_field" not in parsed_0
    # Verify default flatten columns are excluded from details
    for def_col in DEFAULT_FLATTEN_COLUMNS:
        assert def_col not in parsed_0

    # Check details for row 1: phone and website were None, should not be present
    raw_details_1 = df["details"][1]
    parsed_1 = json.loads(raw_details_1)
    assert "phone" not in parsed_1
    assert "website" not in parsed_1
    assert "rating" not in parsed_1
    assert parsed_1["opening_hours"] == {"Monday": "06:00 - 10:00, 18:00 - 20:30"}


def test_format_places_dataframe_flatten_true(sample_places_data):
    """Verifies that flatten=True flattens all fields into individual columns without details column."""
    df = format_places_dataframe(sample_places_data, flatten=True)

    assert isinstance(df, pl.DataFrame)
    assert len(df) == 2
    assert "details" not in df.columns

    # All fields should be top-level columns
    for def_col in DEFAULT_FLATTEN_COLUMNS:
        assert def_col in df.columns

    assert "phone" in df.columns
    assert "website" in df.columns
    assert "opening_hours" in df.columns
    assert "price_level" in df.columns

    assert df["name"][0] == "Cà Phê Trứng Hà Nội"
    assert df["rating"][0] == 4.8
    assert df["city"][0] == "Hà Nội"


def test_format_places_dataframe_empty_flatten_false():
    """Verifies that an empty results list with flatten=False returns a DataFrame with 12 standard columns."""
    df = format_places_dataframe([], flatten=False)

    assert isinstance(df, pl.DataFrame)
    assert len(df) == 0
    expected_cols = list(DEFAULT_FLATTEN_COLUMNS) + ["details"]
    assert df.columns == expected_cols
    assert df.schema["name"] == pl.String
    assert df.schema["place_id"] == pl.String
    assert df.schema["latitude"] == pl.Float64
    assert df.schema["longitude"] == pl.Float64
    assert df.schema["address"] == pl.String
    assert df.schema["link"] == pl.String
    assert df.schema["categories"] == pl.List(pl.String)
    assert df.schema["rating"] == pl.Float64
    assert df.schema["reviews_count"] == pl.Int64
    assert df.schema["plus_code"] == pl.String
    assert df.schema["city"] == pl.String
    assert df.schema["details"] == pl.String


def test_format_places_dataframe_empty_flatten_true():
    """Verifies that an empty results list with flatten=True returns an empty DataFrame."""
    df = format_places_dataframe([], flatten=True)

    assert isinstance(df, pl.DataFrame)
    assert len(df) == 0
    assert df.columns == []


def test_format_places_dataframe_with_mixed_fields(sample_places_data):
    """Verifies that passing mixed fields with flatten=False places default columns at top-level

    and bundles other requested fields into details.
    """
    fields = ["name", "latitude", "city", "phone"]
    df = format_places_dataframe(sample_places_data, flatten=False, fields=fields)

    assert isinstance(df, pl.DataFrame)
    assert len(df) == 2

    # Top-level should have only name, latitude, city, and details
    assert df.columns == ["name", "latitude", "city", "details"]
    assert df.schema["latitude"] == pl.Float64
    assert df.schema["city"] == pl.String
    assert df["name"][0] == "Cà Phê Trứng Hà Nội"
    assert df["latitude"][0] == pytest.approx(21.028511)
    assert df["city"][0] == "Hà Nội"

    # details contains phone only
    details_0 = json.loads(df["details"][0])
    assert details_0 == {"phone": "+84 24 3828 5004"}

    # row 1 phone was None, so details is empty dict
    details_1 = json.loads(df["details"][1])
    assert details_1 == {}


def test_format_places_dataframe_with_only_required_fields(sample_places_data):
    """Verifies that passing only default columns with flatten=False results in NO details column."""
    fields = ["name", "address", "city"]
    df = format_places_dataframe(sample_places_data, flatten=False, fields=fields)

    assert isinstance(df, pl.DataFrame)
    assert len(df) == 2
    assert df.columns == ["name", "address", "city"]
    assert "details" not in df.columns
    assert df["name"][0] == "Cà Phê Trứng Hà Nội"
    assert df["address"][0] == "77 Hàng Gai, Hoàn Kiếm, Hà Nội"
    assert df["city"][0] == "Hà Nội"

    # Empty results test with only default fields
    df_empty = format_places_dataframe([], flatten=False, fields=fields)
    assert len(df_empty) == 0
    assert df_empty.columns == ["name", "address", "city"]


def test_format_places_dataframe_with_only_detail_fields(sample_places_data):
    """Verifies that passing only detail fields with flatten=False results in ONLY details column."""
    fields = ["phone", "website"]
    df = format_places_dataframe(sample_places_data, flatten=False, fields=fields)

    assert isinstance(df, pl.DataFrame)
    assert len(df) == 2
    assert df.columns == ["details"]

    details_0 = json.loads(df["details"][0])
    assert details_0 == {
        "phone": "+84 24 3828 5004",
        "website": "https://caphetrunghanoi.vn",
    }

    # Empty results test with only detail fields
    df_empty = format_places_dataframe([], flatten=False, fields=fields)
    assert len(df_empty) == 0
    assert df_empty.columns == ["details"]


def test_format_places_dataframe_with_fields_flatten_true(sample_places_data):
    """Verifies that passing fields with flatten=True produces flattened columns matching fields."""
    fields = ["name", "rating"]
    df = format_places_dataframe(sample_places_data, flatten=True, fields=fields)

    assert isinstance(df, pl.DataFrame)
    assert len(df) == 2
    assert df.columns == ["name", "rating"]
    assert "details" not in df.columns
    assert df["name"][0] == "Cà Phê Trứng Hà Nội"
    assert df["rating"][0] == 4.8

    # Empty results with fields and flatten=True
    df_empty = format_places_dataframe([], flatten=True, fields=fields)
    assert len(df_empty) == 0
    assert df_empty.columns == ["name", "rating"]


def test_scrape_google_maps_forwards_flatten_parameter():
    """Verifies that scrape_google_maps forwards the flatten parameter to format_places_dataframe."""

    async def _run():
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

        mock_results = [
            {
                "name": "Highlands Coffee",
                "place_id": "ChIJ_highlands",
                "latitude": 21.02,
                "longitude": 105.83,
                "address": "1 Tràng Tiền",
                "link": "https://maps.google.com/place_id:ChIJ_highlands",
                "rating": 4.2,
                "phone": "024 1234 5678",
            }
        ]
        mock_spa = AsyncMock(return_value=mock_results)

        # 1. Default flatten=False: 11 default columns + 'details'
        with (
            patch(
                "map_miner.scraper.async_playwright",
                return_value=MockPlaywrightContext(),
            ),
            patch("map_miner.scraper._scrape_query_spa", mock_spa),
        ):
            df_default = await scrape_google_maps(
                queries={"coffee"},
                geo_coordinates=Point(21.0, 105.8),
                zoom=16,
                use_spa=True,
            )

        assert len(df_default) == 1
        assert "details" in df_default.columns
        for def_col in DEFAULT_FLATTEN_COLUMNS:
            assert def_col in df_default.columns
        # rating is one of DEFAULT_FLATTEN_COLUMNS
        assert df_default["rating"][0] == pytest.approx(4.2)
        details_json = json.loads(df_default["details"][0])
        assert details_json["phone"] == "024 1234 5678"
        assert "rating" not in details_json

        # 2. Explicit flatten=True: flattened columns without 'details'
        with (
            patch(
                "map_miner.scraper.async_playwright",
                return_value=MockPlaywrightContext(),
            ),
            patch("map_miner.scraper._scrape_query_spa", mock_spa),
        ):
            df_flattened = await scrape_google_maps(
                queries={"coffee"},
                geo_coordinates=Point(21.0, 105.8),
                zoom=16,
                use_spa=True,
                flatten=True,
            )

        assert len(df_flattened) == 1
        assert "details" not in df_flattened.columns
        assert "rating" in df_flattened.columns
        assert "phone" in df_flattened.columns
        assert df_flattened["rating"][0] == 4.2

    asyncio.run(_run())


def test_required_columns_backward_compatibility():
    """Verifies that REQUIRED_COLUMNS remains accessible as a backward compatibility alias."""
    assert REQUIRED_COLUMNS == DEFAULT_FLATTEN_COLUMNS
