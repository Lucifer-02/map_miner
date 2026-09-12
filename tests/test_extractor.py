import json

from map_miner.extractor import (
    extract_place_data,
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
    assert "Ha Noi" in res["city"]
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
