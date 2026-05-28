# Lead Gen System - Green Marketing

## Purpose
Automated prospect research and scoring system for Green Marketing, a Florida-based marketing agency serving the green industry (lawn care, landscaping, hardscaping). Prospects are contacted manually via Facebook Messenger using the "Katherine" account.

## Current state
The `prospect_scraper.py` script is working end-to-end:
1. Takes a search query (e.g. "landscaping Orlando FL") and a max count
2. Uses Google Places API to find businesses
3. For each business: fetches website HTML, extracts Facebook URL, detects existing agencies, detects copyright year
4. Calculates a Prospect_Score (1-5) based on review count, photos, website quality, agency detection
5. Saves a JSON backup and uploads directly to a Google Sheet via service account

## Key files
- `prospect_scraper.py` - main script
- `.env` - contains GOOGLE_PLACES_API_KEY (do NOT log, do NOT commit)
- `service-account.json` - credentials for Google Sheets API (do NOT log, do NOT commit)
- `.gitignore` - excludes .env, service-account.json, *.json
- `prospects_output.json` - last run's JSON backup

## Dependencies
Python 3.13
Libraries: requests, beautifulsoup4, python-dotenv, lxml, gspread, google-auth

## Target Google Sheet
- Name: "Green Marketing - Facebook Outreach Tracker"
- Sheet ID: 1UEKd9ItlMcm5WwTw0D69_MRTNJBOgkMAtx66qCS-Dfs
- Main tab: PROSPECTS (columns A-Y)
- Service account email has Editor access
- Additional tabs: MESSAGES_LOG, PAIN_POINTS_LIBRARY, DASHBOARD, CONFIG, ARCHIVE

## Scoring logic (current)
- Score 5 (Dream/THRIVING fit): 100+ reviews, has real website, 5+ photos, no detected agency
- Score 4 (Strong/SPROUTING fit): 50+ reviews, 3+ photos, no agency (Facebook-only caps here)
- Score 3 (Potential/SEEDING fit): 20-50 reviews, has online presence
- Score 2 (Weak): under 20 reviews, OR copyright older than 3 years
- Score 1 (Skip): no website AND no Facebook, or business closed, or existing agency detected

## ICP (Ideal Customer Profile)
- Industry: lawn care + landscaping + hardscaping
- Geography: Florida (South: Miami-Dade/Broward/Palm Beach; West: Tampa/St Pete/Sarasota; Central/North: Orlando/Jacksonville)
- Revenue range: $200K - $3M
- Tier mapping: SEEDING ($200K-$500K / $1000/mo) / SPROUTING ($500K-$1M / $2000/mo) / THRIVING ($1M+ / $3000/mo)

## User preferences
- User is Thomas (also called Paco), based in Buenos Aires
- Respond in Spanish when communicating with the user
- No em dashes in any written content
- Don't be sycophantic. Push back if something the user says is incorrect
- Ask clarifying questions before assuming context
- Prefer short, concrete answers over long explanations

## Running the script
```bash
# Normal run with sheet upload:
python3 prospect_scraper.py "landscaping Tampa FL" --max 20

# Test without writing to sheet:
python3 prospect_scraper.py "query" --max 5 --skip-sheet

# Custom output file:
python3 prospect_scraper.py "query" --max 20 --output custom.json
```

## What NOT to do
- Never log, print, or display the contents of .env or service-account.json
- Never commit secrets to any repo
- Never write manual HTTP calls when gspread provides the function
- Never assume Google Places returns complete categories (often only 1 category)
- Never auto-send messages to prospects (all outreach is manual by user)
- Never modify the main Google Sheet schema without asking - there are Apps Scripts depending on the exact column order

## Known issues / pending improvements (backlog)
- Detect lead aggregators (LawnStarter, Thumbtack-style sites, Chop Chop): they have websites that link to many businesses but aren't single contractors
- Add optional CLI flag --min-score N to filter out low scores before writing to sheet
- Consider adding score refinement based on website content (has pricing page? blog activity? contact form quality?)
- Eventual: generate first draft of outreach message per prospect using an LLM API

## Style
- Facebook Messenger messages to prospects should follow Aiden Silvers style: casual but not "wassup"-level, one pain point per message, no em dashes, proper sentence capitalization, humble tone with a soft exit line
- Videos (5-min Loom walkthroughs) are offered only when prospects have multiple technical issues worth explaining visually, not on every outreach

## Outreach tracking workflow
1. Script scrapes prospects into PROSPECTS tab with Status = "New"
2. User reviews and manually fills Draft_Message_1 + Pain_Point_1 columns
3. User sends message via Facebook (Katherine account)
4. User checks "Sent_1" checkbox - Apps Script auto-updates Last_Touch_Date, Touch_Count, Next_Touch_Due, Status -> Active
5. Daily 9am Apps Script emails overdue follow-ups