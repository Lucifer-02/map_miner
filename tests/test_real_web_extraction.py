from pathlib import Path

import pytest

from map_miner.extractor import (
    extract_place_data,
    get_address_components,
    parse_preview_json,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def real_preview_text() -> str:
    preview_file = FIXTURES_DIR / "real_preview.txt"
    assert preview_file.exists(), f"Missing fixture: {preview_file}"
    return preview_file.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def real_html_text() -> str:
    html_file = FIXTURES_DIR / "real_place.html"
    assert html_file.exists(), f"Missing fixture: {html_file}"
    return html_file.read_text(encoding="utf-8")


def test_real_preview_json_parsing(real_preview_text: str):
    blob = parse_preview_json(real_preview_text)
    assert blob is not None
    assert isinstance(blob, list)
    assert len(blob) > 100


def test_real_preview_json_full_extraction(real_preview_text: str):
    data = extract_place_data(preview_json=real_preview_text)
    assert data is not None

    # Core Identifiers
    assert data["name"] == "MỘC LAB"
    assert data["place_id"] == "ChIJlY6RQ8ysNTERHSuxkiDz6Is"

    # Coordinates
    assert data["latitude"] == pytest.approx(20.984367, abs=1e-4)
    assert data["longitude"] == pytest.approx(105.7836899, abs=1e-4)

    # Address & Detailed Components (with Vietnamese diacritics preserved)
    assert (
        data["address"] == "11 BT16A1, Làng Việt kiều Châu Âu, Hà Đông, Hà Nội, Vietnam"
    )
    assert data["street"] == "11 BT16A1"
    assert data["sublocality"] == "Làng Việt kiều Châu Âu"
    assert data["district"] == "Hà Đông"
    assert data["city"] == "Hà Nội"
    assert data["country_code"] == "VN"
    assert data["plus_code"] == "XQMM+PF Ha Dong, Ha Noi, Vietnam"

    # Ratings & Reviews
    assert data["rating"] == 4.5
    assert data["reviews_count"] == 253

    # Business Information
    assert data["categories"] == ["Cafe"]
    assert data["phone"] == "+84 334 097 895"
    assert data["website"] == "https://www.facebook.com/moclabcafe"
    assert data["is_claimed"] is True
    assert data["timezone"] == "Asia/Saigon"

    # Operating Status & Hours
    assert "Open" in data["open_status"]
    assert isinstance(data["opening_hours"], dict)
    assert "Saturday" in data["opening_hours"]
    assert "Monday" in data["opening_hours"]

    # Media & Amenities
    assert data["photos_count"] == 9
    assert isinstance(data["photos"], list)
    assert len(data["photos"]) > 0
    assert all(p.startswith("https://") for p in data["photos"])
    assert data["thumbnail"].startswith("https://")
    assert isinstance(data["amenities"], list)


def test_real_html_extraction(real_html_text: str):
    data = extract_place_data(html_content=real_html_text)
    assert data is not None

    assert data["name"] == "MỘC LAB"
    assert "11 BT16A1" in data["address"]
    assert data["phone"] == "+84 334 097 895"
    assert data["website"] == "https://www.facebook.com/moclabcafe"
    assert data["rating"] == 4.5
    assert data["reviews_count"] == 253
    assert data["categories"] == ["Cafe"]

    # Address components from HTML / DOM
    assert data["street"] == "11 BT16A1"
    assert data["sublocality"] == "Làng Việt kiều Châu Âu"
    assert data["district"] == "Hà Đông"
    assert data["city"] == "Hà Nội"


def test_real_data_fields_filtering(real_preview_text: str):
    requested_fields = ["name", "place_id", "rating", "phone", "website"]
    filtered_data = extract_place_data(
        preview_json=real_preview_text, fields=requested_fields
    )
    assert filtered_data is not None
    assert list(filtered_data.keys()) == requested_fields
    assert filtered_data["name"] == "MỘC LAB"
    assert filtered_data["place_id"] == "ChIJlY6RQ8ysNTERHSuxkiDz6Is"
    assert filtered_data["rating"] == 4.5
    assert filtered_data["phone"] == "+84 334 097 895"
    assert filtered_data["website"] == "https://www.facebook.com/moclabcafe"


def test_real_address_components_direct(real_preview_text: str):
    blob = parse_preview_json(real_preview_text)
    assert blob is not None
    comps = get_address_components(blob)
    assert comps["street"] == "11 BT16A1"
    assert comps["sublocality"] == "Làng Việt kiều Châu Âu"
    assert comps["district"] == "Hà Đông"
    assert comps["city"] == "Hà Nội"


def test_output_schema_conformance(real_preview_text: str):
    """
    Validates that real web extracted data conforms strictly to CONTEXT.md Section 4 schema.
    """
    data = extract_place_data(preview_json=real_preview_text)
    assert data is not None

    expected_schema_types = {
        "name": str,
        "place_id": str,
        "latitude": float,
        "longitude": float,
        "plus_code": str,
        "address": str,
        "street": str,
        "sublocality": str,
        "district": str,
        "city": str,
        "rating": float,
        "reviews_count": int,
        "categories": list,
        "phone": str,
        "website": str,
        "open_status": str,
        "opening_hours": dict,
        "timezone": str,
        "amenities": list,
        "photos_count": int,
        "photos": list,
        "thumbnail": str,
        "is_claimed": bool,
        "country_code": str,
    }

    for key, expected_type in expected_schema_types.items():
        assert key in data, f"Key '{key}' missing from extracted real data"
        assert isinstance(data[key], expected_type), (
            f"Key '{key}' expected {expected_type.__name__}, got {type(data[key]).__name__}"
        )
