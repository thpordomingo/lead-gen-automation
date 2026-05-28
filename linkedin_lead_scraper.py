import argparse
import datetime
import os
import re
import sys
import time
from urllib.parse import parse_qs, urlparse

import gspread
import requests
from bs4 import BeautifulSoup
from google.oauth2.service_account import Credentials

from prospect_scraper import GSPREAD_SCOPES, SERVICE_ACCOUNT_FILE, SHEET_ID

LINKEDIN_READY_TAB = "LINKEDIN_READY"
LINKEDIN_QUERY_PLAN_TAB = "LINKEDIN_QUERY_PLAN"
LINKEDIN_QUERY_TRACKER_TAB = "LINKEDIN_QUERY_TRACKER"
SEARCH_URL = "https://html.duckduckgo.com/html/"
LEAD_ID_PATTERN = re.compile(r"^L(\d+)$")
RUN_ID_PATTERN = re.compile(r"^R(\d+)$")
REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    )
}

SERVICE_CATEGORIES = [
    ("outdoor living", "Outdoor Living"),
    ("lawn care", "Lawn Care"),
    ("tree service", "Tree Service"),
    ("hardscape", "Hardscape"),
    ("irrigation", "Irrigation"),
    ("landscaping", "Landscaping"),
    ("landscape construction", "Landscape Construction"),
]
TITLE_KEYWORDS = ("owner", "founder", "president", "ceo")


def normalize_text(value):
    value = (value or "").strip().lower()
    value = re.sub(r"[^a-z0-9\s]", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def normalize_url(value):
    if not value:
        return ""
    parsed = urlparse(value)
    scheme = parsed.scheme or "https"
    netloc = parsed.netloc.lower()
    path = parsed.path.rstrip("/")
    return f"{scheme}://{netloc}{path}"


def owner_company_key(owner_name, company_name):
    owner = normalize_text(owner_name)
    company = normalize_text(company_name)
    if owner and company:
        return f"{owner}|{company}"
    return ""


def open_workbook_and_sheets():
    if not os.path.exists(SERVICE_ACCOUNT_FILE):
        raise FileNotFoundError(f"service-account.json not found at {SERVICE_ACCOUNT_FILE}")

    creds = Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE,
        scopes=GSPREAD_SCOPES,
    )
    gc = gspread.authorize(creds)
    workbook = gc.open_by_key(SHEET_ID)

    try:
        linkedin_ready = workbook.worksheet(LINKEDIN_READY_TAB)
        query_plan = workbook.worksheet(LINKEDIN_QUERY_PLAN_TAB)
        query_tracker = workbook.worksheet(LINKEDIN_QUERY_TRACKER_TAB)
    except gspread.WorksheetNotFound as exc:
        raise RuntimeError(f"Required worksheet missing: {exc}") from exc

    return workbook, linkedin_ready, query_plan, query_tracker


def get_next_lead_id(sheet):
    values = sheet.get_all_values()
    data_rows = values[1:] if len(values) > 1 else []
    existing_ids = []
    for row in data_rows:
        if row and row[0]:
            match = LEAD_ID_PATTERN.match(row[0].strip())
            if match:
                existing_ids.append(int(match.group(1)))
    next_num = max(existing_ids) + 1 if existing_ids else 1
    return next_num


def get_next_run_id(sheet):
    values = sheet.get_all_values()
    data_rows = values[1:] if len(values) > 1 else []
    existing_ids = []
    for row in data_rows:
        if len(row) > 1 and row[1]:
            match = RUN_ID_PATTERN.match(row[1].strip())
            if match:
                existing_ids.append(int(match.group(1)))
    next_num = max(existing_ids) + 1 if existing_ids else 1
    return f"R{next_num:03d}"


def load_existing_dedupes(sheet):
    values = sheet.get_all_values()
    data_rows = values[1:] if len(values) > 1 else []
    profile_urls = set()
    company_urls = set()
    owner_company_keys = set()

    for row in data_rows:
        profile_url = normalize_url(row[4]) if len(row) > 4 else ""
        company_url = normalize_url(row[5]) if len(row) > 5 else ""
        owner_name = row[1] if len(row) > 1 else ""
        company_name = row[3] if len(row) > 3 else ""
        pair_key = owner_company_key(owner_name, company_name)

        if profile_url:
            profile_urls.add(profile_url)
        if company_url:
            company_urls.add(company_url)
        if pair_key:
            owner_company_keys.add(pair_key)

    return profile_urls, company_urls, owner_company_keys


def infer_city_state(query):
    tokens = re.findall(r"[A-Za-z]+", query or "")
    if len(tokens) < 2:
        return "", ""
    state = tokens[-1].upper() if len(tokens[-1]) == 2 else ""
    if not state:
        return "", ""
    city_tokens = tokens[:-1]
    filler = {"owner", "founder", "president", "ceo", "landscaping", "hardscape", "outdoor", "living", "irrigation", "landscape", "construction"}
    filtered = [token for token in city_tokens if token.lower() not in filler]
    if not filtered:
        return "", state
    city = " ".join(word.capitalize() for word in filtered[-3:])
    return city, state


def infer_service_category(query):
    query_text = normalize_text(query)
    for needle, label in SERVICE_CATEGORIES:
        if needle in query_text:
            return label
    return "Green Industry"


def extract_search_urls(raw_url):
    if not raw_url:
        return ""
    parsed = urlparse(raw_url)
    if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
        params = parse_qs(parsed.query)
        return params.get("uddg", [""])[0]
    return raw_url


def search_public_results(query, max_results):
    response = requests.get(
        SEARCH_URL,
        params={"q": query},
        headers=REQUEST_HEADERS,
        timeout=20,
    )
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    results = []

    for block in soup.select(".result"):
        link_tag = block.select_one(".result__a")
        if not link_tag:
            continue
        url = extract_search_urls(link_tag.get("href", "").strip())
        title = link_tag.get_text(" ", strip=True)
        snippet_tag = block.select_one(".result__snippet")
        snippet = snippet_tag.get_text(" ", strip=True) if snippet_tag else ""
        if not url:
            continue
        results.append({
            "url": url,
            "title": title,
            "snippet": snippet,
        })
        if len(results) >= max_results:
            break

    return results


def classify_linkedin_url(url):
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    if "linkedin.com" not in host:
        return ""
    if path.startswith("/in/"):
        return "profile"
    if path.startswith("/company/"):
        return "company"
    return ""


def infer_owner_name(title):
    title = title.split(" - ")[0]
    title = title.split(" | ")[0]
    title = re.sub(r"\s*\(.*?\)\s*", " ", title)
    parts = [part.strip() for part in re.split(r"[,\-–|]", title) if part.strip()]
    candidate = parts[0] if parts else title.strip()
    candidate = re.sub(r"\b(LinkedIn|Owner|Founder|President|CEO)\b", "", candidate, flags=re.IGNORECASE)
    candidate = re.sub(r"\s+", " ", candidate).strip()
    words = candidate.split()
    if 1 <= len(words) <= 4 and all(word[:1].isupper() for word in words if word):
        return candidate
    return ""


def infer_title(query, title, snippet):
    haystack = normalize_text(" ".join([query, title, snippet]))
    for keyword in ("owner", "founder", "president", "ceo"):
        if keyword in haystack:
            return keyword.upper() if keyword == "ceo" else keyword.capitalize()
    return ""


def infer_company_name(query, title, snippet):
    for text in (title, snippet):
        cleaned = text.split(" - ")[0].split(" | ")[0].strip()
        if " at " in cleaned.lower():
            match = re.search(r"\bat\s+(.+)$", cleaned, flags=re.IGNORECASE)
            if match:
                company = match.group(1).strip()
                if company:
                    return company
        if "owner of" in cleaned.lower():
            match = re.search(r"\bowner of\s+(.+)$", cleaned, flags=re.IGNORECASE)
            if match:
                company = match.group(1).strip()
                if company:
                    return company
    query_words = re.findall(r"[A-Za-z]+", query)
    filtered = [
        word for word in query_words
        if word.lower() not in {"owner", "founder", "president", "ceo", "austin", "round", "rock", "cedar", "park", "georgetown", "dallas", "fort", "worth", "tampa", "orlando", "charlotte", "nashville", "tx", "fl", "nc", "tn"}
    ]
    return " ".join(word.capitalize() for word in filtered[:3])


def score_lead(lead):
    score = 0
    if normalize_text(lead["title"]) in TITLE_KEYWORDS:
        score += 3
    if lead["service_category"] in {"Landscaping", "Hardscape", "Irrigation", "Outdoor Living", "Lawn Care", "Tree Service"}:
        score += 3
    if lead["city"] and lead["state"]:
        score += 1
    if lead["linkedin_profile_url"]:
        score += 2
    if lead["linkedin_company_url"]:
        score += 1
    return score


def dedupe_reason(lead, existing_profile_urls, existing_company_urls, existing_owner_company_keys):
    profile_url = normalize_url(lead["linkedin_profile_url"])
    company_url = normalize_url(lead["linkedin_company_url"])
    pair_key = owner_company_key(lead["owner_name"], lead["company_name"])

    if profile_url and profile_url in existing_profile_urls:
        return "profile_url"
    if company_url and company_url in existing_company_urls:
        return "company_url"
    if pair_key and pair_key in existing_owner_company_keys:
        return "owner_company"
    return ""


def build_queries(query):
    service_query = normalize_text(query)
    service_query = re.sub(r"\s+", " ", service_query)
    return [
        f"site:linkedin.com/in {service_query}",
        f"site:linkedin.com/company {service_query}",
    ]


def extract_leads_from_results(results, source_query):
    city, state = infer_city_state(source_query)
    service_category = infer_service_category(source_query)
    leads = []

    profile_results = []
    company_results = []
    for result in results:
        kind = classify_linkedin_url(result["url"])
        if kind == "profile":
            profile_results.append(result)
        elif kind == "company":
            company_results.append(result)

    for result in profile_results:
        owner_name = infer_owner_name(result["title"])
        title = infer_title(source_query, result["title"], result["snippet"])
        company_name = infer_company_name(source_query, result["title"], result["snippet"])
        company_match = None
        company_name_tokens = normalize_text(company_name).split()

        for company_result in company_results:
            company_haystack = normalize_text(" ".join([company_result["title"], company_result["snippet"]]))
            if company_name_tokens and any(token in company_haystack for token in company_name_tokens[:2]):
                company_match = company_result
                break

        lead = {
            "owner_name": owner_name,
            "title": title,
            "company_name": company_name,
            "linkedin_profile_url": result["url"],
            "linkedin_company_url": company_match["url"] if company_match else "",
            "city": city,
            "state": state,
            "service_category": service_category,
            "source_query": source_query,
            "notes": f"Profile title: {result['title']}",
        }
        lead["fit_score"] = score_lead(lead)
        leads.append(lead)

    if not leads:
        for result in company_results:
            lead = {
                "owner_name": "",
                "title": infer_title(source_query, result["title"], result["snippet"]),
                "company_name": infer_company_name(source_query, result["title"], result["snippet"]),
                "linkedin_profile_url": "",
                "linkedin_company_url": result["url"],
                "city": city,
                "state": state,
                "service_category": service_category,
                "source_query": source_query,
                "notes": f"Company title: {result['title']}",
            }
            lead["fit_score"] = score_lead(lead)
            leads.append(lead)

    return leads


def build_row(lead_id, lead, today):
    return [
        lead_id,
        lead["owner_name"],
        lead["title"],
        lead["company_name"],
        lead["linkedin_profile_url"],
        lead["linkedin_company_url"],
        lead["city"],
        lead["state"],
        lead["service_category"],
        lead["source_query"],
        lead["fit_score"],
        "New",
        "",
        "",
        0,
        lead["notes"],
        today,
    ]


def append_query_tracker_row(sheet, query, max_results, rows_added, leads_kept, duplicates_skipped, low_score_skipped):
    run_id = get_next_run_id(sheet)
    date_added = datetime.date.today().strftime("%Y-%m-%d")
    city, state = infer_city_state(query)
    category = infer_service_category(query)
    row = [
        date_added,
        run_id,
        city,
        state,
        category,
        query,
        max_results,
        rows_added,
        leads_kept,
        duplicates_skipped,
        low_score_skipped,
        "Done",
        "",
    ]
    sheet.append_row(row, value_input_option="USER_ENTERED")


def mark_query_plan_done(sheet, query):
    values = sheet.get_all_values()
    if not values:
        print(f"WARNING: worksheet {LINKEDIN_QUERY_PLAN_TAB} is empty")
        return

    header = values[0]
    try:
        query_col = header.index("query") + 1
        status_col = header.index("status") + 1
    except ValueError:
        print(f"WARNING: worksheet {LINKEDIN_QUERY_PLAN_TAB} is missing query/status headers")
        return

    matches = []
    for row_index, row in enumerate(values[1:], start=2):
        row_query = row[query_col - 1].strip() if len(row) >= query_col else ""
        if row_query == query:
            matches.append((row_index, row))

    if not matches:
        print(f"WARNING: query not found in {LINKEDIN_QUERY_PLAN_TAB}: {query}")
        return

    for row_index, row in matches:
        current_status = row[status_col - 1].strip() if len(row) >= status_col else ""
        if current_status == "Planned":
            cell_ref = gspread.utils.rowcol_to_a1(row_index, status_col)
            sheet.update_acell(cell_ref, "Done")
            return
        if current_status == "Done":
            return

    print(f"WARNING: matching query found in {LINKEDIN_QUERY_PLAN_TAB}, but no Planned row was available to update: {query}")


def main():
    parser = argparse.ArgumentParser(
        description="LinkedIn lead scraper using public web search only."
    )
    parser.add_argument("query", help='e.g. "owner landscaping Austin TX"')
    parser.add_argument("--max", type=int, default=20, help="Max search results to inspect")
    parser.add_argument("--dry-run", action="store_true", help="Preview results without writing to Sheets")
    args = parser.parse_args()

    try:
        _, linkedin_ready_sheet, query_plan_sheet, query_tracker_sheet = open_workbook_and_sheets()
    except Exception as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)

    print(f"Searching public web results: {args.query}")
    queries = build_queries(args.query)
    combined_results = []
    seen_urls = set()

    for query in queries:
        try:
            results = search_public_results(query, args.max)
        except Exception as exc:
            print(f"  search warning: {exc}")
            continue

        for result in results:
            normalized_url = normalize_url(result["url"])
            if normalized_url in seen_urls:
                continue
            seen_urls.add(normalized_url)
            combined_results.append(result)
            if len(combined_results) >= args.max:
                break
        if len(combined_results) >= args.max:
            break
        time.sleep(1)

    print(f"Total results found: {len(combined_results)}")

    extracted_leads = extract_leads_from_results(combined_results, args.query)
    existing_profile_urls, existing_company_urls, existing_owner_company_keys = load_existing_dedupes(linkedin_ready_sheet)
    batch_profile_urls = set()
    batch_company_urls = set()
    batch_owner_company_keys = set()

    rows_to_append = []
    dry_run_leads = []
    low_score_skipped = 0
    duplicates_skipped = 0
    leads_kept = 0
    next_lead_num = get_next_lead_id(linkedin_ready_sheet)
    today = datetime.date.today().strftime("%Y-%m-%d")

    for lead in extracted_leads:
        if lead["fit_score"] < 6:
            low_score_skipped += 1
            continue

        reason = dedupe_reason(lead, existing_profile_urls | batch_profile_urls, existing_company_urls | batch_company_urls, existing_owner_company_keys | batch_owner_company_keys)
        if reason:
            duplicates_skipped += 1
            continue

        leads_kept += 1
        if args.dry_run:
            dry_run_leads.append(lead)
        else:
            lead_id = f"L{next_lead_num:03d}"
            next_lead_num += 1
            rows_to_append.append(build_row(lead_id, lead, today))

        profile_url = normalize_url(lead["linkedin_profile_url"])
        company_url = normalize_url(lead["linkedin_company_url"])
        pair_key = owner_company_key(lead["owner_name"], lead["company_name"])
        if profile_url:
            batch_profile_urls.add(profile_url)
        if company_url:
            batch_company_urls.add(company_url)
        if pair_key:
            batch_owner_company_keys.add(pair_key)

    rows_added = 0
    if args.dry_run:
        for lead in dry_run_leads:
            print(
                f"  keep: score {lead['fit_score']}/10 | "
                f"{lead['owner_name'] or 'Unknown Owner'} | "
                f"{lead['company_name'] or 'Unknown Company'} | "
                f"profile {lead['linkedin_profile_url'] or 'none'} | "
                f"company {lead['linkedin_company_url'] or 'none'}"
            )
    elif rows_to_append:
        linkedin_ready_sheet.append_rows(rows_to_append, value_input_option="USER_ENTERED")
        rows_added = len(rows_to_append)
        append_query_tracker_row(
            query_tracker_sheet,
            args.query,
            args.max,
            rows_added,
            leads_kept,
            duplicates_skipped,
            low_score_skipped,
        )
        mark_query_plan_done(query_plan_sheet, args.query)
    else:
        append_query_tracker_row(
            query_tracker_sheet,
            args.query,
            args.max,
            0,
            leads_kept,
            duplicates_skipped,
            low_score_skipped,
        )
        mark_query_plan_done(query_plan_sheet, args.query)

    print("\nSummary")
    print(f"total results found: {len(combined_results)}")
    print(f"leads kept: {leads_kept}")
    print(f"duplicates skipped: {duplicates_skipped}")
    print(f"low score skipped: {low_score_skipped}")
    print(f"rows added to LINKEDIN_READY: {rows_added}")


if __name__ == "__main__":
    main()
