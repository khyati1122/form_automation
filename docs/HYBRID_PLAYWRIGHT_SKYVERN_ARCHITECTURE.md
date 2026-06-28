# Hybrid Playwright + Skyvern + Session Handoff Architecture

## Goal

This application prepares browser-based form drafts from structured source data, local source files, and source websites, then **stops before submission** so a human can review the full form inside the live browser session.

The system is intentionally designed so that:

- automation can save time on repetitive navigation and data entry
- the user always reviews the final browser state
- the app never auto-submits a form as part of the draft-preparation workflow

## Why This Architecture

The original prototype used an ADK wrapper that converted a user prompt into a single Skyvern task call. That created three problems:

1. The orchestration layer added latency without adding meaningful control.
2. The task prompt was biased toward one-shot form submission instead of a durable review workflow.
3. The app treated task creation as completion and had no persistent review-session contract.

The refactored design uses a **hybrid execution model**:

- **Playwright-style deterministic control** for session setup, login selectors, validation checkpoints, screenshots, and review gating.
- **Skyvern AI actions/tasks** only where flexibility is valuable:
  - site navigation when selectors are brittle or unknown
  - form filling across changing page layouts
  - extraction from source websites
- **Persistent browser sessions** as the handoff boundary between automation and human review.

This gives us a better safety posture than a pure prompt-driven browser agent and a better adaptability posture than pure selectors.

## High-Level Flow

### Phase 1: Request Intake

The backend accepts a JSON request with:

- `site_id`
- login `credentials`
- optional structured `source_data`
- optional `target_payload`
- optional `source_documents`
- optional `session.browser_profile_id`

Example request shape:

```json
{
  "site_id": "orangehrm_demo",
  "credentials": {
    "username": "admin",
    "password": "admin123"
  },
  "source_data": {
    "request_type": "admin-review-demo"
  },
  "source_documents": [
    {
      "type": "text",
      "label": "Operator Notes",
      "content": "Use this run only to validate login, navigation, and review handoff."
    }
  ],
  "session": {
    "timeout_minutes": 60,
    "browser_profile_id": null
  }
}
```

### Phase 2: Session Provisioning

The service creates a Skyvern browser session and connects to it over CDP.

Why this matters:

- the browser stays alive beyond the current process
- cookies and tab state remain attached to a single reviewable session
- the human can take over the exact state produced by automation

### Phase 3: Deterministic Login

If a target site provides login selectors in its site config, the service uses them directly:

- fill username
- fill password
- click login
- validate URL or page state

This keeps the most sensitive boundary deterministic instead of delegating credentials to a free-form prompt.

### Phase 4: Source Resolution

The service builds a source bundle from:

- inline structured `source_data`
- local files
- source websites

For source websites, the service opens a separate page in the same browser session and uses Skyvern extraction to convert page content into structured fields.

### Phase 5: Hybrid Draft Preparation

After login, the service navigates to the target form area using one of two strategies:

- deterministic `form_url` when available
- guarded Skyvern navigation prompt when selectors are unknown or brittle

Then it runs a guarded Skyvern draft-preparation task that:

- fills as much as possible
- uses provided source fields and notes
- stops short of any final action
- never clicks submit/save/confirm/finish/place-order buttons

### Phase 6: Review Handoff

The service validates the review boundary before returning control:

- current URL matches configured review patterns
- forbidden post-submit text is not present
- AI validation confirms the browser is on a review-safe page
- configured submit selectors are highlighted for the human reviewer

It then captures a screenshot artifact and returns:

- `session_id`
- `session_url`
- `current_url`
- `page_title`
- `screenshot_path`
- `missing_information`

## Core Safety Model

The system uses multiple independent controls because prompt-only safety is not enough for a production form workflow.

### 1. Draft and Submit Are Different Workflows

The current implementation only supports **draft preparation**.

There is no submit workflow in this code path.

That is intentional. In production, the submit action should be:

- a separate endpoint
- a separate job type
- gated by explicit user approval
- logged independently for auditability

### 2. Guarded AI Prompts

Every AI-driven navigation or fill task appends a hard rule:

- never click final submit/save/confirm/finish/place-order controls
- stop on a human-reviewable page

### 3. Review Boundary Validation

Each site configuration defines review expectations:

- expected review URLs
- forbidden post-submit text
- optional AI review validation prompt

If any check fails, the run is treated as unsafe and the session is closed instead of handed off.

### 4. Handoff Instead of Completion

The service only marks success when the browser is left in a reviewable state. Success no longer means “the agent started a task.”

## Configuration Model

Target sites are configured under `config/sites/<site_id>.json`.

Each site config can define:

- `start_url`
- `login`
- `navigation`
- `fill`
- `review`

This keeps the orchestration engine generic while pushing site-specific behavior into data.

### Config Sections

`login`

- deterministic selectors for username, password, submit
- expected success URL patterns
- optional validation prompt

`navigation`

- direct form URL or AI navigation prompt
- review handoff prompt

`fill`

- task prompt template
- field hints
- max AI steps

`review`

- review-safe URL patterns
- forbidden post-submit text
- submit selectors to highlight

## Runtime Components

### `agent.py`

CLI entrypoint that:

- loads the request JSON
- loads the target site config
- runs the hybrid draft-preparation service

### `automation/config.py`

Loads:

- environment variables
- Skyvern connection settings
- site configuration paths

### `automation/models.py`

Defines the typed request/config/result objects for:

- site config
- source documents
- review sessions
- final handoff payload

### `automation/service.py`

Main orchestration logic:

- create session
- connect browser
- login
- resolve source inputs
- navigate
- fill
- validate review boundary
- capture artifacts
- hand off session

## Production Design

### Recommended Server Topology

Use a serverized layout with three logical layers:

1. API layer
2. queue/worker layer
3. session + artifact storage

Suggested components:

- API service: FastAPI or similar
- job queue: Celery, Dramatiq, RQ, Temporal, or cloud queue workers
- persistence: Postgres for jobs, reviews, and audit events
- object storage: screenshots, recordings, logs
- Skyvern: self-hosted or managed deployment for browser sessions

### Recommended Database Entities

At minimum:

- `automation_requests`
- `automation_runs`
- `review_sessions`
- `review_decisions`
- `artifacts`
- `site_configs`

Useful fields:

- request payload hash
- site id
- source references
- session id
- browser profile id
- run state
- review required flag
- reviewed by
- approved at
- submission workflow id

### Multi-User Scaling Pattern

For 100s of users, prefer:

- browser **profiles** for reusable account state
- browser **sessions** only during active draft or review windows

Do not keep a live session open for every inactive user.

Recommended pattern:

1. reuse or create a browser profile per account/tenant
2. open a fresh browser session from that profile for draft prep
3. hand the session to a reviewer
4. close the session after review or timeout

### Review UI Expectations

The user-facing app should show:

- screenshot preview
- session status
- current page URL
- extracted source summary
- missing information list
- explicit buttons:
  - `Resume Review`
  - `Reject Draft`
  - `Approve For Submission`

`Approve For Submission` should not submit directly from the browser UI. It should create a new, auditable submit job.

## Target Site Lifecycle

When onboarding a new target site:

1. create a new site JSON config
2. add deterministic login selectors if possible
3. define review URL patterns
4. define forbidden success text
5. tune the fill prompt and max step count
6. test with a no-submit sandbox account first

## OrangeHRM Demo Notes

`https://opensource-demo.orangehrmlive.com` is included as a test site config.

That site is useful for validating:

- selector-based login
- AI-assisted menu navigation
- review-session handoff

It is not a perfect representation of a production “fill full form, pause before submit” workflow, but it is sufficient as a smoke-test target.

## Future Extensions

### Explicit Submit Service

Add a separate submit workflow that:

- reconnects to an approved review session
- performs final validation
- clicks submit only after explicit approval
- records the submit audit event

### Confidence and Missing-Field Scoring

Track:

- autofilled fields
- unresolved required fields
- confidence score by source

This lets reviewers focus on ambiguous sections instead of rereading the whole form every time.

### Per-Site Policies

Extend site config with:

- allowed submit button text
- blocked button text
- pre-submit review selectors
- anti-bot wait strategies
- upload rules

## Current Repository Deliverables

This refactor introduces:

- a hybrid automation service
- configuration-driven target sites
- OrangeHRM demo config
- screenshot artifacts for review
- source document ingestion
- persistent review-session handoff

The repo now matches the intended operating model: automate the draft, stop for human review, and keep submission outside the draft-preparation path.
