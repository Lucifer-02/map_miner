import json
import logging
import re
import unicodedata
from collections.abc import Sequence
from typing import Any

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


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
    return safe_get(data, 78) or safe_get(data, 10)


def get_latitude(data: list) -> float | None:
    """Extracts latitude coordinate."""
    lat = safe_get(data, 9, 2)
    if lat is not None:
        return lat
    return safe_get(data, 208, 0, 2)


def get_longitude(data: list) -> float | None:
    """Extracts longitude coordinate."""
    lon = safe_get(data, 9, 3)
    if lon is not None:
        return lon
    return safe_get(data, 208, 0, 3)


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
    val = safe_get(data, 4, 7)
    if val is not None:
        try:
            return float(val)
        except (ValueError, TypeError):
            pass
    return None


def get_reviews_count(data: list) -> int | None:
    """Extracts the total number of reviews."""
    val = safe_get(data, 4, 8)
    if val is not None:
        try:
            return int(val)
        except (ValueError, TypeError):
            pass
    rev_str = safe_get(data, 4, 3, 1)
    if isinstance(rev_str, str):
        m = re.search(r"([0-9,]+)", rev_str)
        if m:
            return int(m.group(1).replace(",", ""))
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
    if isinstance(lvl, int) and lvl > 0:
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
    candidates = (safe_get(data, 72, 0) or []) + (safe_get(data, 51, 0) or [])
    for p in candidates:
        url = safe_get(p, 6, 0)
        if url and isinstance(url, str) and url not in photos:
            photos.append(url)
    return photos if photos else None


def get_is_claimed(data: list) -> bool:
    """Checks whether the business is claimed by its owner."""
    return safe_get(data, 57, 1) is not None or safe_get(data, 57, 2) is not None


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

    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        dom_data["name"] = h1.get_text(strip=True)

    # Plus code
    pc_el = soup.select_one("[data-item-id='oloc'], [aria-label*='Plus code:']")
    if pc_el:
        label = _attr(pc_el, "aria-label")
        m = re.sub(r"^Plus code:\s*", "", label, flags=re.IGNORECASE).strip()
        dom_data["plus_code"] = m or pc_el.get_text(strip=True)

    # Address
    addr_el = soup.select_one("[data-item-id='address'], [aria-label^='Address:']")
    if addr_el:
        label = _attr(addr_el, "aria-label")
        m = re.sub(r"^Address:\s*", "", label, flags=re.IGNORECASE).strip()
        dom_data["address"] = m or addr_el.get_text(strip=True)

    # Phone
    phone_el = soup.select_one("[data-item-id^='phone:'], [aria-label^='Phone:']")
    if phone_el:
        label = _attr(phone_el, "aria-label")
        m = re.sub(r"^Phone:\s*", "", label, flags=re.IGNORECASE).strip()
        dom_data["phone"] = m or phone_el.get_text(strip=True)

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
    if wanted is None or not wanted.isdisjoint(
        {"street", "sublocality", "district", "city", "postal_code"}
    ):
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
    return {k: v for k, v in candidate_data.items() if v is not None and is_wanted(k)}


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

    # 2. Automatically parse rendered DOM from HTML if dom_data was not explicitly supplied
    if dom_data is None and html_content:
        dom_data = parse_dom_from_html(html_content)

    place_details: dict[str, Any] = {}
    wanted = set(fields) if fields is not None else None

    def is_wanted(f: str) -> bool:
        return wanted is None or f in wanted

    # 1. First priority: rich preview blob from /maps/preview/place
    if preview_blob and isinstance(preview_blob, list):
        place_details = _extract_from_blob(preview_blob, wanted)

    # 2. Second priority: extract from HTML (APP_INITIALIZATION_STATE) if fields are missing
    if html_content and (not place_details or len(place_details) < 4):
        json_str = extract_initial_json(html_content)
        if json_str:
            data_blob = parse_json_data(json_str)
            if data_blob:
                html_details = _extract_from_blob(data_blob, wanted)
                for k, v in html_details.items():
                    if v is not None and k not in place_details:
                        place_details[k] = v

    # 3. Third priority: DOM data fallback for missing fields
    if dom_data and isinstance(dom_data, dict):
        for k, v in dom_data.items():
            if (
                v is not None
                and is_wanted(k)
                and (k not in place_details or place_details[k] is None)
            ):
                place_details[k] = v

    # 4. Fallback heuristic for address components if full address is available but components are missing
    address_comp_keys = ["street", "sublocality", "district", "city", "postal_code"]
    if "address" in place_details and any(
        is_wanted(k) and k not in place_details for k in address_comp_keys
    ):
        fb_comps = parse_address_string_fallback(place_details["address"])
        for k, v in fb_comps.items():
            if v and is_wanted(k) and k not in place_details:
                place_details[k] = v

    if not place_details:
        return None

    # If specific fields are requested, return dict preserving the requested order
    if fields is not None:
        return {f: place_details.get(f) for f in fields if f != "link"}

    return place_details
