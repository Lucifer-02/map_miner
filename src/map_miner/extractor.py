import json
import logging
import math
import re
import unicodedata
from collections.abc import Sequence
from typing import Any
from urllib.parse import quote_plus, unquote, urlsplit

import polars as pl
from bs4 import BeautifulSoup
from geopy.distance import geodesic
from geopy.point import Point

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_FLATTEN_COLUMNS",
    "DEFAULT_RANGE_LIMIT",
    "REQUIRED_COLUMNS",
    "calculate_distance",
    "compare_place_url_ids",
    "extract_coordinates_from_url",
    "extract_feed_item_dom",
    "extract_initial_json",
    "extract_place_coordinates_from_url",
    "extract_place_data",
    "extract_place_identity_from_url",
    "format_places_dataframe",
    "get_address_components",
    "is_preview_response_for_link",
    "is_within_range",
    "make_place_url",
    "parse_address_string_fallback",
    "parse_dom_from_html",
    "parse_json_data",
    "parse_preview_json",
    "preview_blob_matches_link",
    "safe_get",
    "strip_accents",
]

# --- Schema Columns ---
DEFAULT_FLATTEN_COLUMNS: tuple[str, ...] = (
    "name",
    "place_id",
    "latitude",
    "longitude",
    "address",
    "link",
    "categories",
    "rating",
    "reviews_count",
    "plus_code",
    "city",
)
REQUIRED_COLUMNS: tuple[str, ...] = (
    DEFAULT_FLATTEN_COLUMNS  # Backward compatibility alias
)
DEFAULT_RANGE_LIMIT: float = 25000.0  # 25 km in meters
_TEXT_FIELDS = {
    "name",
    "place_id",
    "address",
    "link",
    "plus_code",
    "city",
    "street",
    "sublocality",
    "district",
    "postal_code",
    "price_level",
    "phone",
    "website",
    "open_status",
    "timezone",
    "thumbnail",
    "country_code",
    "menu_url",
    "query",
}
_FLOAT_FIELDS = {"latitude", "longitude", "rating"}
_COUNT_FIELDS = {"reviews_count", "photos_count"}
_TEXT_LIST_FIELDS = {"categories", "amenities", "photos"}
_ADDRESS_COMPONENT_FIELDS = {"street", "sublocality", "district", "city", "postal_code"}
_OUTPUT_FIELDS = {"link", "query"}


def _float_value(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _int_value(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if -(2**63) <= number < 2**63 else None


def _normalize_field_value(field: str, value: Any) -> Any:
    if value is None:
        return None
    if field in _TEXT_FIELDS:
        if not isinstance(value, str):
            return None
        if field == "query":
            return value
        return value.strip() or None
    if field in _FLOAT_FIELDS:
        number = _float_value(value)
        if number is not None and (
            (field == "latitude" and abs(number) > 90)
            or (field == "longitude" and abs(number) > 180)
        ):
            return None
        return number
    if field in _COUNT_FIELDS:
        return _int_value(value)
    if field in _TEXT_LIST_FIELDS:
        if field == "categories" and isinstance(value, str):
            value = [value]
        if not isinstance(value, (list, tuple)):
            return None
        return [
            item.strip() for item in value if isinstance(item, str) and item.strip()
        ] or None
    if field == "is_claimed":
        return value if isinstance(value, bool) else None
    if field == "opening_hours":
        if not isinstance(value, dict):
            return None
        return {
            day: hours
            for day, hours in value.items()
            if isinstance(day, str) and isinstance(hours, str) and day and hours
        } or None
    return value


def safe_get(data: Any, *keys: Any) -> Any:
    """
    Safely retrieves nested data from a dictionary or list using a sequence of keys/indices.
    Returns None if any key/index is not found or if the data structure is invalid.
    """
    current = data
    for key in keys:
        try:
            if isinstance(current, list):
                if isinstance(key, int) and 0 <= key < len(current):
                    current = current[key]
                else:
                    return None
            elif isinstance(current, dict):
                if key in current:
                    current = current[key]
                else:
                    return None
            else:
                return None
        except (IndexError, TypeError, KeyError):
            return None
    return current


def extract_initial_json(html_content: str) -> str | None:
    """
    Extracts the JSON string assigned to window.APP_INITIALIZATION_STATE from HTML content.
    """
    try:
        match = re.search(
            r";window\.APP_INITIALIZATION_STATE\s*=\s*(.*?);window\.APP_FLAGS",
            html_content,
            re.DOTALL,
        )
        if match:
            json_str = match.group(1)
            if json_str.strip().startswith(("[", "{")):
                return json_str
            logger.debug("Extracted content doesn't look like valid JSON start.")
            return None
        logger.debug("APP_INITIALIZATION_STATE pattern not found.")
        return None
    except (re.error, TypeError) as e:
        logger.debug("Error extracting JSON string: %s", e)
        return None


def parse_json_data(json_str: str) -> list | None:
    """
    Parses the window.APP_INITIALIZATION_STATE JSON string to extract the inner data blob.
    Returns the data blob list or None.
    """
    if not json_str:
        return None
    try:
        initial_data = json.loads(json_str)

        # Check path [3][6]
        if (
            isinstance(initial_data, list)
            and len(initial_data) > 3
            and isinstance(initial_data[3], list)
            and len(initial_data[3]) > 6
        ):
            data_blob_or_str = initial_data[3][6]
            if isinstance(data_blob_or_str, list):
                return data_blob_or_str
            if isinstance(data_blob_or_str, str) and ")]}'" in data_blob_or_str:
                json_str_inner = data_blob_or_str.split(")]}'", 1)[1].strip()
                actual_data = json.loads(json_str_inner)
                if isinstance(actual_data, list) and len(actual_data) > 6:
                    potential_data_blob = safe_get(actual_data, 6)
                    if isinstance(potential_data_blob, list):
                        return potential_data_blob

        # Check alternative path [3][5]
        alt_blob = safe_get(initial_data, 3, 5)
        if isinstance(alt_blob, str) and ")]}'" in alt_blob:
            alt_parsed = json.loads(alt_blob.split(")]}'", 1)[1].strip())
            if isinstance(alt_parsed, list) and len(alt_parsed) > 0:
                first_entry = alt_parsed[0]
                if isinstance(first_entry, list):
                    return first_entry
    except (json.JSONDecodeError, IndexError, TypeError, KeyError) as e:
        logger.debug("Error parsing initial JSON data: %s", e)

    return None


def parse_preview_json(json_str: str) -> list | None:
    """
    Parses the Google Maps preview/place XHR response, extracting the inner rich data blob at actual_data[6].
    """
    if not json_str:
        return None
    try:
        text = json_str.strip()
        if text.startswith(")]}'"):
            text = text.split(")]}'", 1)[1].strip()
        data = json.loads(text)
        if isinstance(data, list) and len(data) > 6 and isinstance(data[6], list):
            return data[6]
    except (json.JSONDecodeError, IndexError, TypeError) as e:
        logger.debug("Error parsing preview JSON: %s", e)
    return None


# --- Field Extraction Functions (Indices relative to the data_blob returned by parse_json_data) ---


def get_main_name(data: list) -> str | None:
    """Extracts the main name of the place."""
    return safe_get(data, 11)


def get_place_id(data: list) -> str | None:
    """Extracts the Google Place ID (canonical ChIJ... or hex ID)."""
    return _normalize_field_value(
        "place_id", safe_get(data, 78)
    ) or _normalize_field_value("place_id", safe_get(data, 10))


def get_latitude(data: list) -> float | None:
    """Extracts latitude coordinate."""
    lat = _normalize_field_value("latitude", safe_get(data, 9, 2))
    if lat is not None:
        return lat
    return _normalize_field_value("latitude", safe_get(data, 208, 0, 2))


def get_longitude(data: list) -> float | None:
    """Extracts longitude coordinate."""
    lon = _normalize_field_value("longitude", safe_get(data, 9, 3))
    if lon is not None:
        return lon
    return _normalize_field_value("longitude", safe_get(data, 208, 0, 3))


def get_complete_address(data: list) -> str | None:
    """Extracts structured address components and joins them."""
    full_address = safe_get(data, 39)
    if isinstance(full_address, str) and full_address.strip():
        return full_address.strip()
    address_parts = safe_get(data, 2)
    if isinstance(address_parts, list):
        str_parts = [
            p.strip() for p in address_parts if isinstance(p, str) and p.strip()
        ]
        if str_parts:
            return ", ".join(str_parts)
    return None


def strip_accents(s: str) -> str:
    """Removes diacritics/accents from a string for loose matching."""
    return (
        "".join(
            c
            for c in unicodedata.normalize("NFD", s)
            if unicodedata.category(c) != "Mn"
        )
        .replace("đ", "d")
        .replace("Đ", "D")
    )


def get_address_components(data: list) -> dict[str, str | None]:
    """
    Extracts individual address components:
      - street: House number and street/route name
      - sublocality: Ward, neighborhood, or commune
      - district: District, county, or borough
      - city: City or province
      - postal_code: Postal/ZIP code
    """
    res: dict[str, str | None] = {
        "street": None,
        "sublocality": None,
        "district": None,
        "city": None,
        "postal_code": None,
    }
    if not data or not isinstance(data, list):
        return res

    raw_comp = safe_get(data, 183, 1)
    if not raw_comp or not isinstance(raw_comp, list):
        return res

    # 1. Street
    if len(raw_comp) > 1 and raw_comp[1]:
        res["street"] = str(raw_comp[1]).strip()
    elif len(raw_comp) > 2 and raw_comp[2]:
        res["street"] = str(raw_comp[2]).strip()

    # 2. City / Province
    if len(raw_comp) > 3 and raw_comp[3]:
        res["city"] = str(raw_comp[3]).strip()

    # 3. Postal Code
    if len(raw_comp) > 4 and raw_comp[4]:
        res["postal_code"] = str(raw_comp[4]).strip()

    # 4. District / County / Area
    if len(raw_comp) > 5 and raw_comp[5]:
        dist_raw = str(raw_comp[5]).strip()
        if "," in dist_raw:
            parts = [p.strip() for p in dist_raw.split(",") if p.strip()]
            res["district"] = parts[0]
        else:
            res["district"] = dist_raw

    # Sanity check: street cannot be identical to district or city
    if res["street"]:
        st_norm = strip_accents(res["street"]).lower()
        if (res["district"] and st_norm == strip_accents(res["district"]).lower()) or (
            res["city"] and st_norm == strip_accents(res["city"]).lower()
        ):
            res["street"] = None

    # 5. Sublocality / Ward / Neighborhood
    if len(raw_comp) > 0 and raw_comp[0]:
        sub_raw = str(raw_comp[0]).strip()
        dist = res["district"] or ""
        city = res["city"] or ""
        if dist and dist.lower() in sub_raw.lower():
            cleaned = sub_raw
            for sep in [", " + dist, "," + dist, dist]:
                if cleaned.lower().endswith(sep.lower()):
                    cleaned = cleaned[: len(cleaned) - len(sep)].rstrip(",").strip()
                    break
            if cleaned:
                res["sublocality"] = cleaned
        elif sub_raw.lower() != dist.lower() and sub_raw.lower() != city.lower():
            res["sublocality"] = sub_raw

    # 6. Enhance with accented local names from data[2] or data[183][0][0][1] if available
    candidates: list[str] = []
    addr_parts = safe_get(data, 2)
    if isinstance(addr_parts, list):
        candidates.extend([str(x).strip() for x in addr_parts if x])

    nested_tokens = safe_get(data, 183, 0, 0, 1)
    if isinstance(nested_tokens, list):
        for item in nested_tokens:
            if isinstance(item, list) and len(item) > 0 and item[0]:
                candidates.append(str(item[0]).strip())

    for field in ["district", "city", "sublocality"]:
        val = res.get(field)
        if val:
            val_norm = strip_accents(val).lower()
            for cand in candidates:
                cand_clean = cand.split(",")[0].strip()
                if strip_accents(cand_clean).lower() == val_norm:
                    res[field] = cand_clean
                    break

    return res


def parse_address_string_fallback(address_str: str) -> dict[str, str | None]:
    """
    Heuristic fallback to extract address components from formatted address string.
    """
    res: dict[str, str | None] = {
        "street": None,
        "sublocality": None,
        "district": None,
        "city": None,
        "postal_code": None,
    }
    if not address_str or not isinstance(address_str, str):
        return res

    parts = [p.strip() for p in address_str.split(",") if p.strip()]
    # Remove trailing country if present (e.g., Vietnam, United States)
    if len(parts) >= 2 and parts[-1].lower() in [
        "vietnam",
        "việt nam",
        "united states",
        "usa",
        "us",
        "vn",
    ]:
        parts = parts[:-1]

    if len(parts) >= 4:
        res["street"] = parts[0]
        res["sublocality"] = parts[1]
        res["district"] = parts[2]
        city_candidate = parts[3]
        zip_m = re.search(r"\b(\d{5,6})\b", city_candidate)
        if zip_m:
            res["postal_code"] = zip_m.group(1)
            city_candidate = city_candidate.replace(zip_m.group(1), "").strip()
        res["city"] = city_candidate if city_candidate else None
    elif len(parts) == 3:
        res["street"] = parts[0]
        res["district"] = parts[1]
        res["city"] = parts[2]
    elif len(parts) == 2:
        # e.g. "Ha Dong, Ha Noi"
        res["district"] = parts[0]
        res["city"] = parts[1]
    elif len(parts) == 1:
        res["city"] = parts[0]
    return res


def get_rating(data: list) -> float | None:
    """Extracts the average star rating."""
    return _float_value(safe_get(data, 4, 7))


def get_reviews_count(data: list) -> int | None:
    """Extracts the total number of reviews, supporting both comma and dot thousand separators."""
    count = _int_value(safe_get(data, 4, 8))
    if count is not None:
        return count
    rev_str = safe_get(data, 4, 3, 1)
    if isinstance(rev_str, str):
        m = re.search(r"([0-9,.]+)", rev_str)
        if m:
            clean_num = m.group(1).replace(",", "").replace(".", "").strip()
            return _int_value(clean_num)
    return None


def get_website(data: list) -> str | None:
    """Extracts the primary website link."""
    return safe_get(data, 7, 0)


def get_plus_code(data: list) -> str | None:
    """Extracts the Google Plus Code / compound code."""
    pc = safe_get(data, 183, 2, 2, 0)
    if isinstance(pc, str) and pc.strip():
        return pc.strip()
    return None


def get_price_level(data: list) -> str | None:
    """Extracts the price tier/level."""
    lvl = safe_get(data, 4, 2)
    if isinstance(lvl, int) and not isinstance(lvl, bool) and 0 < lvl <= 5:
        return "$" * lvl
    if isinstance(lvl, str) and lvl.strip():
        return lvl.strip()
    return None


def get_open_status(data: list) -> str | None:
    """Extracts real-time open/closed status string."""
    status = safe_get(data, 203, 1, 4, 0) or safe_get(data, 203, 1, 8, 0)
    if isinstance(status, str) and status.strip():
        return status.strip()
    return None


def get_opening_hours(data: list) -> dict[str, str] | None:
    """Extracts opening hours schedule dict."""
    hours_raw = safe_get(data, 203, 0)
    if hours_raw and isinstance(hours_raw, list):
        res: dict[str, str] = {}
        for entry in hours_raw:
            if isinstance(entry, list) and len(entry) > 0:
                day = entry[0]
                time_str = safe_get(entry, 3, 0, 0)
                if day and time_str:
                    res[str(day)] = str(time_str)
        if res:
            return res
    return None


def get_timezone(data: list) -> str | None:
    """Extracts local timezone."""
    tz = safe_get(data, 30)
    return tz if isinstance(tz, str) else None


def get_amenities(data: list) -> list[str] | None:
    """Extracts business amenities, service options and accessibility features."""
    amenities: list[str] = []
    groups = safe_get(data, 100, 1) or []
    if isinstance(groups, list):
        for group in groups:
            items = safe_get(group, 2) or []
            if isinstance(items, list):
                for item in items:
                    label = safe_get(item, 1)
                    if label and isinstance(label, str) and label not in amenities:
                        amenities.append(label)
    return amenities if amenities else None


def get_photos_count(data: list) -> int | None:
    """Extracts total photo count."""
    count = safe_get(data, 146, 0)
    if isinstance(count, int):
        return count
    return None


def get_photos(data: list) -> list[str] | None:
    """Extracts list of prominent photo URLs."""
    photos: list[str] = []
    for candidates in (safe_get(data, 72, 0), safe_get(data, 51, 0)):
        if isinstance(candidates, list):
            for candidate in candidates:
                url = safe_get(candidate, 6, 0)
                if isinstance(url, str) and url and url not in photos:
                    photos.append(url)
    return photos if photos else None


def get_is_claimed(data: list) -> bool | None:
    """Checks whether the business is claimed by its owner."""
    claimed = safe_get(data, 57)
    if not isinstance(claimed, list):
        return None
    return safe_get(claimed, 1) is not None or safe_get(claimed, 2) is not None


def get_country_code(data: list) -> str | None:
    """Extracts country code (e.g., VN)."""
    cc = safe_get(data, 243)
    if isinstance(cc, str) and cc.strip():
        return cc.strip()
    raw_comp = safe_get(data, 183, 1)
    if isinstance(raw_comp, list) and len(raw_comp) > 6 and raw_comp[6]:
        return str(raw_comp[6]).strip()
    return None


def _find_phone_recursively(data_structure: Any) -> str | None:
    """
    Recursively searches a nested list/dict structure for a list containing
    the phone icon URL followed by the phone number string.
    """
    if isinstance(data_structure, list):
        if (
            len(data_structure) >= 2
            and isinstance(data_structure[0], str)
            and "call_googblue" in data_structure[0]
            and isinstance(data_structure[1], str)
        ):
            phone_number_str = data_structure[1]
            standardized_number = re.sub(r"\D", "", phone_number_str)
            if standardized_number:
                return standardized_number

        for item in data_structure:
            found_phone = _find_phone_recursively(item)
            if found_phone:
                return found_phone

    elif isinstance(data_structure, dict):
        for value in data_structure.values():
            found_phone = _find_phone_recursively(value)
            if found_phone:
                return found_phone

    return None


def get_phone_number(data_blob: list) -> str | None:
    """
    Extracts and standardizes the primary phone number by checking direct index
    or recursively searching data_blob.
    """
    phone = safe_get(data_blob, 178, 0, 0)
    if isinstance(phone, str) and phone.strip():
        return phone.strip()
    return _find_phone_recursively(data_blob)


def get_categories(data: list) -> list[str] | None:
    """Extracts the list of categories/types."""
    cats = safe_get(data, 13)
    if isinstance(cats, list):
        return cats
    if isinstance(cats, str):
        return [cats]
    return None


def parse_rating_reviews_from_html(
    html_content: str,
) -> tuple[float | None, int | None]:
    """
    Fallback parsing for rating and review count directly from HTML attributes.
    """
    rating = None
    reviews = None

    m_rating = re.search(r'aria-label="([0-9.]+)\s*stars', html_content, re.IGNORECASE)
    if m_rating:
        try:
            rating = float(m_rating.group(1))
        except (TypeError, ValueError):
            rating = None

    m_reviews = re.search(
        r'aria-label="([0-9,]+)\s*reviews', html_content, re.IGNORECASE
    )
    if m_reviews:
        try:
            reviews = int(m_reviews.group(1).replace(",", ""))
        except (TypeError, ValueError):
            reviews = None

    return rating, reviews


def get_thumbnail(data: list) -> str | None:
    """Extracts the main thumbnail image URL."""
    return (
        safe_get(data, 72, 0, 0, 6, 0)
        or safe_get(data, 37, 0, 1, 6, 0)
        or safe_get(data, 51, 0, 0, 6, 0)
        or safe_get(data, 14, 0, 0, 6, 0)
    )


def parse_dom_from_html(html_content: str) -> dict[str, Any]:
    """
    Extracts place attributes from rendered DOM HTML using BeautifulSoup.
    Pure function without any side effects.
    """
    if not html_content or not isinstance(html_content, str):
        return {}

    soup = BeautifulSoup(html_content, "html.parser")
    dom_data: dict[str, Any] = {}

    def _attr(element: Any, attribute: str) -> str:
        val = element.get(attribute, "")
        if isinstance(val, list):
            return " ".join(val)
        return str(val) if val is not None else ""

    def _extract_labeled(selector: str, prefix: str) -> str | None:
        el = soup.select_one(selector)
        if not el:
            return None
        label = _attr(el, "aria-label")
        val = re.sub(rf"^{prefix}:\s*", "", label, flags=re.IGNORECASE).strip()
        return val or el.get_text(strip=True) or None

    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        dom_data["name"] = h1.get_text(strip=True)

    # Plus code
    if pc := _extract_labeled(
        "[data-item-id='oloc'], [aria-label*='Plus code:']", "Plus code"
    ):
        dom_data["plus_code"] = pc

    # Address
    if addr := _extract_labeled(
        "[data-item-id='address'], [aria-label^='Address:']", "Address"
    ):
        dom_data["address"] = addr

    # Phone
    if phone := _extract_labeled(
        "[data-item-id^='phone:'], [aria-label^='Phone:']", "Phone"
    ):
        dom_data["phone"] = phone

    # Website
    web_el = soup.select_one("[data-item-id='authority'], [aria-label^='Website:']")
    if web_el:
        dom_data["website"] = (
            _attr(web_el, "href")
            or _attr(web_el, "aria-label").replace("Website:", "").strip()
        )

    # Menu url
    menu_el = soup.select_one("a[data-item-id='menu'], a[aria-label*='Menu']")
    if menu_el and _attr(menu_el, "href"):
        dom_data["menu_url"] = _attr(menu_el, "href")

    # Price level
    pr_el = soup.select_one(
        "[aria-label*='Price:'], [aria-label*='Moderate'], [aria-label*='Inexpensive']"
    )
    if pr_el:
        dom_data["price_level"] = (
            _attr(pr_el, "aria-label") or pr_el.get_text(strip=True)
        ).strip()

    # Open status
    os_el = soup.select_one("[aria-label*='Open'], [aria-label*='Closed']")
    if os_el:
        dom_data["open_status"] = (
            _attr(os_el, "aria-label") or os_el.get_text(strip=True)
        ).strip()

    # Opening hours table
    hours: dict[str, str] = {}
    valid_days = [
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
        "thứ hai",
        "thứ ba",
        "thứ tư",
        "thứ năm",
        "thứ sáu",
        "thứ bảy",
        "chủ nhật",
    ]
    for tr in soup.select("table tr"):
        tds = tr.find_all("td")
        if len(tds) >= 2:
            day = tds[0].get_text(strip=True)
            if any(d in day.lower() for d in valid_days):
                hours[day] = tds[1].get_text(strip=True)
    if hours:
        dom_data["opening_hours"] = hours

    # Rating & Reviews from DOM elements
    stars_el = soup.select_one("[aria-label*='stars']")
    if stars_el:
        stars_label = _attr(stars_el, "aria-label")
        m = re.search(r"([0-9.]+)\s*stars", stars_label, re.IGNORECASE)
        if m:
            try:
                dom_data["rating"] = float(m.group(1))
            except ValueError:
                pass

    rev_el = soup.select_one("[aria-label*='reviews']")
    if rev_el:
        rev_label = _attr(rev_el, "aria-label")
        m = re.search(r"([0-9,]+)\s*reviews", rev_label, re.IGNORECASE)
        if m:
            try:
                dom_data["reviews_count"] = int(m.group(1).replace(",", ""))
            except ValueError:
                pass

    # Fallback to regex if still missing
    if "rating" not in dom_data or "reviews_count" not in dom_data:
        r, rc = parse_rating_reviews_from_html(html_content)
        if r is not None and "rating" not in dom_data:
            dom_data["rating"] = r
        if rc is not None and "reviews_count" not in dom_data:
            dom_data["reviews_count"] = rc

    # Categories
    cat_el = soup.select_one("button[jsaction*='category']")
    if cat_el and cat_el.get_text(strip=True):
        dom_data["categories"] = [cat_el.get_text(strip=True)]

    return dom_data


def _extract_from_blob(
    data_blob: list, wanted: set[str] | None = None
) -> dict[str, Any]:
    """
    Consolidated extractor for Google Maps place data array (used for both
    rich preview XHR payloads and APP_INITIALIZATION_STATE blobs).
    """

    def is_wanted(field_name: str) -> bool:
        return wanted is None or field_name in wanted

    addr_comps: dict[str, str | None] = {}
    if wanted is None or not wanted.isdisjoint(_ADDRESS_COMPONENT_FIELDS):
        addr_comps = get_address_components(data_blob)

    candidate_data: dict[str, Any] = {
        "name": get_main_name(data_blob) if is_wanted("name") else None,
        "place_id": get_place_id(data_blob) if is_wanted("place_id") else None,
        "latitude": get_latitude(data_blob) if is_wanted("latitude") else None,
        "longitude": get_longitude(data_blob) if is_wanted("longitude") else None,
        "plus_code": get_plus_code(data_blob) if is_wanted("plus_code") else None,
        "address": get_complete_address(data_blob) if is_wanted("address") else None,
        "street": addr_comps.get("street") if is_wanted("street") else None,
        "sublocality": addr_comps.get("sublocality")
        if is_wanted("sublocality")
        else None,
        "district": addr_comps.get("district") if is_wanted("district") else None,
        "city": addr_comps.get("city") if is_wanted("city") else None,
        "postal_code": addr_comps.get("postal_code")
        if is_wanted("postal_code")
        else None,
        "rating": get_rating(data_blob) if is_wanted("rating") else None,
        "reviews_count": get_reviews_count(data_blob)
        if is_wanted("reviews_count")
        else None,
        "price_level": get_price_level(data_blob) if is_wanted("price_level") else None,
        "categories": get_categories(data_blob) if is_wanted("categories") else None,
        "phone": get_phone_number(data_blob) if is_wanted("phone") else None,
        "website": get_website(data_blob) if is_wanted("website") else None,
        "open_status": get_open_status(data_blob) if is_wanted("open_status") else None,
        "opening_hours": get_opening_hours(data_blob)
        if is_wanted("opening_hours")
        else None,
        "timezone": get_timezone(data_blob) if is_wanted("timezone") else None,
        "amenities": get_amenities(data_blob) if is_wanted("amenities") else None,
        "photos_count": get_photos_count(data_blob)
        if is_wanted("photos_count")
        else None,
        "photos": get_photos(data_blob) if is_wanted("photos") else None,
        "thumbnail": get_thumbnail(data_blob) if is_wanted("thumbnail") else None,
        "is_claimed": get_is_claimed(data_blob) if is_wanted("is_claimed") else None,
        "country_code": get_country_code(data_blob)
        if is_wanted("country_code")
        else None,
    }
    return {
        field: normalized
        for field, value in candidate_data.items()
        if (normalized := _normalize_field_value(field, value)) is not None
    }


def extract_place_data(
    html_content: str | None = None,
    preview_blob: list | None = None,
    preview_json: str | None = None,
    dom_data: dict[str, Any] | None = None,
    fields: Sequence[str] | set[str] | None = None,
) -> dict[str, Any] | None:
    """
    High-level function to orchestrate pure extraction from HTML content,
    rich preview response blob/JSON, and/or DOM extracted data.
    Completely free of side effects.

    Args:
        html_content: Raw HTML text of the page.
        preview_blob: Pre-parsed Google Maps preview list structure.
        preview_json: Raw string of /maps/preview/place response.
        dom_data: Optional pre-extracted DOM dictionary.
        fields: Optional sequence/set of field names to extract. If None, extracts all available fields.
    """
    # 1. Parse preview JSON if provided as string
    if preview_blob is None and preview_json:
        preview_blob = parse_preview_json(preview_json)

    place_details: dict[str, Any] = {}
    wanted = set(fields) - _OUTPUT_FIELDS if fields is not None else None
    extract_fields = wanted
    if wanted is not None and not wanted.isdisjoint(_ADDRESS_COMPONENT_FIELDS):
        extract_fields = wanted | {"address"}

    def is_wanted(f: str) -> bool:
        return wanted is None or f in wanted

    # 1. First priority: rich preview blob from /maps/preview/place
    if preview_blob and isinstance(preview_blob, list):
        place_details = _extract_from_blob(preview_blob, extract_fields)

    # 2. Second priority: extract from HTML (APP_INITIALIZATION_STATE) if fields are missing
    if html_content and (wanted is None or wanted - place_details.keys()):
        json_str = extract_initial_json(html_content)
        if json_str:
            data_blob = parse_json_data(json_str)
            if data_blob:
                missing = (
                    extract_fields - place_details.keys()
                    if extract_fields is not None
                    else None
                )
                html_details = _extract_from_blob(data_blob, missing)
                place_details.update(
                    {
                        field: value
                        for field, value in html_details.items()
                        if field not in place_details
                    }
                )

    # 3. Third priority: DOM data fallback for missing fields
    if (
        dom_data is None
        and html_content
        and (wanted is None or wanted - place_details.keys())
    ):
        dom_data = parse_dom_from_html(html_content)
    if dom_data and isinstance(dom_data, dict):
        for field, value in dom_data.items():
            if (
                extract_fields is None or field in extract_fields
            ) and field not in place_details:
                normalized = _normalize_field_value(field, value)
                if normalized is not None:
                    place_details[field] = normalized

    # 4. Fallback heuristic for address components if full address is available but components are missing
    if "address" in place_details and any(
        is_wanted(k) and k not in place_details for k in _ADDRESS_COMPONENT_FIELDS
    ):
        fb_comps = parse_address_string_fallback(place_details["address"])
        for k, v in fb_comps.items():
            if v and is_wanted(k) and k not in place_details:
                place_details[k] = v

    if not place_details or (wanted is not None and wanted.isdisjoint(place_details)):
        return None

    # If specific fields are requested, return dict preserving the requested order
    if fields is not None:
        return {f: place_details.get(f) for f in fields if f != "link"}

    return place_details


def make_place_url(
    query: str, geo_coordinates: Point, zoom: float, lang: str = "en"
) -> str:
    """Builds a localized Google Maps search URL."""
    encoded_query = quote_plus(query)
    return (
        f"https://www.google.com/maps/search/{encoded_query}/"
        f"@{geo_coordinates.latitude},{geo_coordinates.longitude},{zoom}z?hl={lang}"
    )


def extract_coordinates_from_url(url: str) -> tuple[float, float] | None:
    """Extracts geographic coordinates (latitude, longitude) from a Google Maps URL.

    Matches formats:
    1. Feed / detail link protobuf format: '!3d<lat>...!4d<lon>'
    2. Viewport coordinate format: '@<lat>,<lon>'

    Args:
        url (str): The Google Maps URL string.

    Returns:
        tuple[float, float] | None: (latitude, longitude) tuple or None if not found/invalid.
    """
    if not url or not isinstance(url, str):
        return None

    decoded_url = unquote(url)
    place_coords = _coordinates_from_match(
        re.search(r"!3d(-?\d+(?:\.\d+)?).*?!4d(-?\d+(?:\.\d+)?)", decoded_url)
    )
    if place_coords is not None:
        return place_coords

    return _coordinates_from_match(
        re.search(r"@(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)", decoded_url)
    )


def extract_place_coordinates_from_url(url: str) -> tuple[float, float] | None:
    """Returns place coordinates encoded as !3d<lat>!4d<lon>."""
    if not url or not isinstance(url, str):
        return None
    return _coordinates_from_match(
        re.search(
            r"!3d(-?\d+(?:\.\d+)?)!4d(-?\d+(?:\.\d+)?)(?=[!/?&#]|$)",
            unquote(url),
        )
    )


def _coordinates_from_match(match: re.Match[str] | None) -> tuple[float, float] | None:
    if match:
        lat, lon = float(match.group(1)), float(match.group(2))
        if -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0:
            return lat, lon
    return None


_HEX_ID = r"0[xX][0-9a-fA-F]+:0[xX][0-9a-fA-F]+"
_PLACE_ID = r"ChIJ[A-Za-z0-9_-]+"


def _place_ids_from_url(url: str) -> dict[str, str]:
    if not isinstance(url, str):
        return {}
    decoded = unquote(url)
    ids: dict[str, str] = {}
    for kind, prefix, pattern in (
        ("hex", r"!\d+s|(?<![A-Za-z0-9_-])", _HEX_ID),
        ("place_id", r"!\d+s|\b(?:query_)?place_id[:=]", _PLACE_ID),
    ):
        match = re.search(
            rf"(?:{prefix})({pattern})(?=[!&?#/]|$)",
            decoded,
        )
        if match:
            value = match.group(1)
            ids[kind] = value.lower() if kind == "hex" else value
    return ids


def extract_place_identity_from_url(url: str) -> str | None:
    """Returns the most specific explicit place identifier in a place URL."""
    ids = _place_ids_from_url(url)
    return ids.get("place_id") or ids.get("hex")


def _ids_match(
    target_ids: dict[str, str], candidate_ids: dict[str, str]
) -> bool | None:
    common = target_ids.keys() & candidate_ids.keys()
    if not common:
        return None
    return all(target_ids[kind] == candidate_ids[kind] for kind in common)


def compare_place_url_ids(first_url: str, second_url: str) -> bool | None:
    """Compares explicit place identifiers, returning None without comparable IDs."""
    return _ids_match(_place_ids_from_url(first_url), _place_ids_from_url(second_url))


def preview_blob_matches_link(preview_blob: list, canonical_link: str) -> bool:
    """Checks comparable identifiers in a preview blob against a place URL."""
    ids: dict[str, str] = {}
    for value in (safe_get(preview_blob, 10), safe_get(preview_blob, 78)):
        if not isinstance(value, str):
            continue
        if re.fullmatch(_HEX_ID, value):
            ids["hex"] = value.lower()
        elif re.fullmatch(_PLACE_ID, value):
            ids["place_id"] = value
    return _ids_match(_place_ids_from_url(canonical_link), ids) is not False


def is_preview_response_for_link(response: Any, canonical_link: str) -> bool:
    """Validates whether an intercepted HTTP response corresponds to the place preview
    for a specific canonical link, avoiding race conditions and cross-place data leakage.
    Requires and validates either Hex ID (0x...:0x...), Google Place ID (ChIJ...),
    or matching place path slug when IDs are absent from the URL structure.
    Returns False if canonical_link lacks identifiable IDs and slug does not match.
    """
    if not canonical_link or not isinstance(canonical_link, str):
        return False

    raw_url = getattr(response, "url", "")
    status = getattr(response, "status", 0)
    if not isinstance(raw_url, str) or not isinstance(status, int):
        return False
    try:
        path = urlsplit(raw_url).path
    except ValueError:
        return False
    if not 200 <= status < 300 or not (
        path == "/maps/preview/place" or path.startswith("/maps/preview/place/")
    ):
        return False

    ids = _place_ids_from_url(canonical_link)
    if ids:
        return _ids_match(ids, _place_ids_from_url(raw_url)) is True

    # If canonical link has neither Hex ID nor Place ID, verify matching place slug
    slug_canonical = re.search(r"/maps/place/([^/@?]+)", unquote(canonical_link))
    slug_preview = re.search(r"/maps/preview/place/([^/@?]+)", unquote(path))
    if slug_canonical and slug_preview:
        s_c = slug_canonical.group(1).replace("+", " ").strip().lower()
        s_p = slug_preview.group(1).replace("+", " ").strip().lower()
        if s_c and s_p and s_c == s_p:
            return True

    return False


def _is_matching_feed_link(target_link: str, dom_link: str) -> bool:
    """Verifies whether a DOM card's href matches the expected target link,

    preventing cross-place data poisoning caused by Google Maps DOM recycling.
    """
    if not target_link or not dom_link:
        return False

    if target_link == dom_link:
        return True

    target_ids = _place_ids_from_url(target_link)
    dom_ids = _place_ids_from_url(dom_link)
    if target_ids or dom_ids:
        return _ids_match(target_ids, dom_ids) is True

    try:
        t_path = unquote(urlsplit(target_link).path).rstrip("/")
        d_path = unquote(urlsplit(dom_link).path).rstrip("/")
    except ValueError:
        return False
    return bool(
        t_path
        and d_path
        and t_path == d_path
        and t_path not in ("/maps/place", "/maps", "")
    )


def extract_feed_item_dom(
    card_html: str,
    link: str | None = None,
    coords: tuple[float, float] | None = None,
    fields: Sequence[str] | set[str] | None = None,
) -> dict[str, Any]:
    """Pure extraction function to parse place attributes from a Google Maps feed card DOM element.
    Used as an ultra-fast and resilient fallback rescue mechanism when SPA preview requests fail or time out.
    """
    if not card_html or not isinstance(card_html, str):
        return {}

    soup = BeautifulSoup(card_html, "html.parser")
    res: dict[str, Any] = {}

    a_tag = soup.select_one("a.hfpxzc[href]") or soup.select_one(
        "a[href*='/maps/place/']"
    )
    dom_href = str(a_tag["href"]).strip() if (a_tag and a_tag.get("href")) else None

    # 1. Target link validation & DOM recycling guard
    if not dom_href or (link and not _is_matching_feed_link(link, dom_href)):
        return {}
    target_link = link or dom_href

    res["link"] = target_link

    # 2. Place ID from link
    if place_id := extract_place_identity_from_url(target_link):
        res["place_id"] = place_id

    # 3. Name
    name = None
    name_el = soup.select_one("div.qBF1Pd, div[role='heading'], h3")
    if name_el and name_el.get_text(strip=True):
        name = name_el.get_text(strip=True)
    if not name and a_tag and a_tag.get("aria-label"):
        name = str(a_tag["aria-label"]).strip()
    if name:
        res["name"] = name

    # 4. Rating
    rating = None
    rating_el = soup.select_one("span.MW4etd")
    if rating_el and rating_el.get_text(strip=True):
        try:
            rating = float(rating_el.get_text(strip=True).replace(",", "."))
        except ValueError:
            pass
    if rating is None:
        star_el = soup.select_one("[aria-label*='star'], [aria-label*='sao']")
        if star_el:
            lbl = star_el.get("aria-label", "")
            m = re.search(
                r"([0-9]+(?:[.,][0-9]+)?)\s*(?:stars|sao|star)",
                str(lbl),
                re.IGNORECASE,
            )
            if m:
                try:
                    rating = float(m.group(1).replace(",", "."))
                except ValueError:
                    pass
    if rating is not None:
        res["rating"] = rating

    # 5. Reviews Count
    reviews_count = None
    rev_el = soup.select_one("span.UY7F9")
    if rev_el and rev_el.get_text(strip=True):
        m = re.search(r"([0-9,.]+)", rev_el.get_text(strip=True))
        if m:
            clean_num = m.group(1).replace(",", "").replace(".", "")
            try:
                reviews_count = int(clean_num)
            except ValueError:
                pass
    if reviews_count is None:
        rev_aria = soup.select_one("[aria-label*='reviews'], [aria-label*='đánh giá']")
        if rev_aria:
            lbl = rev_aria.get("aria-label", "")
            m = re.search(r"([0-9,.]+)\s*(?:reviews|đánh giá)", str(lbl), re.IGNORECASE)
            if m:
                clean_num = m.group(1).replace(",", "").replace(".", "")
                try:
                    reviews_count = int(clean_num)
                except ValueError:
                    pass
    if reviews_count is None:
        m = re.search(r"\(([0-9,.]+)\)", card_html)
        if m:
            clean_num = m.group(1).replace(",", "").replace(".", "")
            try:
                reviews_count = int(clean_num)
            except ValueError:
                pass
    if reviews_count is not None:
        res["reviews_count"] = reviews_count

    # 6. Categories and Address from div.W4Efsd
    categories = None
    cat_btn = soup.select_one("button[jsaction*='category']")
    if cat_btn and cat_btn.get_text(strip=True):
        categories = [cat_btn.get_text(strip=True)]

    w4_divs = soup.select("div.W4Efsd")
    address = None
    for w4 in w4_divs:
        line_text = w4.get_text(" · ", strip=True)
        tokens = [t.strip() for t in re.split(r"[·•]+", line_text) if t.strip()]
        for token in tokens:
            if re.fullmatch(r"[0-9]+(?:[.,][0-9]+)?", token) or re.fullmatch(
                r"\([0-9,.]+\)", token
            ):
                continue
            if any(
                s in token.lower()
                for s in [
                    "open",
                    "closed",
                    "đang mở",
                    "đã đóng",
                    "mở cửa",
                    "đóng cửa",
                ]
            ):
                continue
            if (
                not categories
                and len(token) < 40
                and not any(c.isdigit() for c in token)
                and "," not in token
            ):
                categories = [token]
                continue
            if not address and (
                len(token) > 8 or "," in token or any(c.isdigit() for c in token)
            ):
                address = token

    if categories:
        res["categories"] = categories

    if address:
        res["address"] = address
        comps = parse_address_string_fallback(address)
        if comps.get("city"):
            res["city"] = comps["city"]

    # 7. Coordinates
    if coords is not None:
        res["latitude"] = coords[0]
        res["longitude"] = coords[1]
    elif target_link:
        extracted_coords = extract_place_coordinates_from_url(target_link)
        if extracted_coords:
            res["latitude"] = extracted_coords[0]
            res["longitude"] = extracted_coords[1]

    # 8. Plus code
    pc_m = re.search(
        r"\b([2-9CFGHJMPQRVWX]{4}\+[2-9CFGHJMPQRVWX]{2,}(?:\s+[^,\n<]+)?)\b",
        card_html,
    )
    if pc_m:
        res["plus_code"] = pc_m.group(1).strip()

    if fields is not None:
        return {f: res.get(f) for f in fields if f in res and res.get(f) is not None}

    return {k: v for k, v in res.items() if v is not None}


def _extract_coordinates(
    pt: Point | tuple[float | None, float | None] | Sequence[float | None] | None,
) -> tuple[float, float] | None:
    """Safely extracts (latitude, longitude) floats from a Point or sequence.

    Returns None if pt is None, malformed, or has None for either dimension.
    """
    if pt is None:
        return None
    if isinstance(pt, Point):
        if (
            getattr(pt, "latitude", None) is None
            or getattr(pt, "longitude", None) is None
        ):
            return None
        try:
            return (float(pt.latitude), float(pt.longitude))
        except (ValueError, TypeError):
            return None
    if isinstance(pt, (tuple, list)) and len(pt) >= 2:
        lat, lon = pt[0], pt[1]
        if lat is None or lon is None:
            return None
        try:
            return (float(lat), float(lon))
        except (ValueError, TypeError):
            return None
    return None


def calculate_distance(
    origin: Point | tuple[float | None, float | None] | Sequence[float | None] | None,
    target: Point | tuple[float | None, float | None] | Sequence[float | None] | None,
) -> float:
    """Calculates the geodesic distance in meters between two points.

    If either origin or target is None or missing coordinate dimensions,
    returns float("inf") to prevent silent Null Island (0,0) fallback.
    """
    p1 = _extract_coordinates(origin)
    p2 = _extract_coordinates(target)
    if p1 is None or p2 is None:
        return float("inf")
    return float(geodesic(p1, p2).meters)


def is_within_range(
    origin: Point | tuple[float | None, float | None] | Sequence[float | None] | None,
    target: Point | tuple[float | None, float | None] | Sequence[float | None] | None,
    range_limit: float = DEFAULT_RANGE_LIMIT,
) -> bool:
    """Checks whether target is within range_limit meters from origin.

    Defaults to DEFAULT_RANGE_LIMIT (25,000 meters = 25 km).
    Returns False if either origin or target is None or missing coordinates.
    """
    if origin is None or target is None:
        return False
    return calculate_distance(origin, target) <= range_limit


def _get_flatten_column_type(col: str) -> pl.DataType | type[pl.DataType]:
    if col in _FLOAT_FIELDS:
        return pl.Float64
    if col in _COUNT_FIELDS:
        return pl.Int64
    if col in _TEXT_LIST_FIELDS:
        return pl.List(pl.String)
    if col == "is_claimed":
        return pl.Boolean
    return pl.String


def _format_flattened_rows(rows: list[dict[str, Any]]) -> pl.DataFrame:
    columns = dict.fromkeys(field for row in rows for field in row)
    typed_fields = (
        _TEXT_FIELDS
        | _FLOAT_FIELDS
        | _COUNT_FIELDS
        | _TEXT_LIST_FIELDS
        | {"is_claimed"}
    )
    overrides = {
        field: _get_flatten_column_type(field)
        for field in columns
        if field in typed_fields
    }
    try:
        return pl.from_dicts(rows, infer_schema_length=None, schema_overrides=overrides)
    except (pl.exceptions.PolarsError, TypeError, ValueError, OverflowError):
        # A caller's flattened metadata can mix shapes that Polars cannot unify.
        # Build the columns independently to preserve the other fields and rows.
        series: list[pl.Series] = []
        for field in columns:
            values = [row.get(field) for row in rows]
            try:
                column = pl.Series(
                    field, values, dtype=overrides.get(field), strict=False
                )
            except (pl.exceptions.PolarsError, TypeError, ValueError, OverflowError):
                column = pl.Series(
                    field,
                    [
                        json.dumps(value, ensure_ascii=False, default=str)
                        if value is not None
                        else None
                        for value in values
                    ],
                    dtype=pl.String,
                )
            series.append(column)
        return pl.DataFrame(series)


def format_places_dataframe(
    results: list[dict[str, Any]],
    flatten: bool = False,
    fields: Sequence[str] | set[str] | None = None,
    include_query: bool = False,
) -> pl.DataFrame:
    """Formats scraped places data into a Polars DataFrame.

    When flatten=False (default), keeps 11 default top-level columns
    ('name', 'place_id', 'latitude', 'longitude', 'address', 'link',
    'categories', 'rating', 'reviews_count', 'plus_code', 'city')
    and bundles all other metadata into a 'details' JSON string column.

    When flatten=True, outputs all fields as flattened columns at top-level.

    Guarantees 'reviews_count' column consistently retains pl.Int64 datatype
    across both flatten=True and flatten=False modes.

    Args:
        results (list[dict[str, Any]]): List of scraped place dictionaries.
        flatten (bool, optional): Whether to flatten all fields into individual columns.
        fields (Sequence[str] | set[str] | None, optional): Specific fields to include.
        include_query (bool, optional): Keep the originating query as a top-level
            string column even when fields omits it. Used by scrape_google_maps.

    Returns:
        pl.DataFrame: Formatted Polars DataFrame.
    """
    results = [
        {field: _normalize_field_value(field, value) for field, value in item.items()}
        for item in results
    ]

    def with_query(row: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
        if include_query:
            row["query"] = item.get("query")
        return row

    if flatten:
        if not results:
            if fields is not None:
                schema = {f: _get_flatten_column_type(f) for f in fields}
                if include_query:
                    schema["query"] = pl.String
                return pl.DataFrame(schema=schema)
            if include_query:
                return pl.DataFrame(schema={"query": pl.String})
            return pl.DataFrame()

        if fields is not None:
            filtered_results = [
                with_query({k: item[k] for k in fields if k in item}, item)
                for item in results
            ]
            df = _format_flattened_rows(filtered_results)
        else:
            df = _format_flattened_rows(results)
    elif fields is None:
        if not results:
            schema: dict[str, Any] = {
                c: _get_flatten_column_type(c) for c in DEFAULT_FLATTEN_COLUMNS
            }
            schema["details"] = pl.String
            if include_query:
                schema["query"] = pl.String
            return pl.DataFrame(schema=schema)

        formatted_rows: list[dict[str, Any]] = []
        for item in results:
            row: dict[str, Any] = {c: item.get(c) for c in DEFAULT_FLATTEN_COLUMNS}
            details_dict = {
                k: v
                for k, v in item.items()
                if k not in DEFAULT_FLATTEN_COLUMNS
                and (not include_query or k != "query")
                and v is not None
            }
            row["details"] = json.dumps(details_dict, ensure_ascii=False, default=str)
            formatted_rows.append(with_query(row, item))
        schema_overrides = {
            c: _get_flatten_column_type(c) for c in DEFAULT_FLATTEN_COLUMNS
        }
        if include_query:
            schema_overrides["query"] = pl.String
        df = pl.from_dicts(
            formatted_rows,
            infer_schema_length=None,
            schema_overrides=schema_overrides,
        )
    else:
        # fields is not None and flatten is False
        top_cols = [f for f in fields if f in DEFAULT_FLATTEN_COLUMNS]
        other_cols = [
            f
            for f in fields
            if f not in DEFAULT_FLATTEN_COLUMNS and (not include_query or f != "query")
        ]
        if not results:
            schema_dict: dict[str, Any] = {
                c: _get_flatten_column_type(c) for c in top_cols
            }
            if other_cols:
                schema_dict["details"] = pl.String
            if include_query:
                schema_dict["query"] = pl.String
            return pl.DataFrame(schema=schema_dict)

        formatted_rows = []
        for item in results:
            row = {c: item.get(c) for c in top_cols}
            if other_cols:
                details_dict = {
                    k: item[k] for k in other_cols if k in item and item[k] is not None
                }
                row["details"] = json.dumps(
                    details_dict, ensure_ascii=False, default=str
                )
            formatted_rows.append(with_query(row, item))

        overrides = {c: _get_flatten_column_type(c) for c in top_cols}
        if include_query:
            overrides["query"] = pl.String

        df = pl.from_dicts(
            formatted_rows,
            infer_schema_length=None,
            schema_overrides=overrides or None,
        )

    if "reviews_count" in df.columns and df["reviews_count"].dtype != pl.Int64:
        df = df.with_columns(pl.col("reviews_count").cast(pl.Int64))
    return df
