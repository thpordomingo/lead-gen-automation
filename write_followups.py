import argparse
import json
import os
import sys
import time

import gspread
from google.oauth2.service_account import Credentials
from gspread.exceptions import APIError

SHEET_ID = "1UEKd9ItlMcm5WwTw0D69_MRTNJBOgkMAtx66qCS-Dfs"
SHEET_TAB = "PROSPECTS"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SERVICE_ACCOUNT_FILE = os.path.join(SCRIPT_DIR, "service-account.json")

GSPREAD_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def load_updates(input_arg):
    if input_arg == "-":
        raw = sys.stdin.read()
    else:
        with open(input_arg, "r", encoding="utf-8") as f:
            raw = f.read()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON input: {e}") from e

    if not isinstance(data, list):
        raise ValueError("Input JSON must be an array of objects")

    normalized = []

    for index, item in enumerate(data, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"Item #{index} is not an object")

        prospect_id = str(item.get("Prospect_ID", "")).strip()
        draft_message_2 = str(item.get("Draft_Message_2", "")).strip()

        if not prospect_id:
            raise ValueError(f"Item #{index} is missing Prospect_ID")

        if not draft_message_2:
            raise ValueError(f"Item #{index} is missing Draft_Message_2")

        normalized.append({
            "Prospect_ID": prospect_id,
            "Draft_Message_2": draft_message_2,
        })

    return normalized


def open_sheet():
    if not os.path.exists(SERVICE_ACCOUNT_FILE):
        raise FileNotFoundError(f"service-account.json not found at {SERVICE_ACCOUNT_FILE}")

    creds = Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE,
        scopes=GSPREAD_SCOPES,
    )

    gc = gspread.authorize(creds)
    return gc.open_by_key(SHEET_ID).worksheet(SHEET_TAB)


def build_row_lookup(sheet):
    values = sheet.get_all_values()
    lookup = {}

    for row_index, row in enumerate(values[1:], start=2):
        if not row:
            continue

        prospect_id = row[0].strip() if len(row) >= 1 else ""

        if prospect_id:
            lookup[prospect_id] = row_index

    return lookup


def batch_update_with_retry(sheet, requests, max_retries=5):
    for attempt in range(max_retries):
        try:
            sheet.batch_update(requests)
            return
        except APIError as e:
            error_text = str(e)

            if "429" in error_text:
                wait_seconds = 30 * (attempt + 1)
                print(f"Hit 429 quota limit. Waiting {wait_seconds} seconds, then retrying...")
                time.sleep(wait_seconds)
                continue

            raise

    raise RuntimeError("Failed after multiple retries due to quota limits")


def update_followups(sheet, updates):
    row_lookup = build_row_lookup(sheet)

    requests = []
    not_found = 0

    for item in updates:
        prospect_id = item["Prospect_ID"]
        row_number = row_lookup.get(prospect_id)

        if not row_number:
            print(f"Not found: {prospect_id}")
            not_found += 1
            continue

        requests.append({
            "range": f"W{row_number}",
            "values": [[item["Draft_Message_2"]]],
        })

    print(f"Prepared updates: {len(requests)}")
    print(f"Not found: {not_found}")

    batch_size = 20
    updated = 0

    for i in range(0, len(requests), batch_size):
        batch = requests[i:i + batch_size]
        batch_update_with_retry(sheet, batch)
        updated += len(batch)
        print(f"Updated batch {i // batch_size + 1}: {len(batch)} rows")
        time.sleep(2)

    return updated, not_found


def main():
    parser = argparse.ArgumentParser(
        description="Update Draft_Message_2 in column W for existing prospects."
    )
    parser.add_argument(
        "input",
        help='JSON file path, or "-" to read a JSON array from stdin',
    )
    args = parser.parse_args()

    try:
        updates = load_updates(args.input)
        sheet = open_sheet()
        updated, not_found = update_followups(sheet, updates)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"\nSummary: {updated} updated, {not_found} not found")


if __name__ == "__main__":
    main()

