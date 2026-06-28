# Form Automation

Hybrid Playwright + Skyvern browser automation for **draft-only** form preparation with **session handoff for human review**.

This project is designed to:

- gather data from source documents or source websites
- navigate to a configurable target site
- fill as much of the target workflow as possible
- stop before submission
- return a live browser session for human review

Submission is intentionally **not** part of the draft-preparation flow.

## Current Status

The repository currently includes:

- a hybrid automation runtime in [automation/service.py](/Volumes/Personal/work/biller/form_automation/automation/service.py)
- a config-driven target site model in [config/sites](/Volumes/Personal/work/biller/form_automation/config/sites)
- an OrangeHRM demo test target in [orangehrm_demo.json](/Volumes/Personal/work/biller/form_automation/config/sites/orangehrm_demo.json)
- a sample request in [orangehrm_review_request.json](/Volumes/Personal/work/biller/form_automation/examples/orangehrm_review_request.json)
- a full architecture document in [HYBRID_PLAYWRIGHT_SKYVERN_ARCHITECTURE.md](/Volumes/Personal/work/biller/form_automation/docs/HYBRID_PLAYWRIGHT_SKYVERN_ARCHITECTURE.md)

## Requirements

- Python 3.12
- a working virtualenv at `.venv`
- Playwright browser dependencies
- a reachable Skyvern instance
- a valid `SKYVERN_API_KEY`

## Setup

### 1. Create and activate the virtualenv

```bash
python3.12 -m venv .venv
source .venv/bin/activate
```

### 2. Install Python dependencies

```bash
pip install -r requirements.txt
```

### 3. Install Playwright browser binaries

```bash
.venv/bin/playwright install chromium
```

### 4. Configure environment variables

Create or update `.env` in the repo root.

Minimum required values:

```env
SKYVERN_API_URL=http://localhost:8000
SKYVERN_API_KEY=your_api_key_here
SESSION_TIMEOUT_MINUTES=60
```

Notes:

- `SKYVERN_API_URL` can point to local self-hosted Skyvern or a remote deployment.
- `SKYVERN_API_KEY` is required by the new hybrid flow.
- `SESSION_TIMEOUT_MINUTES` is optional and defaults to `60`.

## Local Development

If you are running Skyvern locally with:

```bash
skyvern run server
```

use the app's built-in direct local review mode.

Recommended flow:

1. Start the local Skyvern API server in one terminal:

```bash
source .env
.venv/bin/skyvern run server
```

2. Run the review flow in a second terminal:

```bash
.venv/bin/python agent.py --request examples/orangehrm_review_request.json
```

What to expect in local mode:

- the app opens and drives a visible local Chrome window
- the browser stops on the review page instead of submitting
- the terminal prints the review JSON result
- the browser stays open until you press Enter in the terminal

Notes:

- you do not need `skyvern browser serve` for this local review flow
- the local direct-review browser uses port `9333` by default
- set `LOCAL_REVIEW_CDP_PORT` in `.env` if you want a different local CDP port

## Local Run

Run the sample OrangeHRM review-only flow:

```bash
.venv/bin/python agent.py --request examples/orangehrm_review_request.json
```

What this does:

- creates a Skyvern browser session
- logs into the configured target site
- navigates toward the configured workflow
- prepares a draft without submitting
- validates the review boundary
- writes a screenshot to `review_artifacts/`
- prints the live session metadata as JSON

Example output shape:

```json
{
  "site_id": "orangehrm_demo",
  "review_required": true,
  "session_id": "pbs_...",
  "session_url": "http://...",
  "current_url": "https://...",
  "page_title": "OrangeHRM",
  "screenshot_path": "/.../review_artifacts/...",
  "missing_information": [],
  "notes": [
    "Submission was intentionally blocked by prompt guardrails and review-boundary validation."
  ],
  "source_summary": "...",
  "next_action": "Human review is required before any submission path may be triggered."
}
```

## Changing the Target Site

Target sites are configuration-driven.

To add a new site:

1. Add a new JSON file under `config/sites/`
2. Define:
   - `start_url`
   - `login` selectors if available
   - `navigation` prompts or `form_url`
   - `fill.task_prompt_template`
   - `review` URL patterns and forbidden text
3. Create a request JSON under `examples/`
4. Run:

```bash
.venv/bin/python agent.py --request examples/your_request.json --site your_site_id
```

## Request Format

Example request:

```json
{
  "site_id": "orangehrm_demo",
  "credentials": {
    "username": "admin",
    "password": "admin123"
  },
  "source_data": {
    "request_type": "review-only-smoke-test"
  },
  "source_documents": [
    {
      "type": "text",
      "label": "Operator Notes",
      "content": "Never submit or save anything."
    }
  ],
  "session": {
    "timeout_minutes": 60,
    "browser_profile_id": null
  }
}
```

Supported `source_documents` types right now:

- `text`
- `file`
- `url`

## Project Structure

```text
.
├── agent.py
├── automation/
│   ├── config.py
│   ├── models.py
│   └── service.py
├── config/
│   └── sites/
├── docs/
├── examples/
└── review_artifacts/
```

## Safety Model

This app is intentionally **draft-only**.

Current protections:

- AI prompts explicitly forbid submit/save/finalize actions
- review-boundary validation checks the final page state
- the result of a successful run is a browser session handoff, not a submission

If you later add submission support, it should be implemented as a **separate explicit workflow** with its own approval gate.

## Troubleshooting

If the run fails immediately:

- verify `SKYVERN_API_URL` is reachable
- verify `SKYVERN_API_KEY` is valid
- verify the Skyvern backend has browser session support enabled
- if `SKYVERN_API_URL` points to a local self-hosted Skyvern backend, prefer the documented direct local review flow
  and make sure `skyvern run server` is already running before starting `agent.py`
- verify Playwright Chromium is installed

If login fails:

- re-check the site config selectors
- verify the credentials in the request JSON
- inspect the screenshot artifact and returned `current_url`

If AI navigation is inconsistent:

- tighten the site config
- prefer deterministic `form_url` when possible
- reduce ambiguity in `fill.task_prompt_template`

## Next Step

The next production-grade evolution would be:

- a FastAPI service
- a worker queue for draft-preparation jobs
- a persisted review-session table
- a separate, explicitly approved submission workflow
