import argparse
import os
import subprocess
import sys

import gspread
from google.oauth2.service_account import Credentials

from prospect_scraper import GSPREAD_SCOPES, SERVICE_ACCOUNT_FILE, SHEET_ID

QUERY_PLAN_TAB = "QUERY_PLAN"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCRAPER_PATH = os.path.join(SCRIPT_DIR, "call_ready_scraper.py")


def open_query_plan_sheet():
    if not os.path.exists(SERVICE_ACCOUNT_FILE):
        raise FileNotFoundError(f"service-account.json not found at {SERVICE_ACCOUNT_FILE}")

    creds = Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE,
        scopes=GSPREAD_SCOPES,
    )
    gc = gspread.authorize(creds)
    workbook = gc.open_by_key(SHEET_ID)
    return workbook.worksheet(QUERY_PLAN_TAB)


def parse_planned_queries(sheet):
    values = sheet.get_all_values()
    if not values:
        return []

    header = values[0]
    try:
        priority_col = header.index("priority")
        query_col = header.index("query")
        max_col = header.index("max_results")
        status_col = header.index("status")
    except ValueError as exc:
        raise ValueError("QUERY_PLAN is missing one or more required headers") from exc

    planned = []
    for row_index, row in enumerate(values[1:], start=2):
        status = row[status_col].strip() if len(row) > status_col else ""
        if status != "Planned":
            continue

        priority_raw = row[priority_col].strip() if len(row) > priority_col else ""
        query = row[query_col].strip() if len(row) > query_col else ""
        max_raw = row[max_col].strip() if len(row) > max_col else ""
        if not query or not max_raw:
            continue

        try:
            priority = int(priority_raw)
            max_results = int(max_raw)
        except ValueError:
            continue

        planned.append({
            "row_index": row_index,
            "priority": priority,
            "query": query,
            "max_results": max_results,
        })

    planned.sort(key=lambda item: (item["priority"], item["row_index"]))
    return planned


def build_command(query, max_results):
    return ["python3", SCRAPER_PATH, query, "--max", str(max_results)]


def main():
    parser = argparse.ArgumentParser(
        description="Run the next planned Call-Ready query from QUERY_PLAN."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the next planned query and command without executing it",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=1,
        help="How many planned queries to run sequentially (default 1)",
    )
    args = parser.parse_args()

    if args.limit < 1:
        print("ERROR: --limit must be at least 1")
        sys.exit(1)

    sheet = open_query_plan_sheet()
    planned = parse_planned_queries(sheet)
    if not planned:
        print("No planned queries left.")
        return

    selected = planned[:args.limit]

    if args.dry_run:
        for item in selected:
            command = build_command(item["query"], item["max_results"])
            print(f"Next planned query: {item['query']}")
            print("Command: " + " ".join(f'"{part}"' if " " in part else part for part in command))
        return

    for index, item in enumerate(selected, start=1):
        command = build_command(item["query"], item["max_results"])
        print(f"Selected query {index}/{len(selected)}: {item['query']}")
        print("Running: " + " ".join(f'"{part}"' if " " in part else part for part in command))
        result = subprocess.run(command, cwd=SCRIPT_DIR)
        if result.returncode != 0:
            print(f"Stopped after failure on query: {item['query']}")
            sys.exit(result.returncode)


if __name__ == "__main__":
    main()
