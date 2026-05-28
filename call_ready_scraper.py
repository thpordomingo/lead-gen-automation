import argparse
import datetime
import json
import os
import re
import sys
import time
from collections import Counter
from urllib.parse import urlparse

import gspread
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials

from prospect_scraper import (
    GOOGLE_PLACES_API_KEY,
    GSPREAD_SCOPES,
    SERVICE_ACCOUNT_FILE,
    SHEET_ID,
    get_place_details,
    search_places,
)

load_dotenv()

CALL_READY_TAB = "CALL_READY"
PROSPECTS_TAB = "PROSPECTS"
QUERY_TRACKER_TAB = "QUERY_TRACKER"
QUERY_PLAN_TAB = "QUERY_PLAN"
ASSIGNED_TO = "Thomas"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    )
}

LEAD_ID_PATTERN = re.compile(r"^C(\d+)$")
RUN_ID_PATTERN = re.compile(r"^R(\d+)$")
EMAIL_PATTERN = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
OWNER_CONTEXT_PATTERN = re.compile(
    r"(?:owner|founder|president|ceo|operator|managed by|owned by)\s*[:\-]?\s*"
    r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})"
)
COPYRIGHT_NAME_PATTERN = re.compile(
    r"(?:copyright|©|\(c\))\s*(?:\d{4}\s*)?([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})"
)

SERVICE_KEYWORDS = {
    "landscap": "Landscaping",
    "lawn": "Lawn Care",
    "hardscap": "Hardscaping",
    "irrigat": "Irrigation",
    "paver": "Pavers",
    "outdoor living": "Outdoor Living",
    "landscape lighting": "Landscape Lighting",
    "drainage": "Drainage",
    "sod": "Sod",
    "turf": "Turf",
    "tree": "Tree Service",
    "arbor": "Tree Service",
    "mulch": "Landscaping",
    "fertiliz": "Lawn Care",
    "weed control": "Lawn Care",
}

GREEN_INDUSTRY_TYPES = {
    "landscaper",
    "landscape_designer",
    "lawn_care_service",
    "tree_service",
    "gardener",
}

NON_USA_MARKERS = {
    "canada",
    "mexico",
    "united kingdom",
    "australia",
    "new zealand",
}

LEAD_AGGREGATOR_DOMAINS = (
    "thumbtack.com",
    "angi.com",
    "angieslist.com",
    "houzz.com",
    "homeadvisor.com",
    "lawnstarter.com",
    "yelp.com",
    "nextdoor.com",
    "bark.com",
)

CALL_READY_COLUMNS = [
    "Lead_ID",
    "Company_Name",
    "Owner_Name",
    "Owner_Name_Status",
    "Phone",
    "City",
    "State",
    "Service_Category",
    "Website_URL",
    "Google_Rating",
    "Google_Review_Count",
    "Google_Profile_URL",
    "Facebook_URL",
    "Fit_Score",
    "Lead_Status",
    "Call_Status",
    "Last_Touch_Date",
    "Next_Touch_Date",
    "Touch_Count",
    "Assigned_To",
    "Call_Notes",
    "Objection",
    "Meeting_Booked",
    "Meeting_Date",
    "Show_Status",
    "Disqualified_Reason",
    "Source_Query",
    "Date_Added",
    "Tags",
    "Skip_Reason",
]

QUERY_TRACKER_COLUMNS = [
    "Date",
    "Run_ID",
    "City",
    "State",
    "Category",
    "Query",
    "Max_Results",
    "Rows_Added",
    "Call_Ready_Count",
    "Review_Count",
    "Skipped_Count",
    "Duplicates_Skipped",
    "Status",
    "Notes",
]


def normalize_text(value):
    return re.sub(r"\s+", " ", (value or "").strip()).lower()


def normalize_phone(value):
    digits = re.sub(r"\D", "", value or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits


def normalize_website(value):
    if not value:
        return ""
    parsed = urlparse(value if "://" in value else f"https://{value}")
    host = (parsed.netloc or parsed.path).lower()
    host = host.lstrip("www.")
    path = parsed.path.rstrip("/") if parsed.netloc else ""
    return f"{host}{path}".rstrip("/")


def titleize_words(value):
    return " ".join(word.capitalize() for word in value.split())


def extract_city(formatted_address):
    if not formatted_address:
        return ""
    parts = [part.strip() for part in formatted_address.split(",")]
    if len(parts) >= 3:
        return parts[-3]
    if len(parts) == 2:
        return parts[0]
    return ""


def extract_state(formatted_address):
    if not formatted_address:
        return ""
    parts = [part.strip() for part in formatted_address.split(",")]
    if len(parts) >= 2:
        match = re.match(r"([A-Z]{2})\b", parts[-2])
        if match:
            return match.group(1)
    return ""


def is_usa_based(formatted_address):
    address = normalize_text(formatted_address)
    if not address:
        return False
    if "usa" in address or "united states" in address:
        return True
    if any(marker in address for marker in NON_USA_MARKERS):
        return False
    return bool(re.search(r",\s*[A-Z]{2}\s+\d{5}(?:-\d{4})?(?:,\s*)?$", formatted_address or ""))


def detect_service_category(details, query):
    haystacks = [
        details.get("name", ""),
        " ".join(details.get("types") or []),
        query or "",
    ]
    joined = " ".join(haystacks).lower()
    for keyword, category in SERVICE_KEYWORDS.items():
        if keyword in joined:
            return category

    types = set(details.get("types") or [])
    if "landscaper" in types or "landscape_designer" in types:
        return "Landscaping"
    if "lawn_care_service" in types:
        return "Lawn Care"
    if "tree_service" in types:
        return "Tree Service"
    return ""


def infer_query_category(query):
    query_text = normalize_text(query)
    ordered_matches = [
        ("landscape lighting", "landscape lighting"),
        ("outdoor living", "outdoor living"),
        ("lawn care", "lawn care"),
        ("tree service", "tree service"),
        ("hardscape", "hardscape"),
        ("irrigation", "irrigation"),
        ("landscaping", "landscaping"),
        ("paver", "pavers"),
        ("drainage", "drainage"),
        ("sod", "sod"),
    ]
    for needle, category in ordered_matches:
        if needle in query_text:
            return category
    return ""


def infer_query_city_state(query):
    tokens = re.findall(r"[A-Za-z]+", query or "")
    if len(tokens) < 2:
        return "", ""

    state = ""
    if len(tokens[-1]) == 2 and tokens[-1].isalpha():
        state = tokens[-1].upper()
        tokens = tokens[:-1]

    category = infer_query_category(query)
    filler_words = {
        "company", "companies", "contractor", "contractors", "service", "services",
        "business", "businesses", "near", "me", "in",
    }
    category_tokens = set(re.findall(r"[A-Za-z]+", category))
    city_tokens = [
        token for token in tokens
        if normalize_text(token) not in filler_words
        and normalize_text(token) not in category_tokens
    ]
    city = titleize_words(" ".join(city_tokens[-3:])) if city_tokens else ""
    return city, state


def is_green_industry(details, query, service_category):
    if service_category:
        return True
    types = set(details.get("types") or [])
    if types & GREEN_INDUSTRY_TYPES:
        return True
    name_and_types = " ".join([
        details.get("name", ""),
        " ".join(details.get("types") or []),
    ]).lower()
    return any(keyword in name_and_types for keyword in SERVICE_KEYWORDS)


def looks_like_aggregator(website_url):
    normalized = normalize_website(website_url)
    return any(domain in normalized for domain in LEAD_AGGREGATOR_DOMAINS)


def estimate_revenue_fit(details, website_url):
    reviews = details.get("user_ratings_total") or 0
    photos = len(details.get("photos") or [])
    rating = details.get("rating") or 0
    name = normalize_text(details.get("name"))
    chain_markers = ("llc of", "franchise", "corporate", "national")
    if any(marker in name for marker in chain_markers):
        return False
    if reviews >= 15 and reviews <= 400 and rating >= 4.0 and (website_url or photos >= 3):
        return True
    if reviews >= 30 and website_url:
        return True
    return False


def fetch_public_web_snippets(website_url):
    result = {
        "facebook_url": "",
        "title": "",
        "owner_name": "",
        "owner_status": "Missing",
    }
    if not website_url or looks_like_aggregator(website_url):
        return result

    try:
        response = requests.get(
            website_url,
            headers=HEADERS,
            timeout=8,
            allow_redirects=True,
        )
        response.raise_for_status()
    except Exception:
        return result

    soup = BeautifulSoup(response.text, "html.parser")
    if soup.title and soup.title.string:
        result["title"] = soup.title.string.strip()

    for tag in soup.find_all("a", href=True):
        href = tag["href"].strip()
        if "facebook.com/" in href and "facebook.com/sharer" not in href:
            if href.startswith("//"):
                href = f"https:{href}"
            result["facebook_url"] = href
            break

    text_chunks = []
    if soup.title and soup.title.string:
        text_chunks.append(soup.title.string)
    for selector in ("meta[name='description']", "meta[property='og:description']"):
        tag = soup.select_one(selector)
        if tag and tag.get("content"):
            text_chunks.append(tag["content"])
    text_chunks.append(soup.get_text(separator=" ", strip=True)[:4000])
    combined_text = " ".join(text_chunks)

    owner_match = OWNER_CONTEXT_PATTERN.search(combined_text)
    if owner_match:
        result["owner_name"] = owner_match.group(1).strip()
        result["owner_status"] = "Confirmed"
        return result

    copyright_match = COPYRIGHT_NAME_PATTERN.search(combined_text)
    if copyright_match:
        candidate = copyright_match.group(1).strip()
        if len(candidate.split()) >= 2 and not EMAIL_PATTERN.search(candidate):
            result["owner_name"] = candidate
            result["owner_status"] = "Probable"

    return result


def infer_owner_from_company(company_name, service_category):
    if not company_name:
        return "", "Missing"

    cleaned = re.sub(r"[^A-Za-z\s&\-]", " ", company_name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return "", "Missing"

    if service_category:
        category_words = set(normalize_text(service_category).split())
    else:
        category_words = set()

    tokens = cleaned.split()
    ignore_tokens = {
        "llc", "inc", "co", "company", "services", "service", "landscaping", "landscape",
        "lawn", "care", "tree", "hardscape", "hardscaping", "irrigation", "outdoor",
        "living", "lighting", "drainage", "sod", "turf", "pavers", "solutions", "pros",
    }
    filtered = [
        token for token in tokens
        if normalize_text(token) not in ignore_tokens and normalize_text(token) not in category_words
    ]

    if len(filtered) >= 2 and all(token[0].isupper() for token in filtered[:2]):
        return " ".join(filtered[:2]), "Probable"
    return "", "Missing"


def fit_score(details, website_url, service_category, usa_based):
    reviews = details.get("user_ratings_total") or 0
    rating = details.get("rating") or 0
    photos_count = len(details.get("photos") or [])
    revenue_fit = estimate_revenue_fit(details, website_url)

    score = 0
    if service_category:
        score += 2
    if usa_based:
        score += 1
    if reviews >= 20:
        score += 2
    if rating >= 4.3:
        score += 1
    if website_url:
        score += 1
    if photos_count > 0:
        score += 1
    if revenue_fit:
        score += 2
    return score


def determine_lead_status(score):
    if score >= 7:
        return "Call-Ready"
    if score >= 5:
        return "Review"
    return "Skip"


def open_workbook():
    if not os.path.exists(SERVICE_ACCOUNT_FILE):
        raise FileNotFoundError(f"service-account.json not found at {SERVICE_ACCOUNT_FILE}")

    creds = Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE,
        scopes=GSPREAD_SCOPES,
    )
    gc = gspread.authorize(creds)
    return gc.open_by_key(SHEET_ID)


def get_existing_call_ready_state(sheet):
    values = sheet.get_all_values()
    data_rows = values[1:] if len(values) > 1 else []

    existing_ids = []
    phones = set()
    websites = set()
    names = set()

    for row in data_rows:
        if len(row) >= 1:
            match = LEAD_ID_PATTERN.match((row[0] or "").strip())
            if match:
                existing_ids.append(int(match.group(1)))
        if len(row) >= 5:
            phone = normalize_phone(row[4])
            if phone:
                phones.add(phone)
        if len(row) >= 9:
            website = normalize_website(row[8])
            if website:
                websites.add(website)
        if len(row) >= 2:
            name = normalize_text(row[1])
            if name:
                names.add(name)

    next_id = max(existing_ids) + 1 if existing_ids else 1
    return next_id, phones, websites, names


def get_existing_prospects_state(sheet):
    values = sheet.get_all_values()
    data_rows = values[1:] if len(values) > 1 else []

    names = set()
    websites = set()
    for row in data_rows:
        if len(row) >= 2:
            name = normalize_text(row[1])
            if name:
                names.add(name)
        if len(row) >= 6:
            website = normalize_website(row[5])
            if website:
                websites.add(website)
    return names, websites


def get_next_run_id(sheet):
    values = sheet.get_all_values()
    data_rows = values[1:] if len(values) > 1 else []
    existing_ids = []
    for row in data_rows:
        if row and row[1]:
            match = RUN_ID_PATTERN.match(row[1].strip())
            if match:
                existing_ids.append(int(match.group(1)))
    next_id = max(existing_ids) + 1 if existing_ids else 1
    return f"R{next_id:03d}"


def append_query_tracker_row(workbook, summary):
    try:
        tracker_sheet = workbook.worksheet(QUERY_TRACKER_TAB)
    except gspread.WorksheetNotFound:
        print(f"WARNING: worksheet {QUERY_TRACKER_TAB} not found. Skipping tracker append.")
        return

    run_id = get_next_run_id(tracker_sheet)
    city, state = infer_query_city_state(summary["query"])
    category = infer_query_category(summary["query"])

    row = [
        summary["date"],
        run_id,
        city,
        state,
        category,
        summary["query"],
        summary["max_results"],
        summary["rows_added"],
        summary["call_ready_count"],
        summary["review_count"],
        summary["skipped_count"],
        summary["duplicates_skipped"],
        "Done",
        "",
    ]
    tracker_sheet.append_row(row, value_input_option="USER_ENTERED")


def mark_query_plan_done(workbook, query):
    try:
        plan_sheet = workbook.worksheet(QUERY_PLAN_TAB)
    except gspread.WorksheetNotFound:
        print(f"WARNING: worksheet {QUERY_PLAN_TAB} not found. Skipping QUERY_PLAN update.")
        return

    values = plan_sheet.get_all_values()
    if not values:
        print(f"WARNING: worksheet {QUERY_PLAN_TAB} is empty. Skipping QUERY_PLAN update.")
        return

    header = values[0]
    try:
        query_col = header.index("query") + 1
        status_col = header.index("status") + 1
    except ValueError:
        print(f"WARNING: worksheet {QUERY_PLAN_TAB} is missing query/status headers. Skipping QUERY_PLAN update.")
        return

    matched_rows = []
    for row_index, row in enumerate(values[1:], start=2):
        row_query = row[query_col - 1].strip() if len(row) >= query_col else ""
        if row_query == query:
            matched_rows.append((row_index, row))

    if not matched_rows:
        print(f"WARNING: query not found in {QUERY_PLAN_TAB}: {query}")
        return

    for row_index, row in matched_rows:
        current_status = row[status_col - 1].strip() if len(row) >= status_col else ""
        if current_status == "Planned":
            cell_ref = gspread.utils.rowcol_to_a1(row_index, status_col)
            plan_sheet.update_acell(cell_ref, "Done")
            return
        if current_status == "Done":
            return

    print(f"WARNING: matching query found in {QUERY_PLAN_TAB}, but no Planned row was available to update: {query}")


def build_call_ready_row(lead_id, lead, query, today):
    tags = f"{lead['Lead_Status']}, Outbound, {lead['Service_Category']}"
    return [
        lead_id,
        lead["Company_Name"],
        lead["Owner_Name"],
        lead["Owner_Name_Status"],
        lead["Phone"],
        lead["City"],
        lead["State"],
        lead["Service_Category"],
        lead["Website_URL"],
        lead["Google_Rating"],
        lead["Google_Review_Count"],
        lead["Google_Profile_URL"],
        lead["Facebook_URL"],
        lead["Fit_Score"],
        lead["Lead_Status"],
        "Not Called",
        "",
        "",
        0,
        ASSIGNED_TO,
        "",
        "",
        "No",
        "",
        "",
        "",
        query,
        today,
        tags,
        "",
    ]


def main():
    parser = argparse.ArgumentParser(
        description="Cold-calling lead scraper: Google Places -> CALL_READY"
    )
    parser.add_argument("query", help='e.g. "landscaping Austin TX"')
    parser.add_argument("--max", type=int, default=50, help="Max results (default 50)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview leads without writing to Google Sheets",
    )
    args = parser.parse_args()

    if not GOOGLE_PLACES_API_KEY:
        print("ERROR: GOOGLE_PLACES_API_KEY not found in .env file. Aborting.")
        sys.exit(1)

    print(f"Searching Google Places: {args.query}")
    places = search_places(args.query, args.max)
    print(f"Total places found: {len(places)}")

    workbook = open_workbook()
    call_ready_sheet = workbook.worksheet(CALL_READY_TAB)
    prospects_sheet = workbook.worksheet(PROSPECTS_TAB)
    next_id = 1
    call_ready_phones = set()
    call_ready_websites = set()
    call_ready_names = set()
    prospects_names = set()
    prospects_websites = set()

    next_id, call_ready_phones, call_ready_websites, call_ready_names = (
        get_existing_call_ready_state(call_ready_sheet)
    )
    prospects_names, prospects_websites = get_existing_prospects_state(prospects_sheet)

    batch_phones = set()
    batch_websites = set()
    batch_names = set()
    leads_to_append = []
    skipped_reasons = Counter()
    duplicate_skipped = 0
    review_count = 0
    call_ready_count = 0

    today = datetime.date.today().strftime("%Y-%m-%d")

    for index, place in enumerate(places, start=1):
        place_id = place.get("place_id")
        raw_name = place.get("name", "Unknown")
        print(f"[{index}/{len(places)}] {raw_name}")

        if not place_id:
            skipped_reasons["missing_place_id"] += 1
            print("  skipped: missing place id")
            continue

        time.sleep(1)
        details = get_place_details(place_id)
        if not details:
            skipped_reasons["details_unavailable"] += 1
            print("  skipped: place details unavailable")
            continue

        company_name = (details.get("name") or raw_name).strip()
        phone = (details.get("formatted_phone_number") or "").strip()
        website_url = (details.get("website") or "").strip()
        google_profile_url = (details.get("url") or "").strip()
        rating = details.get("rating") or ""
        review_count_value = details.get("user_ratings_total") or 0
        city = extract_city(details.get("formatted_address", ""))
        state = extract_state(details.get("formatted_address", ""))
        usa_based = is_usa_based(details.get("formatted_address", ""))
        service_category = detect_service_category(details, args.query)

        if not phone:
            skipped_reasons["missing_phone"] += 1
            print("  skipped: phone required")
            continue

        if not usa_based:
            skipped_reasons["non_usa"] += 1
            print("  skipped: not clearly USA-based")
            continue

        if not is_green_industry(details, args.query, service_category):
            skipped_reasons["non_green_industry"] += 1
            print("  skipped: not green-industry related")
            continue

        if looks_like_aggregator(website_url):
            skipped_reasons["aggregator_site"] += 1
            print("  skipped: aggregator or directory site")
            continue

        phone_key = normalize_phone(phone)
        website_key = normalize_website(website_url)
        name_key = normalize_text(company_name)

        duplicate_reason = ""
        if phone_key and phone_key in batch_phones:
            duplicate_reason = "duplicate in current batch by phone"
        elif website_key and website_key in batch_websites:
            duplicate_reason = "duplicate in current batch by website"
        elif name_key and name_key in batch_names:
            duplicate_reason = "duplicate in current batch by company name"
        elif phone_key and phone_key in call_ready_phones:
            duplicate_reason = "already exists in CALL_READY by phone"
        elif website_key and website_key in call_ready_websites:
            duplicate_reason = "already exists in CALL_READY by website"
        elif name_key and name_key in call_ready_names:
            duplicate_reason = "already exists in CALL_READY by company name"
        elif website_key and website_key in prospects_websites:
            duplicate_reason = "already exists in PROSPECTS by website"
        elif name_key and name_key in prospects_names:
            duplicate_reason = "already exists in PROSPECTS by company name"

        if duplicate_reason:
            duplicate_skipped += 1
            print(f"  skipped: {duplicate_reason}")
            continue

        web_data = fetch_public_web_snippets(website_url)
        owner_name = web_data["owner_name"]
        owner_status = web_data["owner_status"]
        facebook_url = web_data["facebook_url"]

        if not owner_name:
            owner_name, owner_status = infer_owner_from_company(company_name, service_category)

        score = fit_score(details, website_url, service_category, usa_based)
        lead_status = determine_lead_status(score)
        if lead_status == "Skip":
            skipped_reasons["fit_score_below_5"] += 1
            print(f"  skipped: fit score {score}/10")
            continue

        lead = {
            "Company_Name": company_name,
            "Owner_Name": owner_name,
            "Owner_Name_Status": owner_status if owner_name else "Missing",
            "Phone": phone,
            "City": city,
            "State": state or "",
            "Service_Category": service_category or "Green Industry",
            "Website_URL": website_url,
            "Google_Rating": rating,
            "Google_Review_Count": review_count_value,
            "Google_Profile_URL": google_profile_url,
            "Facebook_URL": facebook_url,
            "Fit_Score": score,
            "Lead_Status": lead_status,
        }

        if lead_status == "Call-Ready":
            call_ready_count += 1
        else:
            review_count += 1

        batch_phones.add(phone_key)
        if website_key:
            batch_websites.add(website_key)
        batch_names.add(name_key)

        if args.dry_run:
            print(
                f"  keep: {lead_status} | score {score}/10 | "
                f"{lead['Phone']} | {lead['Service_Category']} | owner {lead['Owner_Name_Status']}"
            )
            leads_to_append.append(lead)
        else:
            lead_id = f"C{next_id:03d}"
            next_id += 1
            leads_to_append.append(build_call_ready_row(lead_id, lead, args.query, today))
            call_ready_phones.add(phone_key)
            if website_key:
                call_ready_websites.add(website_key)
            call_ready_names.add(name_key)
            print(
                f"  keep: {lead_status} | {lead_id} | score {score}/10 | "
                f"{lead['Phone']} | {lead['Service_Category']}"
            )

    rows_added = 0
    if args.dry_run:
        print("\nDry run preview")
        for lead in leads_to_append:
            preview = {
                "Company_Name": lead["Company_Name"],
                "Owner_Name": lead["Owner_Name"],
                "Owner_Name_Status": lead["Owner_Name_Status"],
                "Phone": lead["Phone"],
                "Service_Category": lead["Service_Category"],
                "Fit_Score": lead["Fit_Score"],
                "Lead_Status": lead["Lead_Status"],
                "Website_URL": lead["Website_URL"],
            }
            print(json.dumps(preview, ensure_ascii=False))
    elif leads_to_append:
        call_ready_sheet.append_rows(leads_to_append, value_input_option="USER_ENTERED")
        rows_added = len(leads_to_append)

    total_skipped = sum(skipped_reasons.values())

    if not args.dry_run:
        summary = {
            "date": today,
            "query": args.query,
            "max_results": args.max,
            "rows_added": rows_added,
            "call_ready_count": call_ready_count,
            "review_count": review_count,
            "skipped_count": total_skipped,
            "duplicates_skipped": duplicate_skipped,
        }
        append_query_tracker_row(workbook, summary)
        try:
            mark_query_plan_done(workbook, args.query)
        except Exception as exc:
            print(f"WARNING: QUERY_PLAN update failed: {exc}")

    print("\nSummary")
    print(f"total places found: {len(places)}")
    print(f"skipped count: {total_skipped}")
    print(f"call-ready count: {call_ready_count}")
    print(f"review count: {review_count}")
    print(f"duplicates skipped: {duplicate_skipped}")
    print(f"rows added to CALL_READY: {rows_added}")

    if skipped_reasons:
        print("\nSkipped reasons")
        for reason, count in skipped_reasons.most_common():
            print(f"- {reason}: {count}")


if __name__ == "__main__":
    main()
