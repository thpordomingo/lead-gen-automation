import argparse
import os
import re
import sys
import time
from urllib.parse import parse_qs, quote_plus, urlparse

import gspread
import requests
from bs4 import BeautifulSoup
from google.oauth2.service_account import Credentials

from prospect_scraper import GSPREAD_SCOPES, SERVICE_ACCOUNT_FILE, SHEET_ID

CALL_READY_TAB = "CALL_READY"
DUCKDUCKGO_HTML_URL = "https://html.duckduckgo.com/html/"
REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    )
}

READ_HEADERS = [
    "Lead_Status",
    "Fit_Score",
    "Company_Name",
    "Owner_Name",
    "City",
    "State",
    "LinkedIn_Profile_URL",
]

WRITE_HEADERS = [
    "LinkedIn_Profile_URL",
    "LinkedIn_Status",
    "LinkedIn_Company_URL",
    "LinkedIn_Match_Confidence",
    "LinkedIn_Notes",
]

ROLE_HINTS = ("owner", "founder", "president", "ceo")


def normalize_text(value):
    cleaned = re.sub(r"[^a-z0-9\s]", " ", (value or "").lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def normalize_company_name(value):
    normalized = normalize_text(value)
    ignore = {
        "llc", "inc", "co", "company", "services", "service", "group", "solutions",
        "contractor", "contractors",
    }
    tokens = [token for token in normalized.split() if token not in ignore]
    return " ".join(tokens)


def title_case_words(value):
    return " ".join(word.capitalize() for word in (value or "").split())


def open_call_ready_sheet():
    if not os.path.exists(SERVICE_ACCOUNT_FILE):
        raise FileNotFoundError(f"service-account.json not found at {SERVICE_ACCOUNT_FILE}")

    creds = Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE,
        scopes=GSPREAD_SCOPES,
    )
    gc = gspread.authorize(creds)
    workbook = gc.open_by_key(SHEET_ID)
    return workbook.worksheet(CALL_READY_TAB)


def build_header_map(sheet):
    headers = sheet.row_values(1)
    return {header.strip(): index + 1 for index, header in enumerate(headers) if header.strip()}


def validate_headers(header_map, required_headers):
    missing = [header for header in required_headers if header not in header_map]
    if missing:
        raise ValueError("Missing required headers in CALL_READY: " + ", ".join(missing))


def get_value(row, header_map, header_name):
    col_index = header_map[header_name] - 1
    return row[col_index].strip() if len(row) > col_index and isinstance(row[col_index], str) else (
        row[col_index] if len(row) > col_index else ""
    )


def select_leads(rows, header_map, overwrite):
    selected = []
    skipped_counts = {
        "not_call_ready": 0,
        "fit_score_below_8": 0,
        "existing_profile_without_overwrite": 0,
    }

    for row_index, row in enumerate(rows[1:], start=2):
        lead_status = str(get_value(row, header_map, "Lead_Status")).strip()
        fit_score_raw = str(get_value(row, header_map, "Fit_Score")).strip()
        existing_profile = str(get_value(row, header_map, "LinkedIn_Profile_URL")).strip()

        try:
            fit_score = float(fit_score_raw or 0)
        except ValueError:
            fit_score = 0

        if lead_status != "Call-Ready":
            skipped_counts["not_call_ready"] += 1
            continue
        if fit_score < 8:
            skipped_counts["fit_score_below_8"] += 1
            continue
        if existing_profile and not overwrite:
            skipped_counts["existing_profile_without_overwrite"] += 1
            continue

        selected.append({
            "row_index": row_index,
            "lead_id": str(get_value(row, header_map, "Lead_ID")).strip(),
            "company_name": str(get_value(row, header_map, "Company_Name")).strip(),
            "owner_name": str(get_value(row, header_map, "Owner_Name")).strip(),
            "city": str(get_value(row, header_map, "City")).strip(),
            "state": str(get_value(row, header_map, "State")).strip(),
            "fit_score": fit_score,
            "existing_profile": existing_profile,
        })

    return selected, skipped_counts


def build_search_queries(lead):
    company_name = lead["company_name"]
    city = lead["city"]
    state = lead["state"]
    owner_name = lead["owner_name"]

    queries = [
        f'site:linkedin.com/in owner "{company_name}" "{city}" "{state}"',
        f'site:linkedin.com/in founder "{company_name}" "{city}" "{state}"',
        f'site:linkedin.com/in president "{company_name}" "{city}" "{state}"',
        f'site:linkedin.com/company "{company_name}" "{city}" "{state}"',
    ]
    if owner_name:
        queries.extend([
            f'site:linkedin.com/in "{owner_name}" "{company_name}"',
            f'site:linkedin.com/in "{owner_name}" "{city}" "{state}"',
        ])
    return queries


def extract_actual_url(raw_url):
    if not raw_url:
        return ""
    parsed = urlparse(raw_url)
    if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
        query_params = parse_qs(parsed.query)
        return query_params.get("uddg", [""])[0]
    return raw_url


def search_public_results(query):
    response = requests.get(
        DUCKDUCKGO_HTML_URL,
        params={"q": query},
        headers=REQUEST_HEADERS,
        timeout=20,
    )
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    results = []

    for result in soup.select(".result"):
        link_tag = result.select_one(".result__a")
        if not link_tag:
            continue
        raw_url = link_tag.get("href", "").strip()
        actual_url = extract_actual_url(raw_url)
        title = link_tag.get_text(" ", strip=True)
        snippet_tag = result.select_one(".result__snippet")
        snippet = snippet_tag.get_text(" ", strip=True) if snippet_tag else ""
        if actual_url:
            results.append({
                "query": query,
                "url": actual_url,
                "title": title,
                "snippet": snippet,
            })

    return results


def linkedin_path_type(url):
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


def score_profile_result(result, lead):
    haystack = normalize_text(" ".join([
        result["title"],
        result["snippet"],
        result["url"],
        result["query"],
    ]))
    score = 0
    notes = []

    owner_name = normalize_text(lead["owner_name"])
    company_name = normalize_company_name(lead["company_name"])
    city = normalize_text(lead["city"])
    state = normalize_text(lead["state"])

    if owner_name:
        owner_tokens = owner_name.split()
        if owner_tokens and all(token in haystack for token in owner_tokens):
            score += 4
            notes.append("owner_name")
        elif any(token in haystack for token in owner_tokens):
            score += 2
            notes.append("partial_owner_name")

    company_tokens = company_name.split()
    if company_tokens and all(token in haystack for token in company_tokens[:3]):
        score += 3
        notes.append("company_name")
    elif company_tokens and any(token in haystack for token in company_tokens[:3]):
        score += 1
        notes.append("partial_company_name")

    if city and city in haystack:
        score += 1
        notes.append("city")
    if state and state in haystack:
        score += 1
        notes.append("state")
    if any(role in haystack for role in ROLE_HINTS):
        score += 1
        notes.append("role_hint")

    confidence = ""
    if score >= 6:
        confidence = "High"
    elif score >= 4:
        confidence = "Medium"

    return score, confidence, notes


def score_company_result(result, lead):
    haystack = normalize_text(" ".join([
        result["title"],
        result["snippet"],
        result["url"],
        result["query"],
    ]))
    company_name = normalize_company_name(lead["company_name"])
    city = normalize_text(lead["city"])
    state = normalize_text(lead["state"])

    score = 0
    notes = []

    company_tokens = company_name.split()
    if company_tokens and all(token in haystack for token in company_tokens[:3]):
        score += 3
        notes.append("company_name")
    elif company_tokens and any(token in haystack for token in company_tokens[:3]):
        score += 1
        notes.append("partial_company_name")

    if city and city in haystack:
        score += 1
        notes.append("city")
    if state and state in haystack:
        score += 1
        notes.append("state")

    confidence = "Medium" if score >= 3 else ""
    return score, confidence, notes


def choose_linkedin_matches(results, lead):
    best_profile = None
    best_company = None
    profile_candidates = []
    company_candidates = []

    for result in results:
        path_type = linkedin_path_type(result["url"])
        if path_type == "profile":
            score, confidence, notes = score_profile_result(result, lead)
            profile_candidates.append({
                "url": result["url"],
                "title": result["title"],
                "query": result["query"],
                "score": score,
                "confidence": confidence or "None",
                "notes": notes,
            })
            if confidence:
                candidate = {
                    "url": result["url"],
                    "confidence": confidence,
                    "score": score,
                    "notes": notes,
                    "query": result["query"],
                    "title": result["title"],
                }
                if not best_profile or candidate["score"] > best_profile["score"]:
                    best_profile = candidate
        elif path_type == "company":
            score, confidence, notes = score_company_result(result, lead)
            company_candidates.append({
                "url": result["url"],
                "title": result["title"],
                "query": result["query"],
                "score": score,
                "confidence": confidence or "None",
                "notes": notes,
            })
            if confidence:
                candidate = {
                    "url": result["url"],
                    "confidence": confidence,
                    "score": score,
                    "notes": notes,
                    "query": result["query"],
                    "title": result["title"],
                }
                if not best_company or candidate["score"] > best_company["score"]:
                    best_company = candidate

    return best_profile, best_company, profile_candidates, company_candidates


def build_update_payload(best_profile, best_company, debug_reason=""):
    if best_profile:
        notes = f"Matched via public search: {best_profile['title']}"
        if best_company:
            notes += f" | company page: {best_company['url']}"
        return {
            "LinkedIn_Profile_URL": best_profile["url"],
            "LinkedIn_Status": "Found",
            "LinkedIn_Company_URL": best_company["url"] if best_company else "",
            "LinkedIn_Match_Confidence": best_profile["confidence"],
            "LinkedIn_Notes": notes,
            "result_type": "profile",
        }

    if best_company:
        return {
            "LinkedIn_Profile_URL": "",
            "LinkedIn_Status": "Company Found",
            "LinkedIn_Company_URL": best_company["url"],
            "LinkedIn_Match_Confidence": "Medium",
            "LinkedIn_Notes": f"Only company page found via public search: {best_company['title']}",
            "result_type": "company",
        }

    return {
        "LinkedIn_Profile_URL": "",
        "LinkedIn_Status": "Not Found",
        "LinkedIn_Company_URL": "",
        "LinkedIn_Match_Confidence": "Not Found",
        "LinkedIn_Notes": debug_reason or "No confident LinkedIn match found via public search",
        "result_type": "not_found",
    }


def explain_not_found(all_results, profile_candidates, company_candidates):
    if not all_results:
        return "No public search results returned"
    if not profile_candidates and not company_candidates:
        return "Search results returned, but no LinkedIn profile or company URLs were found"
    if profile_candidates and not any(candidate["confidence"] in ("High", "Medium") for candidate in profile_candidates):
        return "LinkedIn profile URLs were found, but none met confidence threshold"
    if company_candidates and not any(candidate["confidence"] == "Medium" for candidate in company_candidates):
        return "LinkedIn company URLs were found, but none met confidence threshold"
    return "No confident LinkedIn match found via public search"


def update_row(sheet, header_map, row_index, payload, overwrite):
    updates = []
    for header_name in WRITE_HEADERS:
        if header_name not in header_map:
            continue
        if header_name == "LinkedIn_Profile_URL" and not overwrite:
            existing_value = str(sheet.cell(row_index, header_map[header_name]).value or "").strip()
            if existing_value:
                continue
        updates.append({
            "range": gspread.utils.rowcol_to_a1(row_index, header_map[header_name]),
            "values": [[payload.get(header_name, "")]],
        })

    if updates:
        sheet.batch_update(updates, value_input_option="USER_ENTERED")
    return len(updates) > 0


def main():
    parser = argparse.ArgumentParser(
        description="Enrich CALL_READY leads with public LinkedIn search results."
    )
    parser.add_argument("--limit", type=int, default=20, help="Maximum leads to check")
    parser.add_argument("--dry-run", action="store_true", help="Preview matches without writing to Sheets")
    parser.add_argument("--overwrite", action="store_true", help="Allow overwriting existing LinkedIn_Profile_URL values")
    args = parser.parse_args()

    if args.limit < 1:
        print("ERROR: --limit must be at least 1")
        sys.exit(1)

    sheet = open_call_ready_sheet()
    header_map = build_header_map(sheet)
    validate_headers(header_map, READ_HEADERS)

    missing_write_headers = [header for header in WRITE_HEADERS if header not in header_map]
    if missing_write_headers:
        print("WARNING: missing write headers in CALL_READY: " + ", ".join(missing_write_headers))
        if not args.dry_run:
            print("ERROR: non-dry-run requires those headers to exist before updating.")
            sys.exit(1)

    rows = sheet.get_all_values()
    selected, skipped_counts = select_leads(rows, header_map, args.overwrite)
    selected = selected[:args.limit]

    leads_checked = 0
    profiles_found = 0
    company_pages_found = 0
    not_found = 0
    rows_updated = 0

    for lead in selected:
        leads_checked += 1
        query_list = build_search_queries(lead)
        all_results = []

        print(
            f"Checking row {lead['row_index']}: {lead['company_name']} | "
            f"{lead['city']}, {lead['state']}"
        )

        for query in query_list:
            try:
                results = search_public_results(query)
                all_results.extend(results)
            except Exception as exc:
                print(f"  search warning: {exc}")
            time.sleep(1)

        best_profile, best_company, profile_candidates, company_candidates = choose_linkedin_matches(all_results, lead)
        debug_reason = explain_not_found(all_results, profile_candidates, company_candidates)
        payload = build_update_payload(best_profile, best_company, debug_reason)

        if payload["result_type"] == "profile":
            profiles_found += 1
            print(
                f"  profile found: {payload['LinkedIn_Profile_URL']} "
                f"({payload['LinkedIn_Match_Confidence']})"
            )
        elif payload["result_type"] == "company":
            company_pages_found += 1
            print(f"  company page found: {payload['LinkedIn_Company_URL']}")
        else:
            not_found += 1
            print("  no confident match found")

        if args.dry_run:
            print(f"  Lead_ID: {lead['lead_id']}")
            print(f"  Company_Name: {lead['company_name']}")
            print(f"  Owner_Name: {lead['owner_name'] or 'N/A'}")
            print(f"  City: {lead['city']}")
            print(f"  State: {lead['state']}")
            print(f"  Fit_Score: {lead['fit_score']}")
            print("  search queries generated:")
            for query in query_list:
                print(f"  - {query}")

            linkedin_urls = []
            seen_urls = set()
            for result in all_results:
                if "linkedin.com/" not in result["url"]:
                    continue
                if result["url"] in seen_urls:
                    continue
                seen_urls.add(result["url"])
                linkedin_urls.append(result["url"])

            print("  URLs found from public search:")
            if linkedin_urls:
                for url in linkedin_urls:
                    print(f"  - {url}")
            else:
                print("  - none")

            print(f"  chosen LinkedIn_Profile_URL: {payload['LinkedIn_Profile_URL'] or 'none'}")
            print(f"  chosen LinkedIn_Company_URL: {payload['LinkedIn_Company_URL'] or 'none'}")
            print(f"  match confidence: {payload['LinkedIn_Match_Confidence']}")
            if payload["result_type"] == "not_found":
                print(f"  reason if Not Found: {payload['LinkedIn_Notes']}")
            print("")

        if args.dry_run:
            continue

        updated = update_row(sheet, header_map, lead["row_index"], payload, args.overwrite)
        if updated:
            rows_updated += 1

    print("\nSummary")
    print(f"leads checked: {leads_checked}")
    print(f"profiles found: {profiles_found}")
    print(f"company pages found: {company_pages_found}")
    print(f"not found: {not_found}")
    print(f"skipped: {sum(skipped_counts.values())}")
    print(f"rows updated: {rows_updated}")

    if args.dry_run:
        print("\nSkipped reasons")
        for reason, count in skipped_counts.items():
            print(f"- {reason}: {count}")


if __name__ == "__main__":
    main()
