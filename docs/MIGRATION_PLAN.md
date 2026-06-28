# Migration Plan: Skyvern Agent → Deterministic Playwright Fill at Scale

## Decisions locked in

- **Target sites are stable and known.** No LLM in the form-fill path. Per-site
  deterministic Playwright page objects.
- **AI is confined to data extraction** (PDF / source website → normalized JSON).
- **Managed browser cloud** (Browserbase / Steel / Browserless) runs the browser
  fleet to start. We connect Playwright to it over CDP. Revisit self-hosting only
  when per-run cost beats the ops burden.
- **No mandatory human review.** Reliability comes from determinism + field-level
  validation gates + CI canaries, not a human eyeball.

## Target architecture

```
                       ┌──────────────────────────────────────┐
  PDF / source site ─► │ AI EXTRACTION (Claude, structured)    │
                       │  → normalized record JSON (schema'd)  │
                       └───────────────┬──────────────────────┘
                                       │ validate against JSON schema
                                       ▼
  API (FastAPI) ─► Queue (Redis/SQS) ─► Worker pool
                                          │  per job:
                                          │   1. acquire managed-cloud browser
                                          │      session (Browserbase/Steel)
                                          │   2. load saved auth (storage state)
                                          │   3. run per-site page object
                                          │      (deterministic fill + submit)
                                          │   4. field-level post-validation
                                          │   5. capture proof artifacts
                                          ▼
   Postgres (jobs, runs, audit)   +   Object store (screenshots, traces)
   Secrets vault (credentials, never in payloads)
```

Two independent failure domains: extraction can be wrong (cheap, caught by schema
validation before any browser opens) vs. fill can fail (caught by post-submit
validation + retried). Keep them separate.

---

## Phase 0 — Foundations (no behavior change yet)

Goal: stand up the skeleton without ripping out Skyvern.

1. **Add a real API.** FastAPI service with `POST /jobs` (returns job id) and
   `GET /jobs/{id}`. The CLI in `agent.py` becomes a thin client of the same
   service layer.
2. **Add a queue + worker.** Start with Redis + a simple worker (RQ/Dramatiq) or
   SQS + a poller. One worker process for now.
3. **Postgres schema.** Tables: `jobs`, `runs`, `artifacts`, `audit_events`.
   Every job carries an **idempotency key**; re-submitting the same key is a
   no-op that returns the existing job.
4. **Secrets out of payloads.** Move credentials to a vault (AWS Secrets Manager /
   Vault), referenced by `tenant_id` + `account_id`. The request payload carries
   references, never plaintext. (Today they're plaintext JSON files — this is the
   first thing to fix.)
5. **Pin a normalized record schema.** Define one JSON Schema for the canonical
   form record (the union of fields your sites need). Everything downstream
   speaks this schema.

Deliverable: jobs flow API → queue → worker → (still Skyvern) → result, with
audit rows and vault-backed secrets.

## Phase 1 — Deterministic fill for ONE real site

Goal: prove the speed/reliability win on a real target before committing.

1. **Pick your highest-volume site.** Replace its Skyvern `fill`/`navigation`
   config with a hand-written page object:

   ```python
   class AcmeBillingForm:
       def __init__(self, page: Page):
           self.page = page

       async def login(self, creds):  # deterministic selectors
           await self.page.goto(START_URL, wait_until="domcontentloaded")
           await self.page.fill("#username", creds.username)
           await self.page.fill("#password", creds.password)
           await self.page.click("button[type=submit]")
           await self.page.wait_for_url("**/dashboard**")

       async def fill(self, record: NormalizedRecord):
           await self.page.goto(FORM_URL, wait_until="domcontentloaded")
           await self.page.fill("#customer_name", record.customer_name)
           await self.page.select_option("#plan", record.plan_code)
           # ... explicit, one line per field

       async def submit_and_verify(self) -> SubmitResult:
           await self.page.click("#submit")
           await self.page.wait_for_selector(".confirmation-number")
           return SubmitResult(
               confirmation=await self.page.text_content(".confirmation-number")
           )
   ```

2. **Field-level validation gate.** Before clicking submit, read back every field
   you set and assert it equals the record. After submit, assert a positive
   success signal (confirmation number / success URL). If either fails → no
   submit / mark run `needs_attention`, don't silently pass. This gate is what
   replaces human review.
3. **Save auth as storage state.** After first login, persist
   `context.storage_state()` per account to the vault/store; reuse it to skip
   login on subsequent runs (big speedup, fewer bot-detection triggers).
4. **Benchmark** against the Skyvern path: wall-clock per form, success rate over
   ~50 runs, cost per run. This is your go/no-go evidence.

Deliverable: one site filled deterministically end-to-end, measurably faster and
with no AI in the fill path.

## Phase 2 — AI extraction step

Goal: feed the deterministic fillers from messy sources, reliably.

1. **PDF / source-site → normalized record** using Claude with a **fixed output
   schema** (tool-use / structured output). Output MUST validate against the
   Phase 0 JSON Schema; reject + flag on validation failure *before* opening a
   browser.
2. **Confidence + missing-field tracking.** Mark fields the model was unsure
   about. Required-but-missing fields short-circuit the job to `needs_input`
   rather than guessing.
3. Keep this a **pure function**: `(source bytes) → (record JSON, confidence)`.
   Easy to unit-test with fixtures, no browser involved.

Deliverable: source documents become validated normalized records with no human
in the loop for the happy path.

## Phase 3 — Managed browser cloud + concurrency

Goal: scale to hundreds of concurrent users.

1. **Connect Playwright to the managed cloud over CDP** instead of launching
   local Chromium. (Browserbase/Steel/Browserless all expose a CDP endpoint;
   `playwright.chromium.connect_over_cdp(ws_url)`.) The page objects from Phase 1
   are unchanged — only session acquisition changes.
2. **Worker pool sizing is throughput math, not "one browser per user."**
   `concurrent_sessions = arrival_rate × avg_fill_seconds`. A 20s deterministic
   fill at 300 forms/hour needs ~2 concurrent sessions, not 300. Size the cloud
   session pool to peak concurrency, not user count.
3. **Autoscale workers on queue depth.** Containerize workers (official Playwright
   image); run on K8s/ECS Fargate; scale on queue backlog, not CPU.
4. **Per-job isolation = one cloud session per job.** The managed cloud gives you
   clean isolation per session; you don't manage contexts yourself.
5. **Retries + dead-letter.** Bounded retries with backoff on transient failures
   (timeouts, navigation). Terminal failures → DLQ + alert. Idempotency key
   prevents double-submission on retry.

Deliverable: horizontally scalable, hundreds of concurrent forms, cost per run
visible in dashboards.

## Phase 4 — Reliability hardening (so review stays off)

1. **CI canaries.** Run each site's page object against the real site on a
   schedule (e.g. hourly) with a sandbox account, stopping before submit. When a
   site changes its markup, the canary breaks *before* a customer job does. This
   is the safety net that lets you keep humans out of the loop.
2. **Playwright tracing on.** Capture trace + screenshots for every run; store in
   object storage keyed by run id. This is your post-hoc debugging, replacing the
   live-browser handoff.
3. **Per-site circuit breaker.** If a site's success rate drops below threshold,
   auto-pause that site's jobs and alert, rather than burning retries.
4. **Audit every submit.** Immutable `audit_events` row per submission with
   record hash, confirmation id, artifacts pointer.

Deliverable: changes to target sites are caught by canaries, not customers; every
submission is auditable.

## Phase 5 — Decommission Skyvern

- Remove `skyvern` from `requirements.txt`, delete the agent-driven
  `_prepare_form_draft` / `_move_to_review_boundary` / `page.agent.run_task`
  paths in `automation/service.py`.
- Keep the config-driven site model (`config/sites/*.json`) but repoint it at
  page-object class names + selector maps instead of AI prompts.
- Retire `SUBMIT_GUARD_PROMPT`, review-boundary prompt validation, and the
  draft-only framing in the docs.

---

## What carries over from today's code

- The **config-driven site model** is a good bone structure — keep it, change its
  contents from prompts to selectors.
- The **request/result typed models** (`automation/models.py`) are reusable; add
  the normalized-record schema alongside them.
- The **artifact capture** idea is right; move it to object storage + tracing.

## What to delete / stop doing

- LLM-per-action filling (`page.agent.run_task`) — root cause of slow + fragile.
- Plaintext credentials in JSON request files — move to vault now.
- One-process-per-run CLI as the production entrypoint — becomes a dev client.
- Mandatory human-review framing — replaced by validation gates + canaries.

## Language note (Python vs .NET)

Stay in **Python** to reuse the existing stack and keep the AI extraction step in
one place. Playwright .NET is equally capable on the browser side, but it would
split your AI tooling and browser tooling across two runtimes for no benefit given
your current codebase is Python. Choose .NET only if your broader backend is .NET.
