import json
from unittest.mock import MagicMock

import polars as pl
import pytest
from geopy.point import Point

from map_miner.extractor import (
    DEFAULT_FLATTEN_COLUMNS,
    REQUIRED_COLUMNS,
    calculate_distance,
    extract_coordinates_from_url,
    extract_feed_item_dom,
    extract_place_data,
    format_places_dataframe,
    is_preview_response_for_link,
    is_within_range,
    make_place_url,
    parse_address_string_fallback,
    parse_dom_from_html,
    parse_json_data,
    safe_get,
    strip_accents,
)


def test_safe_get():
    data = {"a": [{"b": 123}, {"c": [10, 20, 30]}]}
    assert safe_get(data, "a", 0, "b") == 123
    assert safe_get(data, "a", 1, "c", 2) == 30
    assert safe_get(data, "a", 99) is None
    assert safe_get(data, "non_existent") is None
    assert safe_get(None, "key") is None


def test_strip_accents():
    assert strip_accents("Hà Nội") == "Ha Noi"
    assert strip_accents("Đà Nẵng") == "Da Nang"
    assert strip_accents("Thành phố Hồ Chí Minh") == "Thanh pho Ho Chi Minh"


def test_parse_address_string_fallback():
    addr = "123 Tran Phu, Van Quan, Ha Dong, Ha Noi 100000, Vietnam"
    res = parse_address_string_fallback(addr)
    assert res["street"] == "123 Tran Phu"
    assert res["sublocality"] == "Van Quan"
    assert res["district"] == "Ha Dong"
    assert res["city"] is not None and "Ha Noi" in res["city"]
    assert res["postal_code"] == "100000"


def test_parse_dom_from_html():
    html = """
    <html>
        <body>
            <h1>Test Coffee Shop</h1>
            <span aria-label="4.5 stars">4.5</span>
            <span aria-label="120 reviews">120 reviews</span>
            <a data-item-id="authority" href="https://example.com">Website</a>
            <div data-item-id="address" aria-label="Address: 123 Nguyen Trai, Ha Noi">123 Nguyen Trai, Ha Noi</div>
            <div data-item-id="phone:0123456789" aria-label="Phone: 0123 456 789">0123 456 789</div>
        </body>
    </html>
    """
    dom = parse_dom_from_html(html)
    assert dom["name"] == "Test Coffee Shop"
    assert dom["rating"] == 4.5
    assert dom["reviews_count"] == 120
    assert dom["website"] == "https://example.com"
    assert dom["address"] == "123 Nguyen Trai, Ha Noi"
    assert dom["phone"] == "0123 456 789"


def test_extract_place_data_blob():
    mock_blob = [None] * 250
    mock_blob[11] = "Highlands Coffee"
    mock_blob[78] = "ChIJ12345"
    mock_blob[9] = [None, None, 21.0, 105.8]
    mock_blob[39] = "123 Tran Phu, Ha Dong, Ha Noi"
    mock_blob[4] = [None, None, None, None, None, None, None, 4.3, 850]
    mock_blob[13] = ["Coffee shop"]
    mock_blob[178] = [["0987654321"]]
    mock_blob[183] = [
        None,
        ["Van Quan", "123 Tran Phu", None, "Ha Noi", "100000", "Ha Dong", "VN"],
    ]
    mock_blob[243] = "VN"

    res = extract_place_data(preview_blob=mock_blob)
    assert res is not None
    assert res["name"] == "Highlands Coffee"
    assert res["place_id"] == "ChIJ12345"
    assert res["latitude"] == 21.0
    assert res["longitude"] == 105.8
    assert res["rating"] == 4.3
    assert res["reviews_count"] == 850
    assert res["city"] == "Ha Noi"
    assert res["district"] == "Ha Dong"
    assert res["street"] == "123 Tran Phu"
    assert res["sublocality"] == "Van Quan"
    assert res["country_code"] == "VN"


def test_extract_place_data_dom_fallback():
    html = """
    <html>
        <body>
            <h1>Test Restaurant</h1>
            <span aria-label="4.8 stars">4.8</span>
            <span aria-label="50 reviews">50 reviews</span>
        </body>
    </html>
    """
    data = extract_place_data(html_content=html)
    assert data is not None
    assert data["name"] == "Test Restaurant"
    assert data["rating"] == 4.8
    assert data["reviews_count"] == 50


def test_extract_place_data_field_filtering():
    html = """
    <html>
        <body>
            <h1>Test Cafe</h1>
            <span aria-label="4.2 stars">4.2</span>
            <span aria-label="30 reviews">30 reviews</span>
        </body>
    </html>
    """
    data = extract_place_data(html_content=html, fields=["name", "rating"])
    assert data == {"name": "Test Cafe", "rating": 4.2}
    assert "reviews_count" not in data


def test_parse_json_data_xssi_variations():
    # Test path [3][6] with \r\n and whitespace after )]}'
    blob = [None] * 20
    blob[11] = "Cafe With Windows CRLF"
    inner_data = [None, None, None, None, None, None, blob]
    crlf_str = ")]}'\r\n" + json.dumps(inner_data)
    state = [None, None, None, [None, None, None, None, None, None, crlf_str]]

    res = parse_json_data(json.dumps(state))
    assert res is not None
    assert res[11] == "Cafe With Windows CRLF"

    # Test path [3][5] alternative with space after )]}'
    alt_blob = [None] * 20
    alt_blob[11] = "Cafe Alt Path"
    alt_inner = [alt_blob]
    space_str = ")]}' " + json.dumps(alt_inner)
    alt_state = [None, None, None, [None, None, None, None, None, space_str]]

    res_alt = parse_json_data(json.dumps(alt_state))
    assert res_alt is not None
    assert res_alt[11] == "Cafe Alt Path"


def test_calculate_distance_and_is_within_range():
    p1 = (21.028511, 105.854444)
    p2 = (21.033333, 105.845555)
    point1 = Point(21.028511, 105.854444)
    point2 = Point(21.033333, 105.845555)

    # Calculate distance returns positive float ~ 1060-1100m
    dist_tuple = calculate_distance(p1, p2)
    dist_point = calculate_distance(point1, point2)
    assert dist_tuple == pytest.approx(dist_point, rel=1e-5)
    assert 1000.0 < dist_tuple < 1200.0

    # Symmetric distance
    assert calculate_distance(p1, p2) == pytest.approx(calculate_distance(p2, p1))

    # Same point distance is approx 0
    assert calculate_distance(p1, p1) == pytest.approx(0.0, abs=1e-3)
    assert calculate_distance(point1, point1) == pytest.approx(0.0, abs=1e-3)

    # is_within_range
    assert is_within_range(p1, p2, range_limit=2000.0) is True
    assert is_within_range(p1, p2, range_limit=500.0) is False
    assert is_within_range(p1, p2, range_limit=dist_tuple) is True
    assert is_within_range(point1, point2, range_limit=dist_tuple - 1.0) is False


def test_make_place_url():
    point = Point(21.0285, 105.8544)
    # Default lang="en"
    url = make_place_url("Cafe Hanoi", point, 15.0)
    assert (
        url
        == "https://www.google.com/maps/search/Cafe+Hanoi/@21.0285,105.8544,15.0z?hl=en"
    )

    # Custom lang="vi"
    url_vi = make_place_url("Cà Phê Trứng", point, 16.0, lang="vi")
    assert (
        url_vi
        == "https://www.google.com/maps/search/C%C3%A0+Ph%C3%AA+Tr%E1%BB%A9ng/@21.0285,105.8544,16.0z?hl=vi"
    )


def test_extract_coordinates_from_url():
    # Format /@lat,lng,zoom
    url_at = "https://www.google.com/maps/place/Cafe+A/@21.028511,105.854444,17z/data=!4m5!3m4!1s0x0:0x0"
    coords = extract_coordinates_from_url(url_at)
    assert coords is not None
    assert coords[0] == pytest.approx(21.028511)
    assert coords[1] == pytest.approx(105.854444)

    # Format !3dlat!4dlng
    url_data = "https://www.google.com/maps/place/Cafe+B/data=!4m2!3m1!1s0x0:0x0!3d21.028511!4d105.854444"
    coords_data = extract_coordinates_from_url(url_data)
    assert coords_data is not None
    assert coords_data[0] == pytest.approx(21.028511)
    assert coords_data[1] == pytest.approx(105.854444)

    # Missing coordinates
    assert (
        extract_coordinates_from_url("https://www.google.com/maps/search/cafe") is None
    )

    # Invalid coordinates out of range
    assert (
        extract_coordinates_from_url("https://www.google.com/maps?q=195.0,200.0")
        is None
    )


def test_is_preview_response_for_link():
    canonical = "https://www.google.com/maps/place/data=!4m2!3m1!1s0x3135ab953357c99f:0x50774a3f338d3ab9"
    matching_url = "https://www.google.com/maps/preview/place?authuser=0&hl=vi&gl=vn&pb=!1m18!1m12!1m3!1d1000!2d105.8!3d21.0!2m3!1f0!2f0!3f0!3m2!1i1024!2i768!4f13.1!3m3!1m2!1s0x3135ab953357c99f:0x50774a3f338d3ab9!2z"
    mismatched_url = "https://www.google.com/maps/preview/place?authuser=0&hl=vi&gl=vn&pb=!1m18!1m12!1m3!1d1000!2d105.8!3d21.0!2m3!1f0!2f0!3f0!3m2!1i1024!2i768!4f13.1!3m3!1m2!1s0x3135ab953357caaa:0x50774a3f338d3bbb!2z"

    # Match
    resp_ok = MagicMock()
    resp_ok.url = matching_url
    resp_ok.status = 200
    resp_ok.ok = True
    assert is_preview_response_for_link(resp_ok, canonical) is True

    # Status not ok
    resp_err = MagicMock()
    resp_err.url = matching_url
    resp_err.status = 500
    resp_err.ok = False
    assert is_preview_response_for_link(resp_err, canonical) is False

    # Mismatched hex ID
    resp_mismatch = MagicMock()
    resp_mismatch.url = mismatched_url
    resp_mismatch.status = 200
    resp_mismatch.ok = True
    assert is_preview_response_for_link(resp_mismatch, canonical) is False

    # Not preview url
    resp_not_preview = MagicMock()
    resp_not_preview.url = "https://www.google.com/maps/search/cafe"
    resp_not_preview.status = 200
    resp_not_preview.ok = True
    assert is_preview_response_for_link(resp_not_preview, canonical) is False


def test_format_places_dataframe_in_extractor():
    # Empty results with flatten=False
    df_empty = format_places_dataframe([], flatten=False)
    assert isinstance(df_empty, pl.DataFrame)
    assert len(df_empty) == 0
    assert set(df_empty.columns) == set(DEFAULT_FLATTEN_COLUMNS) | {"details"}
    assert REQUIRED_COLUMNS == DEFAULT_FLATTEN_COLUMNS

    # Empty results with flatten=True
    df_empty_flat = format_places_dataframe([], flatten=True)
    assert isinstance(df_empty_flat, pl.DataFrame)
    assert len(df_empty_flat) == 0

    # Populated results with flatten=False and fields subset
    data = [
        {
            "name": "Test Place",
            "rating": 4.5,
            "custom_field": "extra_val",
        }
    ]
    df = format_places_dataframe(data, flatten=False, fields=["name", "custom_field"])
    assert "name" in df.columns
    assert "details" in df.columns
    assert df["name"][0] == "Test Place"
    assert '"custom_field": "extra_val"' in df["details"][0]


def test_is_preview_response_for_link_place_id():
    """Verifies is_preview_response_for_link matching with Google Place ID (ChIJ...)."""
    chij_link = (
        "https://www.google.com/maps/place/?q=place_id:ChIJdWb_e3MvdTERRLy4KKg2lnw"
    )
    preview_url_chij = "https://www.google.com/maps/preview/place?authuser=0&pb=!1m18!1sChIJdWb_e3MvdTERRLy4KKg2lnw"
    preview_url_mismatch = "https://www.google.com/maps/preview/place?authuser=0&pb=!1m18!1sChIJotherplaceid123456"

    # Match
    resp_match = MagicMock(url=preview_url_chij, status=200, ok=True)
    assert is_preview_response_for_link(resp_match, chij_link) is True

    # Mismatch
    resp_mismatch = MagicMock(url=preview_url_mismatch, status=200, ok=True)
    assert is_preview_response_for_link(resp_mismatch, chij_link) is False

    # Link with neither ID (falls back to URL structure check)
    simple_link = "https://www.google.com/maps/place/SomeCafe"
    assert is_preview_response_for_link(resp_match, simple_link) is True


def test_extract_feed_item_dom():
    """Verifies pure extraction from rendered feed card HTML DOM."""
    card_html = """
    <div class="Nv2PK THOPZb">
        <a class="hfpxzc" href="https://www.google.com/maps/place/Cafe+Highlands/data=!4m7!3m6!1s0x31752f3b9c025075:0x7c96362828b8cf44!8m2!3d10.771971!4d106.698345" aria-label="Cafe Highlands"></a>
        <div class="qBF1Pd fontHeadlineSmall">Cafe Highlands</div>
        <div class="W4Efsd">
            <span class="MW4etd">4.6</span>
            <span class="UY7F9">(1,520)</span>
            <span> · </span>
            <span>Quán cà phê</span>
        </div>
        <div class="W4Efsd">
            <span>123 Đường Lê Lợi, Phường Bến Nghé, Quận 1, TP. Hồ Chí Minh</span>
        </div>
    </div>
    """
    data = extract_feed_item_dom(card_html)
    assert data["name"] == "Cafe Highlands"
    assert data["rating"] == 4.6
    assert data["reviews_count"] == 1520
    assert "Quán cà phê" in data.get("categories", [])
    assert "123 Đường Lê Lợi" in data.get("address", "")
    assert data.get("city") is not None and "Hồ Chí Minh" in data["city"]
    assert data.get("latitude") == 10.771971
    assert data.get("longitude") == 106.698345
    assert data.get("place_id") == "0x31752f3b9c025075:0x7c96362828b8cf44"

    # Filter with fields
    filtered = extract_feed_item_dom(card_html, fields=["name", "rating"])
    assert filtered == {"name": "Cafe Highlands", "rating": 4.6}

    # Empty card html
    assert extract_feed_item_dom("") == {}


def test_format_places_dataframe_reviews_count_int64():
    """Verifies that reviews_count consistently retains pl.Int64 schema across all branches."""
    # 1. flatten=True empty with fields
    df_empty_fields = format_places_dataframe(
        [], flatten=True, fields=["name", "reviews_count"]
    )
    assert df_empty_fields["reviews_count"].dtype == pl.Int64

    # 2. flatten=True populated with mixed types (int, float, str numeric, None, nan)
    data = [
        {"name": "Place 1", "reviews_count": 10},
        {"name": "Place 2", "reviews_count": 25.0},
        {"name": "Place 3", "reviews_count": "150"},
        {"name": "Place 4", "reviews_count": None},
        {"name": "Place 5", "reviews_count": float("nan")},
        {"name": "Place 6", "reviews_count": "invalid_str"},
    ]
    df_flat = format_places_dataframe(data, flatten=True)
    assert df_flat["reviews_count"].dtype == pl.Int64
    assert df_flat["reviews_count"].to_list() == [10, 25, 150, None, None, None]

    # 3. flatten=False empty (default schema)
    df_empty_default = format_places_dataframe([], flatten=False)
    assert df_empty_default["reviews_count"].dtype == pl.Int64

    # 4. flatten=False populated
    df_pop = format_places_dataframe(data, flatten=False)
    assert df_pop["reviews_count"].dtype == pl.Int64
    assert df_pop["reviews_count"].to_list() == [10, 25, 150, None, None, None]

    # 5. 100% null values in reviews_count
    all_nulls = [{"name": "A", "reviews_count": None}, {"name": "B"}]
    df_nulls = format_places_dataframe(all_nulls, flatten=True)
    assert df_nulls["reviews_count"].dtype == pl.Int64
    assert df_nulls["reviews_count"].to_list() == [None, None]
