import inspect
import json
from typing import Any
from unittest.mock import MagicMock

import polars as pl
import pytest
from geopy.point import Point

import map_miner
from map_miner.extractor import (
    DEFAULT_FLATTEN_COLUMNS,
    DEFAULT_RANGE_LIMIT,
    REQUIRED_COLUMNS,
    _is_matching_feed_link,
    calculate_distance,
    compare_place_url_ids,
    extract_coordinates_from_url,
    extract_feed_item_dom,
    extract_place_coordinates_from_url,
    extract_place_data,
    extract_place_identity_from_url,
    format_places_dataframe,
    get_reviews_count,
    is_preview_response_for_link,
    is_within_range,
    make_place_url,
    parse_address_string_fallback,
    parse_dom_from_html,
    parse_json_data,
    preview_blob_matches_link,
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
    mock_blob: list[Any] = [None] * 250
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
    assert data is not None
    assert data == {"name": "Test Cafe", "rating": 4.2}
    assert "reviews_count" not in data


@pytest.mark.parametrize(
    "fields", [None, ["name", "place_id", "latitude", "longitude", "website"]]
)
def test_html_fills_missing_fields_from_an_otherwise_usable_preview(fields):
    preview: list[Any] = [None] * 12
    preview[9] = [None, None, 21.0, 105.8]
    preview[10] = "0x1:0x2"
    preview[11] = "Preview Cafe"
    html_blob = preview.copy()
    html_blob[7] = ["https://example.com"]
    html_blob[11] = "HTML Cafe"
    state = [None] * 3 + [[None] * 6 + [html_blob]]
    html = f"<script>;window.APP_INITIALIZATION_STATE={json.dumps(state)};window.APP_FLAGS=[];</script>"

    extracted = extract_place_data(
        html_content=html, preview_blob=preview, fields=fields
    )

    assert extracted == {
        "name": "Preview Cafe",
        "place_id": "0x1:0x2",
        "latitude": 21.0,
        "longitude": 105.8,
        "website": "https://example.com",
    }


def test_malformed_payload_fields_are_discarded_without_losing_other_fields():
    preview: list[Any] = [None] * 250
    preview[9] = [None, None, 200, "105.8"]
    preview[10] = "0x1:0x2"
    preview[11] = ["not a place name"]
    preview[13] = ["Cafe", {}, ""]
    preview[72] = [{"wrong": "photo collection"}]
    preview[78] = ["not a place id"]
    preview[208] = [[None, None, "21.0"]]

    assert extract_place_data(preview_blob=preview) == {
        "place_id": "0x1:0x2",
        "latitude": 21.0,
        "longitude": 105.8,
        "categories": ["Cafe"],
    }


def test_parse_json_data_xssi_variations():
    # Test path [3][6] with \r\n and whitespace after )]}'
    blob: list[Any] = [None] * 20
    blob[11] = "Cafe With Windows CRLF"
    inner_data = [None, None, None, None, None, None, blob]
    crlf_str = ")]}'\r\n" + json.dumps(inner_data)
    state = [None, None, None, [None, None, None, None, None, None, crlf_str]]

    res = parse_json_data(json.dumps(state))
    assert res is not None
    assert res[11] == "Cafe With Windows CRLF"

    # Test path [3][5] alternative with space after )]}'
    alt_blob: list[Any] = [None] * 20
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


def test_place_coordinates_ignore_viewport_and_unpaired_protobuf_values():
    base = "https://www.google.com/maps/place/Cafe/@0,0,17z"
    separated = base + "/data=!3d21.0!1m2!4d105.8"
    assert extract_place_coordinates_from_url(base) is None
    assert extract_place_coordinates_from_url(separated) is None
    assert extract_coordinates_from_url(separated) == (21.0, 105.8)
    assert extract_place_coordinates_from_url(base + "/data=%213d21.0%214d105.8") == (
        21.0,
        105.8,
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

    # Link with neither ID (safely rejected to avoid cross-place data poisoning)
    simple_link = "https://www.google.com/maps/place/SomeCafe"
    assert is_preview_response_for_link(resp_match, simple_link) is False


def test_preview_ids_match_full_tokens_and_preserve_place_id_case():
    preview_url = "https://www.google.com/maps/preview/place?pb=!1s"
    target = "https://www.google.com/maps/place/Branch/data=!1s0x1:0x23"
    assert (
        is_preview_response_for_link(
            MagicMock(url=preview_url + "0x1:0x234", status=200), target
        )
        is False
    )
    assert (
        is_preview_response_for_link(
            MagicMock(url=preview_url + "0X1%3A0X23!2z", status=200), target
        )
        is True
    )

    place_id = "ChIJdWb_e3MvdTERRLy4KKg2lnw-"
    target = "https://www.google.com/maps/place/?query_place_id=" + place_id
    assert extract_place_identity_from_url(target) == place_id
    assert extract_place_identity_from_url("/maps/place/ChIJUsedAsName") is None
    assert (
        is_preview_response_for_link(
            MagicMock(url=preview_url + place_id, status=200), target
        )
        is True
    )
    assert (
        is_preview_response_for_link(
            MagicMock(url=preview_url + place_id.swapcase(), status=200), target
        )
        is False
    )
    assert compare_place_url_ids(target, preview_url + "0x1:0x23") is None
    assert compare_place_url_ids(target, preview_url + place_id + "0") is False

    blob: list[Any] = [None] * 79
    blob[78] = place_id + "0"
    assert preview_blob_matches_link(blob, target) is False
    blob[78] = place_id
    assert preview_blob_matches_link(blob, target) is True


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


def test_extract_feed_item_dom_dom_recycling_guard():
    """Verifies that DOM recycling cross-place poisoning is prevented."""
    card_highlands = """
    <div class="Nv2PK THOPZb">
        <a class="hfpxzc" href="https://www.google.com/maps/place/Highlands+Coffee/data=!4m7!3m6!1s0x31752f3b9c025075:0x7c96362828b8cf44!8m2!3d10.771971!4d106.698345"></a>
        <div class="qBF1Pd">Highlands Coffee</div>
    </div>
    """
    cafe_la_link = "https://www.google.com/maps/place/Qu%C3%A1n+Cafe+L%E1%BA%A3/@10.771971,106.698345,17z/data=!4m6!3m5!1s0x31752f1111111111:0x2222222222222222"

    # 1. Target link mismatch (Highlands card vs Cafe La link) -> safely rejected
    res = extract_feed_item_dom(card_highlands, link=cafe_la_link)
    assert res == {}

    # 2. Matching target link with URL encoding difference
    highlands_encoded_link = "https://www.google.com/maps/place/Highlands+Coffee/@10.771971,106.698345,17z/data=!4m7!3m6!1s0x31752f3b9c025075:0x7c96362828b8cf44"
    res_match = extract_feed_item_dom(card_highlands, link=highlands_encoded_link)
    assert res_match.get("name") == "Highlands Coffee"
    assert res_match.get("link") == highlands_encoded_link

    # 3. Relative DOM href matches absolute link
    card_relative = """
    <div class="Nv2PK">
        <a class="hfpxzc" href="/maps/place/Cafe+L%E1%BA%A3/@10.7,106.6,17z"></a>
        <div class="qBF1Pd">Cafe Lả</div>
    </div>
    """
    abs_link = "https://www.google.com/maps/place/Cafe+L%E1%BA%A3/@10.7,106.6,17z"
    res_rel = extract_feed_item_dom(card_relative, link=abs_link)
    assert res_rel.get("name") == "Cafe Lả"

    # 4. Conflicting Place ID
    card_place_b = """
    <div class="Nv2PK">
        <a class="hfpxzc" href="https://www.google.com/maps/place/?q=place_id:ChIJplaceBBBBBBBBBBBBBBB"></a>
        <div class="qBF1Pd">Place B</div>
    </div>
    """
    link_place_a = (
        "https://www.google.com/maps/place/?q=place_id:ChIJplaceAAAAAAAAAAAAAAA"
    )
    assert extract_feed_item_dom(card_place_b, link=link_place_a) == {}

    # 5. Missing anchor tag in card HTML
    card_no_a = '<div class="Nv2PK"><div class="qBF1Pd">No Link Cafe</div></div>'
    assert extract_feed_item_dom(card_no_a, link=abs_link) == {}
    assert extract_feed_item_dom(card_no_a) == {}

    # 6. None or non-string card_html
    assert extract_feed_item_dom(None) == {}  # type: ignore
    assert extract_feed_item_dom("") == {}


def test_feed_card_requires_matching_href_and_omits_viewport_coordinates():
    target = "https://www.google.com/maps/place/Branch/@21,105,17z"
    different_branch = target.replace("@21,105", "@22,105")
    card = '<div><a href="{}"></a><h3>Branch</h3></div>'

    assert extract_feed_item_dom(card.format(different_branch), link=target) == {}
    assert extract_feed_item_dom("<h3>Branch</h3>", link=target) == {}
    assert extract_feed_item_dom(card.format(target), link=target) == {
        "name": "Branch",
        "link": target,
    }


def test_is_matching_feed_link_direct():
    """Directly unit tests _is_matching_feed_link multi-tier matching logic."""
    # Exact match
    assert (
        _is_matching_feed_link(
            "https://maps.google.com/place/A", "https://maps.google.com/place/A"
        )
        is True
    )
    # Canonical match with query params stripped
    assert (
        _is_matching_feed_link(
            "https://maps.google.com/place/A?q=1", "https://maps.google.com/place/A?q=2"
        )
        is True
    )
    # Empty inputs
    assert _is_matching_feed_link("", "https://maps.google.com/place/A") is False
    assert _is_matching_feed_link("https://maps.google.com/place/A", "") is False


def test_is_preview_response_for_link_no_id_returns_false():
    """Verifies that is_preview_response_for_link strictly rejects links without unique identifiers."""
    preview_url = "https://www.google.com/maps/preview/place?authuser=0&pb=!1m18!1sChIJdWb_e3MvdTERRLy4KKg2lnw"
    resp_ok = MagicMock(url=preview_url, status=200, ok=True)

    # Simple link without Hex ID or ChIJ ID -> must return False
    assert (
        is_preview_response_for_link(
            resp_ok, "https://www.google.com/maps/place/SomeCafe"
        )
        is False
    )
    assert is_preview_response_for_link(resp_ok, "") is False
    assert is_preview_response_for_link(resp_ok, None) is False  # type: ignore

    # Non-preview response URL
    non_preview_resp = MagicMock(
        url="https://www.google.com/maps/search/cafe", status=200, ok=True
    )
    chij_link = (
        "https://www.google.com/maps/place/?q=place_id:ChIJdWb_e3MvdTERRLy4KKg2lnw"
    )
    assert is_preview_response_for_link(non_preview_resp, chij_link) is False

    # HTTP error response
    error_resp = MagicMock(url=preview_url, status=500, ok=False)
    assert is_preview_response_for_link(error_resp, chij_link) is False


def test_get_reviews_count_dot_thousand_separator():
    """Verifies that get_reviews_count correctly extracts review counts with dot or comma thousand separators."""
    # Direct integer in data[4][8]
    assert (
        get_reviews_count(
            [
                None,
                None,
                None,
                None,
                [None, None, None, None, None, None, None, None, 500],
            ]
        )
        == 500
    )

    # String with dot separator (e.g. Vietnamese / European: "1.234 đánh giá")
    data_dot = [None, None, None, None, [None, None, None, [None, "1.234 đánh giá"]]]
    assert get_reviews_count(data_dot) == 1234

    # String with multiple dots: "1.234.567 đánh giá"
    data_multi_dot = [
        None,
        None,
        None,
        None,
        [None, None, None, [None, "1.234.567 đánh giá"]],
    ]
    assert get_reviews_count(data_multi_dot) == 1234567

    # String with comma separator: "1,234 reviews"
    data_comma = [None, None, None, None, [None, None, None, [None, "1,234 reviews"]]]
    assert get_reviews_count(data_comma) == 1234

    # Parentheses format: "(1.234)"
    data_paren = [None, None, None, None, [None, None, None, [None, "(1.234)"]]]
    assert get_reviews_count(data_paren) == 1234

    # Zero reviews: "0 đánh giá"
    data_zero = [None, None, None, None, [None, None, None, [None, "0 đánh giá"]]]
    assert get_reviews_count(data_zero) == 0

    # Non-numeric string: "Chưa có đánh giá"
    data_none = [None, None, None, None, [None, None, None, [None, "Chưa có đánh giá"]]]
    assert get_reviews_count(data_none) is None

    # Empty data
    assert get_reviews_count([]) is None


def test_calculate_distance_handles_none_and_incomplete_coords():
    """Verifies calculate_distance and is_within_range handle None and 1D-None coordinates returning inf and False."""
    p_vn = (21.0285, 105.8542)  # Hanoi
    p_null_island = (0.0, 0.0)

    # 1. Target is None -> returns inf
    assert calculate_distance(p_vn, None) == float("inf")
    assert calculate_distance(p_null_island, None) == float("inf")

    # 2. Origin is None -> returns inf
    assert calculate_distance(None, p_vn) == float("inf")
    assert calculate_distance(None, None) == float("inf")

    # 3. 1D None coordinates (latitude or longitude is None) -> returns inf
    assert calculate_distance((None, 105.8542), p_vn) == float("inf")
    assert calculate_distance(p_vn, (21.0285, None)) == float("inf")
    assert calculate_distance((None, None), p_vn) == float("inf")

    # 4. Geopy Point with None attributes -> returns inf
    mock_point_none = MagicMock(spec=Point)
    mock_point_none.latitude = None
    mock_point_none.longitude = None
    assert calculate_distance(mock_point_none, p_vn) == float("inf")

    # 5. True Null Island distance between real (0.0, 0.0) coordinates
    assert calculate_distance((0.0, 0.0), (0.0, 0.0)) == 0.0

    # 6. is_within_range safety: None coordinates never fall within range limit
    assert is_within_range(p_null_island, None, range_limit=25000.0) is False
    assert is_within_range(None, p_vn, range_limit=25000.0) is False
    assert is_within_range(p_vn, (21.0285, None), range_limit=25000.0) is False
    assert is_within_range((None, 105.8542), p_vn, range_limit=25000.0) is False
    assert is_within_range((0.0, 0.0), (0.0, 0.0), range_limit=25000.0) is True


def test_default_range_limit_constant_and_export():
    """Kiểm tra hằng số DEFAULT_RANGE_LIMIT trong extractor, __all__ và map_miner package."""
    assert isinstance(DEFAULT_RANGE_LIMIT, float)
    assert DEFAULT_RANGE_LIMIT == 25000.0

    import map_miner.extractor as extractor_module

    assert "DEFAULT_RANGE_LIMIT" in extractor_module.__all__
    assert hasattr(map_miner, "DEFAULT_RANGE_LIMIT")
    assert map_miner.DEFAULT_RANGE_LIMIT == 25000.0
    assert "DEFAULT_RANGE_LIMIT" in map_miner.__all__


def test_is_within_range_default_parameter():
    """Kiểm tra giá trị mặc định của tham số range_limit trong is_within_range là DEFAULT_RANGE_LIMIT."""
    sig = inspect.signature(is_within_range)
    assert "range_limit" in sig.parameters
    assert sig.parameters["range_limit"].default == DEFAULT_RANGE_LIMIT
    assert sig.parameters["range_limit"].default is DEFAULT_RANGE_LIMIT

    # 1. Hai điểm cách nhau ~900m (Hoan Kiem - Hai Ba Trung, Hanoi) -> nằm trong bán kính mặc định 25km
    p_hoan_kiem = (21.0285, 105.8542)
    p_hai_ba_trung = (21.0188, 105.8560)
    assert is_within_range(p_hoan_kiem, p_hai_ba_trung) is True

    # 2. Hai điểm cách nhau > 25km (Hanoi - Bac Ninh ~29km) -> ngoài bán kính mặc định
    p_bac_ninh = (21.1861, 106.0763)
    assert calculate_distance(p_hoan_kiem, p_bac_ninh) > 25000.0
    assert is_within_range(p_hoan_kiem, p_bac_ninh) is False

    # 3. Điểm None hoặc tọa độ thiếu trả về False khi dùng range_limit mặc định
    assert is_within_range(None, p_hoan_kiem) is False
    assert is_within_range(p_hoan_kiem, None) is False
    assert is_within_range((None, 105.85), p_hoan_kiem) is False
    assert is_within_range(p_hoan_kiem, (21.02, None)) is False
