import os
import sys
import json
import time
import re
import argparse
import datetime

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials

load_dotenv()

GOOGLE_PLACES_API_KEY = os.getenv("GOOGLE_PLACES_API_KEY")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    )
}

PLACES_TEXT_SEARCH_URL = "https://maps.googleapis.com/maps/api/place/textsearch/json"
PLACES_DETAILS_URL = "https://maps.googleapis.com/maps/api/place/details/json"

SHEET_ID = "1UEKd9ItlMcm5WwTw0D69_MRTNJBOgkMAtx66qCS-Dfs"
SHEET_TAB = "PROSPECTS"
SHEET_URL = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SERVICE_ACCOUNT_FILE = os.path.join(SCRIPT_DIR, "service-account.json")
GSPREAD_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

PROSPECT_ID_PATTERN = re.compile(r'^P(\d+)$')

EXCLUDED_FB_PATTERNS = [
    "facebook.com/sharer",
    "facebook.com/login",
    "facebook.com/dialog",
    "facebook.com/plugins",
    "facebook.com/tr",
]

GENERIC_PLACE_TYPES = {
    "point_of_interest",
    "establishment",
}

# Social/directory URLs that businesses sometimes put in the website field
SOCIAL_DIRECTORY_DOMAINS = [
    "facebook.com/",
    "instagram.com/",
    "linkedin.com/",
    "yelp.com/",
    "thumbtack.com/",
    "nextdoor.com/",
]

AGENCY_FALSE_POSITIVE_PHRASES = [
    "cookies",
    "website uses",
    "privacy",
    "terms",
    "rights reserved",
    "all rights",
    "google",
    "wordpress",
    "the owner",
    "the company",
]

AGENCY_PATTERN = re.compile(
    r'(?:designed|built|powered|website|created|developed)\s+by\s+'
    r'([A-Za-z0-9][A-Za-z0-9\s&.,\-]{1,58}?)(?:\s*[\.\|<\n]|$)',
    re.IGNORECASE,
)

COPYRIGHT_PATTERN = re.compile(
    r'(?:©|\(c\)|copyright)\s*[\-–]?\s*(?:\d{4}\s*[\-–]\s*)?(\d{4})',
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Google Places helpers
# ---------------------------------------------------------------------------

def search_places(query, max_results):
    results = []
    params = {"query": query, "key": GOOGLE_PLACES_API_KEY}

    while len(results) < max_results:
        try:
            resp = requests.get(PLACES_TEXT_SEARCH_URL, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"  ⚠️  Places Text Search request failed: {e}")
            break

        status = data.get("status")
        if status == "ZERO_RESULTS":
            break
        if status != "OK":
            print(f"  ⚠️  Places API error: {status} — {data.get('error_message', '')}")
            break

        results.extend(data.get("results", []))

        next_page_token = data.get("next_page_token")
        if not next_page_token or len(results) >= max_results:
            break

        time.sleep(2)  # Google requires a short pause before next-page token is valid
        params = {"pagetoken": next_page_token, "key": GOOGLE_PLACES_API_KEY}

    return results[:max_results]


def get_place_details(place_id):
    fields = (
        "name,formatted_address,formatted_phone_number,website,"
        "rating,user_ratings_total,types,business_status,url,"
        "opening_hours,photos"
    )
    params = {"place_id": place_id, "fields": fields, "key": GOOGLE_PLACES_API_KEY}

    try:
        resp = requests.get(PLACES_DETAILS_URL, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"  ⚠️  Place Details request failed: {e}")
        return None

    if data.get("status") != "OK":
        return None

    return data.get("result", {})


# ---------------------------------------------------------------------------
# Website scraping helpers
# ---------------------------------------------------------------------------

def fetch_website_data(url):
    result = {
        "facebook_url": None,
        "detected_agency": None,
        "copyright_year": None,
        "is_https": url.startswith("https://"),
        "fetch_error": None,
    }

    try:
        resp = requests.get(url, headers=HEADERS, timeout=10, allow_redirects=True)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # Facebook URL — search all <a> tags
        for tag in soup.find_all("a", href=True):
            href = tag["href"]
            if "facebook.com/" in href:
                if not any(excl in href for excl in EXCLUDED_FB_PATTERNS):
                    if href.startswith("//"):
                        href = "https:" + href
                    result["facebook_url"] = href
                    break

        # Agency detection — check footer first, fall back to full page
        footer = soup.find("footer") or soup
        footer_text = footer.get_text(separator=" ", strip=True)
        match = AGENCY_PATTERN.search(footer_text)
        if match:
            candidate = match.group(1).strip().rstrip(".,")
            candidate_lower = candidate.lower()
            is_false_positive = (
                len(candidate) > 40
                or not candidate[0].isupper()
                or candidate_lower in ("us", "our", "the", "a")
                or any(phrase in candidate_lower for phrase in AGENCY_FALSE_POSITIVE_PHRASES)
            )
            if not is_false_positive:
                result["detected_agency"] = candidate

        # Copyright year — take the latest year found
        full_text = soup.get_text(separator=" ", strip=True)
        years = COPYRIGHT_PATTERN.findall(full_text)
        if years:
            result["copyright_year"] = max(years)

    except requests.exceptions.Timeout:
        result["fetch_error"] = "Timeout"
    except requests.exceptions.TooManyRedirects:
        result["fetch_error"] = "Too many redirects"
    except requests.exceptions.SSLError:
        result["fetch_error"] = "SSL error"
    except requests.exceptions.ConnectionError:
        result["fetch_error"] = "Connection error"
    except Exception as e:
        result["fetch_error"] = str(e)[:100]

    return result


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def calculate_score(details, website_data):
    """
    Returns (score: int, reason: str).
    Red flags are evaluated first; then Score 5 → 4 → 3 → 2 in order.
    """
    business_status = details.get("business_status", "")
    website = details.get("website", "")
    reviews = details.get("user_ratings_total") or 0
    photos_count = len(details.get("photos") or [])

    agency = website_data.get("detected_agency")
    copyright_year = website_data.get("copyright_year")
    facebook_only = website_data.get("facebook_only", False)

    has_online_presence = bool(website) or facebook_only
    presence_label = "Facebook-only (no website)" if facebook_only else "has website"

    # --- Red Flags → Score 1 ---
    if business_status and business_status != "OPERATIONAL":
        label = "permanently closed" if business_status == "CLOSED_PERMANENTLY" else business_status
        return 1, f"Red flag: business status is {label}"

    if not has_online_presence:
        return 1, "Red flag: no website found"

    if agency:
        return 1, f"Red flag: existing agency detected ({agency})"

    if reviews < 5:
        return 1, f"Red flag: fewer than 5 reviews ({reviews})"

    # --- Score 5: Dream fit — requires a real standalone website ---
    if reviews > 100 and photos_count >= 5 and website and not facebook_only:
        return 5, f"Dream fit — {reviews} reviews, {photos_count} photos, no agency"

    # --- Score 4: Strong fit ---
    # Facebook-only businesses cap here even with high reviews/photos
    if reviews >= 50 and photos_count >= 3 and has_online_presence:
        return 4, f"Strong fit — {reviews} reviews, {photos_count} photos, {presence_label}"

    # --- Score 2: Weak signals (checked before Score 3 so old sites don't drift up) ---
    if reviews < 20:
        return 2, f"Weak — under 20 reviews ({reviews})"

    if copyright_year and int(copyright_year) < (2026 - 3):
        return 2, f"Weak — copyright year {copyright_year} suggests dormant site"

    # --- Score 3: Potential ---
    # Covers 20-49 reviews, OR 50+ reviews with fewer than 3 photos
    return 3, f"Potential — {reviews} reviews, {presence_label}"


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def extract_city(formatted_address):
    """
    Handles "Street, City, ST ZIP, USA" and shorter variants.
    City is reliably the third-to-last comma-separated segment.
    """
    if not formatted_address:
        return ""
    parts = [p.strip() for p in formatted_address.split(",")]
    if len(parts) >= 3:
        return parts[-3]
    if len(parts) == 2:
        return parts[0]
    return ""


def build_notes(details, website_data, score_reason):
    address = details.get("formatted_address") or "N/A"
    phone = details.get("formatted_phone_number") or "N/A"
    rating = details.get("rating") or "N/A"
    reviews = details.get("user_ratings_total") or 0
    photos_count = len(details.get("photos") or [])
    status = details.get("business_status") or "N/A"
    all_types = [t for t in (details.get("types") or []) if t not in GENERIC_PLACE_TYPES]

    copyright_year = website_data.get("copyright_year") or "N/A"
    is_https = "Yes" if website_data.get("is_https") else "No"
    agency = website_data.get("detected_agency") or "None"
    fetch_error = website_data.get("fetch_error")
    facebook_only = website_data.get("facebook_only", False)

    lines = [
        f"Address: {address}",
        f"Phone: {phone}",
        f"Rating: {rating} ({reviews} reviews)",
        f"Categories: {', '.join(all_types) if all_types else 'N/A'}",
        f"Photos (GBP): {photos_count}",
        f"Business Status: {status}",
    ]

    if facebook_only:
        lines.append("Online presence: Facebook page only (no standalone website)")
    else:
        lines.append(f"Copyright Year: {copyright_year}")
        lines.append(f"HTTPS: {is_https}")
        lines.append(f"Detected Agency: {agency}")

    if fetch_error:
        lines.append(f"Website Fetch Error: {fetch_error}")
    lines.append(f"Score Reason: {score_reason}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Google Sheets upload
# ---------------------------------------------------------------------------

def upload_to_sheet(prospects):
    """
    Appends new prospects to the PROSPECTS tab, skipping duplicates by Business_Name.
    Returns (uploaded_count, skipped_count) on success, raises on failure.
    """
    if not os.path.exists(SERVICE_ACCOUNT_FILE):
        raise FileNotFoundError(
            f"service-account.json not found at {SERVICE_ACCOUNT_FILE}"
        )

    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=GSPREAD_SCOPES)
    gc = gspread.authorize(creds)
    sheet = gc.open_by_key(SHEET_ID).worksheet(SHEET_TAB)

    all_values = sheet.get_all_values()
    data_rows = all_values[1:] if len(all_values) > 1 else []  # skip header row

    # Find the highest existing Prospect_ID
    existing_numbers = []
    for row in data_rows:
        if row and row[0]:
            m = PROSPECT_ID_PATTERN.match(row[0].strip())
            if m:
                existing_numbers.append(int(m.group(1)))
    next_num = max(existing_numbers) + 1 if existing_numbers else 1

    # Build set of existing business names for duplicate detection (case-insensitive)
    existing_names = {
        row[1].strip().lower()
        for row in data_rows
        if len(row) > 1 and row[1].strip()
    }

    today = datetime.date.today().strftime("%Y-%m-%d")
    rows_to_append = []
    skipped = 0

    for prospect in prospects:
        name_key = prospect["Business_Name"].strip().lower()
        if name_key in existing_names:
            skipped += 1
            continue

        prospect_id = f"P{next_num:03d}"
        next_num += 1

        rows_to_append.append([
            prospect_id,                      # A: Prospect_ID
            prospect["Business_Name"],        # B
            prospect["Owner_Name"],           # C
            prospect["City"],                 # D
            prospect["State"],                # E
            prospect["Website_URL"],          # F
            prospect["Facebook_URL"],         # G
            prospect["Google_Profile_URL"],   # H
            prospect["Prospect_Score"],       # I
            "Cold",                           # J: Temperature
            prospect["Pain_Points_Found"],    # K
            "",                               # L: Pain_Points_Delivered
            "",                               # M: Last_Touch_Date
            "",                               # N: Next_Touch_Due
            0,                                # O: Touch_Count
            "New",                            # P: Status
            "Katherine",                      # Q: Profile_Used
            today,                            # R: Date_Added
            prospect["Notes"],                # S
            "",                               # T: Draft_Message_1
            "",                               # U: Pain_Point_1
            "",                               # V: Sent_1
            "",                               # W: Draft_Message_2
            "",                               # X: Pain_Point_2
            "",                               # Y: Sent_2
        ])

    if rows_to_append:
        sheet.append_rows(rows_to_append, value_input_option="USER_ENTERED")

    return len(rows_to_append), skipped


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Green-industry prospect scraper — Google Places → Sheets + JSON"
    )
    parser.add_argument("query", help='e.g. "lawn care companies in Tampa FL"')
    parser.add_argument("--max", type=int, default=20, help="Max results (default 20)")
    parser.add_argument(
        "--output", default="prospects_output.json", help="Output JSON file"
    )
    parser.add_argument(
        "--skip-sheet", action="store_true", help="Skip Google Sheets upload (useful for testing)"
    )
    args = parser.parse_args()

    if not GOOGLE_PLACES_API_KEY:
        print("❌ GOOGLE_PLACES_API_KEY not found in .env file. Aborting.")
        sys.exit(1)

    print(f"🔍 Searching Google Places: {args.query}")
    places = search_places(args.query, args.max)
    print(f"✅ Found {len(places)} businesses\n")

    prospects = []
    score_counts = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
    facebook_found = 0
    facebook_tbd = 0

    for i, place in enumerate(places, 1):
        place_id = place.get("place_id")
        name = place.get("name", "Unknown")

        print(f"📍 Processing {i}/{len(places)}: {name}")

        time.sleep(1)
        details = get_place_details(place_id)
        if not details:
            print("  ⚠️  Could not fetch details — skipping\n")
            continue

        raw_website = details.get("website", "")

        # Detect social/directory URLs placed in the website field
        is_social_url = raw_website and any(d in raw_website for d in SOCIAL_DIRECTORY_DOMAINS)
        is_fb_as_website = raw_website and "facebook.com/" in raw_website

        if is_social_url:
            website = ""  # treat as no real website for scoring/output
            prefilled_fb = raw_website if is_fb_as_website else None
            print(f"  → Website: social/directory URL only ({raw_website})")
        else:
            website = raw_website
            prefilled_fb = None
            print(f"  → Website: {'yes (' + website + ')' if website else 'no'}")

        website_data = {
            "facebook_url": prefilled_fb,
            "detected_agency": None,
            "copyright_year": None,
            "is_https": False,
            "fetch_error": None,
            "facebook_only": is_social_url,
        }

        if website:
            time.sleep(2)
            website_data.update(fetch_website_data(website))

            fb = website_data.get("facebook_url")
            print(f"  → Facebook URL: {'found (' + fb + ')' if fb else 'not found'}")

            ag = website_data.get("detected_agency")
            print(f"  → Detected agency: {'yes (' + ag + ')' if ag else 'no'}")
        elif is_social_url:
            fb = website_data.get("facebook_url")
            print(f"  → Facebook URL: {'used from website field (' + fb + ')' if fb else 'social URL (non-Facebook)'}")
            print("  → Detected agency: skipped (no standalone website)")
        else:
            print("  → Facebook URL: skipped (no website)")
            print("  → Detected agency: skipped (no website)")

        score, score_reason = calculate_score(details, website_data)
        print(f"  → Score: {score}/5\n")

        score_counts[score] += 1

        fb_url = website_data.get("facebook_url")
        if fb_url:
            facebook_found += 1
        elif website:
            facebook_tbd += 1

        prospects.append({
            "Business_Name": details.get("name") or name,
            "Owner_Name": "",
            "City": extract_city(details.get("formatted_address", "")),
            "State": "FL",
            "Website_URL": website or "",
            "Facebook_URL": fb_url if fb_url else ("TBD" if (website or is_social_url) else ""),
            "Google_Profile_URL": details.get("url") or "",
            "Prospect_Score": score,
            "Pain_Points_Found": "",
            "Notes": build_notes(details, website_data, score_reason),
        })

    # --- Always save JSON backup first ---
    output_path = args.output
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(prospects, f, indent=2, ensure_ascii=False)
    print(f"\n💾 JSON backup saved to: {output_path}")

    # --- Upload to Google Sheets ---
    sheet_uploaded = 0
    sheet_skipped = 0
    if args.skip_sheet:
        print("⏭️  Sheet upload skipped (--skip-sheet flag)")
    else:
        print("\n📤 Uploading to Google Sheet...", end=" ", flush=True)
        try:
            sheet_uploaded, sheet_skipped = upload_to_sheet(prospects)
            print(f"{sheet_uploaded} new prospects, {sheet_skipped} skipped as duplicates")
        except Exception as e:
            print(f"\n❌ Sheet upload failed: {e}")
            print(f"   Your data is safe — JSON backup is at: {output_path}")

    print("\n" + "=" * 52)
    print("📊 SUMMARY")
    print("=" * 52)
    print(f"Total prospects processed : {len(prospects)}")
    print(f"  Score 5 — Dream fit     : {score_counts[5]}")
    print(f"  Score 4 — Strong fit    : {score_counts[4]}")
    print(f"  Score 3 — Potential     : {score_counts[3]}")
    print(f"  Score 2 — Weak fit      : {score_counts[2]}")
    print(f"  Score 1 — Skip          : {score_counts[1]}")
    print(f"Facebook found auto       : {facebook_found}")
    print(f"Facebook needs manual     : {facebook_tbd}")
    if not args.skip_sheet and sheet_uploaded > 0:
        print(f"\n✅ {sheet_uploaded} rows added to sheet: {SHEET_URL}")


if __name__ == "__main__":
    main()
