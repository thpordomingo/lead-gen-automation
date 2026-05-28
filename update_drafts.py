import argparse
import json
import os
import sys

import gspread
from google.oauth2.service_account import Credentials

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
        pain_point = str(item.get("Pain_Point_1", ""))
        draft_message = str(item.get("Draft_Message_1", ""))

        if not prospect_id:
            raise ValueError(f"Item #{index} is missing Prospect_ID")

        normalized.append({
            "Prospect_ID": prospect_id,
            "Pain_Point_1": pain_point,
            "Draft_Message_1": draft_message,
        })

    return normalized


def open_sheet():
    if not os.path.exists(SERVICE_ACCOUNT_FILE):
        raise FileNotFoundError(
            f"service-account.json not found at {SERVICE_ACCOUNT_FILE}"
        )

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
        business_name = row[1].strip() if len(row) >= 2 else ""

        if prospect_id:
            lookup[prospect_id] = {
                "row": row_index,
                "business_name": business_name or "Unknown",
            }

    return lookup


def update_drafts(sheet, updates):
    row_lookup = build_row_lookup(sheet)
    updated = 0
    not_found = 0

    for item in updates:
        prospect_id = item["Prospect_ID"]
        row_info = row_lookup.get(prospect_id)

        if not row_info:
            print(f"Not found: {prospect_id}")
            not_found += 1
            continue

        row_number = row_info["row"]
        business_name = row_info["business_name"]

        sheet.batch_update([{
            "range": f"T{row_number}:U{row_number}",
            "values": [[item["Draft_Message_1"], item["Pain_Point_1"]]],
        }])

        print(f"Updated {prospect_id} ({business_name}) ✓")
        updated += 1

    return updated, not_found


def main():
    parser = argparse.ArgumentParser(
        description="Update Draft_Message_1 and Pain_Point_1 for existing prospects."
    )
    parser.add_argument(
        "input",
        help='JSON file path, or "-" to read a JSON array from stdin',
    )
    args = parser.parse_args()

    try:
        updates = load_updates(args.input)
        sheet = open_sheet()
        updated, not_found = update_drafts(sheet, updates)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"\nSummary: {updated} updated, {not_found} not found")


if __name__ == "__main__":
    main()
