import json
import logging
import re
from typing import Dict

logger = logging.getLogger("root.scraper")


def safe_get(data, *keys):
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
                    # logger.debug(f"Index {key} out of bounds or invalid for list.")
                    return None
            elif isinstance(current, dict):
                if key in current:
                    current = current[key]
                else:
                    # logger.debug(f"Key {key} not found in dict.")
                    return None
            else:
                # logger.debug(f"Cannot access key {key} on non-dict/list item: {type(current)}")
                return None
        except (IndexError, TypeError, KeyError):
            # logger.debug(f"Error accessing key {key}: {e}")
            return None
    return current


def extract_initial_json(html_content: str):
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
            else:
                logger.debug("Extracted content doesn't look like valid JSON start.")
                return None
        else:
            logger.debug("APP_INITIALIZATION_STATE pattern not found.")
            return None
    except Exception as e:
        logger.debug(f"Error extracting JSON string: {e}")
        return None


def parse_json_data(json_str):
    """
    Parses the extracted JSON string, handling the nested JSON string if present.
    Returns a tuple of (data_blob, format_tag) or (None, None) if parsing fails.
    format_tag is "rich" for the usual structure and "lean" for a slimmed-down
    variant observed in some pages.
    """
    if not json_str:
        return None, None
    try:
        initial_data = json.loads(json_str)
        data_blob = None

        # Check the initial heuristic path [3][6]
        if (
            isinstance(initial_data, list)
            and len(initial_data) > 3
            and isinstance(initial_data[3], list)
            and len(initial_data[3]) > 6
        ):
            data_blob_or_str = initial_data[3][6]

            # Case 1: It's already the list we expect (older format?)
            if isinstance(data_blob_or_str, list):
                logger.debug(
                    "Found expected list structure directly at initial_data[3][6]."
                )
                data_blob = data_blob_or_str

            # Case 2: It's the string containing the actual JSON
            elif isinstance(data_blob_or_str, str) and data_blob_or_str.startswith(
                ")]}'\n"
            ):
                logger.debug(
                    "Found string at initial_data[3][6], attempting to parse inner JSON."
                )
                try:
                    json_str_inner = data_blob_or_str.split(")]}'\n", 1)[1]
                    actual_data = json.loads(json_str_inner)

                    # Check if the parsed inner data is a list and has the expected sub-structure at index 6
                    if isinstance(actual_data, list) and len(actual_data) > 6:
                        potential_data_blob = safe_get(actual_data, 6)
                        if isinstance(potential_data_blob, list):
                            logger.debug("Returning data blob found at actual_data[6].")
                            data_blob = potential_data_blob  # This is the main data structure
                        else:
                            logger.debug(
                                f"Data at actual_data[6] is not a list, but {type(potential_data_blob)}."
                            )
                            data_blob = None  # Structure mismatch within inner data
                    else:
                        logger.debug(
                            f"Parsed inner JSON is not a list or too short (len <= 6), type: {type(actual_data)}."
                        )
                        data_blob = None  # Inner JSON structure not as expected

                except json.JSONDecodeError as e_inner:
                    logger.debug(f"Error decoding inner JSON string: {e_inner}")
                    data_blob = None
                except Exception as e_inner_general:
                    logger.debug(
                        f"Unexpected error processing inner JSON string: {e_inner_general}"
                    )
                    data_blob = None

            # Case 3: Data at [3][6] is neither a list nor the expected string
            else:
                logger.debug(
                    f"Parsed JSON structure unexpected at [3][6]. Expected list or prefixed JSON string, got {type(data_blob_or_str)}."
                )
                data_blob = None  # Unexpected structure at [3][6]

        # Case 4: Initial path [3][6] itself wasn't valid
        else:
            logger.debug(
                f"Initial JSON structure not as expected (list[3][6] path not valid). Type: {type(initial_data)}"
            )
            data_blob = None  # Initial structure invalid

    except json.JSONDecodeError as e:
        logger.debug(f"Error decoding initial JSON: {e}")
        data_blob = None
    except Exception as e:
        logger.debug(f"Unexpected error parsing JSON data: {e}")
        data_blob = None

    if data_blob is not None:
        return data_blob, "rich"

    # --- Fallback path: some pages store the data at [3][5] instead of [3][6] ---
    try:
        alt_blob = safe_get(initial_data, 3, 5)
        if isinstance(alt_blob, str) and alt_blob.startswith(")]}'\n"):
            alt_parsed = json.loads(alt_blob.split(")]}'\n", 1)[1])
            if isinstance(alt_parsed, list) and len(alt_parsed) > 0:
                first_entry = alt_parsed[0]
                if isinstance(first_entry, list):
                    logger.debug("Using fallback data blob at initial_data[3][5].")
                    return first_entry, "lean"
        logger.debug("Fallback path [3][5] did not yield a usable list structure.")
    except json.JSONDecodeError as e:
        logger.debug(f"Error decoding fallback JSON string: {e}")
    except Exception as e:
        logger.debug(f"Unexpected error in fallback parsing: {e}")

    return None, None


# --- Field Extraction Functions (Indices relative to the data_blob returned by parse_json_data) ---


def get_main_name(data):
    """Extracts the main name of the place."""
    # Index relative to the data_blob returned by parse_json_data
    # Confirmed via debug_inner_data.json: data_blob = actual_data[6], name = data_blob[11]
    return safe_get(data, 11)


def get_place_id(data):
    """Extracts the Google Place ID."""
    return safe_get(data, 10)  # Updated index


def get_latitude(data):
    lat = safe_get(data, 9, 2)
    if lat is not None:
        return lat
    return None


def get_longitude(data):
    lon = safe_get(data, 9, 3)
    if lon is not None:
        return lon
    return None


def get_complete_address(data):
    """Extracts structured address components and joins them."""
    address_parts = safe_get(data, 2)  # Updated index
    if isinstance(address_parts, list):
        formatted = ", ".join(filter(None, address_parts))
        return formatted if formatted else None
    return None


def get_rating(data):
    """Extracts the average star rating."""
    return safe_get(data, 4, 7)


def get_reviews_count(data):
    """Extracts the total number of reviews."""
    return safe_get(data, 4, 8)


def get_website(data):
    """Extracts the primary website link."""
    # Index based on debug_inner_data.json structure relative to data_blob (actual_data[6])
    return safe_get(data, 7, 0)


def _find_phone_recursively(data_structure):
    """
    Recursively searches a nested list/dict structure for a list containing
    the phone icon URL followed by the phone number string.
    """
    if isinstance(data_structure, list):
        # Check if this list matches the pattern [icon_url, phone_string, ...]
        if (
            len(data_structure) >= 2
            and isinstance(data_structure[0], str)
            and "call_googblue" in data_structure[0]
            and isinstance(data_structure[1], str)
        ):
            # Found the pattern, assume data_structure[1] is the phone number
            phone_number_str = data_structure[1]
            standardized_number = re.sub(r"\D", "", phone_number_str)
            if standardized_number:
                # logger.debug(f"Debug: Found phone via recursive search: {standardized_number}")
                return standardized_number

        # If not the target list, recurse into list elements
        for item in data_structure:
            found_phone = _find_phone_recursively(item)
            if found_phone:
                return found_phone

    elif isinstance(data_structure, dict):
        # Recurse into dictionary values
        for key, value in data_structure.items():
            found_phone = _find_phone_recursively(value)
            if found_phone:
                return found_phone

    # Base case: not a list/dict or pattern not found in this branch
    return None


def get_phone_number(data_blob):
    """
    Extracts and standardizes the primary phone number by recursively searching
    the data_blob for the phone icon pattern.
    """
    # data_blob is the main list structure (e.g., actual_data[6])
    found_phone = _find_phone_recursively(data_blob)
    if found_phone:
        return found_phone
    else:
        # logger.debug("Debug: Phone number pattern not found in data_blob.")
        return None


def get_categories(data):
    """Extracts the list of categories/types."""
    return safe_get(data, 13)


# --- Fallback (lean structure) extraction helpers ---


def get_main_name_lean(data):
    return safe_get(data, 1)


def get_place_id_lean(data):
    return safe_get(data, 0)


def get_latitude_lean(data):
    return safe_get(data, 3, 2)


def get_longitude_lean(data):
    return safe_get(data, 3, 3)


def get_address_lean(initial_data):
    """Extract address from summary string in APP_INITIALIZATION_STATE[9][0]."""
    summary = safe_get(initial_data, 9, 0)
    if isinstance(summary, str) and "·" in summary:
        parts = summary.split("·", 1)
        if len(parts) > 1:
            return parts[1].strip()
    return None


def get_categories_lean(initial_data):
    """
    Extract categories from summary string or gcid marker.
    """
    summary = safe_get(initial_data, 9, 1)
    if isinstance(summary, str) and "·" in summary:
        cat = summary.split("·", 1)[1].strip()
        if cat:
            return [cat]

    gcid = safe_get(initial_data, 2, 3, 0, 13, 0, 14)
    if isinstance(gcid, str) and gcid.startswith("gcid:"):
        cat = gcid.split(":", 1)[1].replace("_", " ").title()
        return [cat]

    return None


def get_thumbnail_lean(initial_data):
    thumb = safe_get(initial_data, 9, 2)
    return thumb if isinstance(thumb, str) else None


def parse_rating_reviews_from_html(html_content: str):
    """
    Fallback parsing for rating and review count directly from HTML attributes.
    """
    rating = None
    reviews = None

    m_rating = re.search(r'aria-label="([0-9\\.]+) stars', html_content)
    if m_rating:
        try:
            rating = float(m_rating.group(1))
        except (TypeError, ValueError):
            rating = None

    m_reviews = re.search(r'aria-label="([0-9,]+) reviews', html_content)
    if m_reviews:
        try:
            reviews = int(m_reviews.group(1).replace(",", ""))
        except (TypeError, ValueError):
            reviews = None

    return rating, reviews


def get_thumbnail(data):
    """Extracts the main thumbnail image URL."""
    # This path might still be relative to the old structure, needs verification
    # If data_blob is the list starting at actual_data[6], this index is likely wrong.
    # We need to find the thumbnail within the new structure from debug_inner_data.json
    # For now, returning None until verified.
    # return safe_get(data, 72, 0, 1, 6, 0) # Placeholder index - LIKELY WRONG
    # Tentative guess based on debug_inner_data structure (might be in a sublist like [14][0][0][6][0]?)
    return safe_get(data, 14, 0, 0, 6, 0)  # Tentative guess


# Add more extraction functions here as needed, using the indices
# from omkarcloud/src/extract_data.py as a reference, BUT VERIFYING against debug_inner_data.json


def extract_place_data(html_content: str) -> Dict | None:
    """
    High-level function to orchestrate extraction from HTML content.
    """
    json_str = extract_initial_json(html_content)
    if not json_str:
        logger.debug("Failed to extract JSON string from HTML.")
        return None

    data_blob, data_format = parse_json_data(json_str)
    initial_data = None
    try:
        initial_data = json.loads(json_str)
    except Exception:
        initial_data = None

    if not data_blob:
        logger.debug("Failed to parse JSON data or find expected structure.")
        return None

    if data_format == "rich":
        # Now extract individual fields using the helper functions
        place_details = {
            "name": get_main_name(data_blob),
            "place_id": get_place_id(data_blob),
            "latitude": get_latitude(data_blob),
            "longitude": get_longitude(data_blob),
            "address": get_complete_address(data_blob),
            # "rating": get_rating(data_blob),
            # "reviews_count": get_reviews_count(data_blob),
            "categories": get_categories(data_blob),
            # "website": get_website(data_blob),
            # "phone": get_phone_number(data_blob), # Needs index verification
            # "thumbnail": get_thumbnail(data_blob), # Needs index verification
            # Add other fields as needed
        }
    elif data_format == "lean":
        # Minimal fallback extraction for lean structure
        place_details = {
            "name": get_main_name_lean(data_blob),
            "place_id": get_place_id_lean(data_blob),
            "latitude": get_latitude_lean(data_blob),
            "longitude": get_longitude_lean(data_blob),
        }

        if initial_data:
            address = get_address_lean(initial_data)
            if address:
                place_details["address"] = address

            categories = get_categories_lean(initial_data)
            if categories:
                place_details["categories"] = categories
    else:
        logger.debug("Unknown data format encountered during extraction.")
        return None

    # Filter out None values if desired
    place_details = {k: v for k, v in place_details.items() if v is not None}

    return place_details if place_details else None
