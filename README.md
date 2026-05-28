# Lead Generation and Outreach Automation

A collection of Python scripts and Google Apps Script automations built to power end-to-end outbound lead generation for a US digital marketing agency (Green Marketing, lawn and landscape niche).

Built iteratively over several months with Claude Code and Codex CLI as development tools.

## What's inside

### Python scripts

**`prospect_scraper.py`** — Core prospecting engine. Queries Google Places API to pull local businesses by search term, enriches each result with website and Facebook detection, applies a custom 1-5 scoring model (review count, website quality, photo presence, agency exclusion), and uploads scored leads directly to a Google Sheet via Service Account authentication. JSON backup included.

**`call_ready_scraper.py`** — Extended version of the scraper with additional enrichment for call-ready outbound, including phone number normalization and call status tracking.

**`linkedin_lead_scraper.py`** — LinkedIn-targeted lead scraper using structured query plans.

**`linkedin_enrichment.py`** — Enrichment layer that augments LinkedIn leads with additional firmographic and contact data.

**`update_drafts.py`** — Pushes generated outreach drafts (JSON input) into the corresponding rows of a Google Sheet, with strict field validation that aborts the script if input shape is unexpected. Built after a silent overwrite bug taught me to never trust unstructured input.

**`write_followups.py`** — Writes follow-up sequences into the Sheet based on prospect status and tags.

**`run_next_query.py`** — Helper that pulls the next un-run query from a query plan CSV and feeds it into the scraper. Lets the system run sequentially across dozens of queries without manual intervention.

### Google Apps Script

**`call_ready_apps_script.js`** — Daily agenda automation. Reads a structured schedule from a Google Sheet, cross-references it against Google Calendar to detect time conflicts, dynamically substitutes a rotating project block based on the day of the week, and sends a formatted HTML email every weekday morning at a fixed time.

**`linkedin_crm_apps_script.js`** — CRM-side automation that syncs LinkedIn outreach activity into the Sheet's tracking columns.

### Documentation

**`AGENTS.md`** — Context document used to brief Claude Code and Codex when working on this codebase. Includes architecture, naming conventions, and operational rules.

## Stack

Python 3 · Google Places API · gspread · Google Sheets API · Google Calendar API · Google Apps Script · Service Account authentication · dotenv for secrets

## Notes

This repo is shared as portfolio evidence of hands-on work with APIs, automation, and AI-assisted development. Credentials, real prospect data, and API keys are excluded via `.gitignore`. The scripts are operational in their original environment but would need credential setup to run elsewhere.
