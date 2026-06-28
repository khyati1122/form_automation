from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from skyvern import Skyvern

from automation.config import AppConfig
from automation.models import AutomationRequest, ReviewDraftResult, SiteConfig, SourceDocument

SOURCE_EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "fields": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "value": {"type": "string"},
                    "notes": {"type": "string"},
                },
                "required": ["name", "value"],
            },
        },
    },
    "required": ["summary", "fields"],
}

DEFAULT_URL_SOURCE_PROMPT = (
    "Extract all information from this source page that may help complete a downstream form draft. "
    "Prefer names, dates, addresses, phone numbers, emails, account identifiers, and any explicit facts."
)

SUBMIT_GUARD_PROMPT = (
    "Critical safety rule: never click any final Submit, Save, Confirm, Finish, Place Order, or equivalent "
    "button. Stop only on a human-reviewable page."
)

SESSION_STARTUP_TIMEOUT_SECONDS = 90
SESSION_STARTUP_POLL_INTERVAL_SECONDS = 2


@dataclass
class SourceBundle:
    merged_fields: dict[str, Any]
    summary_lines: list[str]
    missing_information: list[str]

    def as_prompt_context(self) -> str:
        if not self.summary_lines:
            return "No source documents were supplied."
        return "\n".join(self.summary_lines)


class HybridReviewAutomationService:
    def __init__(self, app_config: AppConfig):
        self.app_config = app_config
        self._local_handoff_browser: Any | None = None
        self._local_handoff_skyvern: Skyvern | None = None

    async def prepare_review_draft(self, site_config: SiteConfig, request: AutomationRequest) -> ReviewDraftResult:
        skyvern = Skyvern(
            api_key=self.app_config.skyvern_api_key,
            base_url=self.app_config.skyvern_api_url,
            environment=self.app_config.skyvern_environment,
        )

        use_direct_local_browser = self._should_use_direct_local_browser()
        session = None
        browser = None
        handoff_ready = False
        handoff_session_id = f"local-review-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        handoff_session_url: str | None = None

        try:
            if use_direct_local_browser:
                browser = await self._launch_direct_local_browser(skyvern)
                handoff_session_url = browser.browser_address
            else:
                session = await skyvern.create_browser_session(
                    timeout=request.session.timeout_minutes or self.app_config.default_session_timeout_minutes,
                    browser_profile_id=request.session.browser_profile_id,
                )
                session = await self._wait_for_browser_session_ready(skyvern, session.browser_session_id)
                browser = await self._connect_to_browser_session(skyvern, session)
                handoff_session_id = session.browser_session_id
                handoff_session_url = session.app_url

            page = await browser.get_working_page()

            await page.goto(site_config.start_url, wait_until="domcontentloaded")

            if site_config.login:
                await self._login(page, site_config, request.credentials)

            source_bundle = await self._resolve_source_bundle(browser, request)

            await self._navigate_to_form(page, site_config)
            await self._prepare_form_draft(page, site_config, request, source_bundle)
            await self._move_to_review_boundary(page, site_config)
            await self._validate_review_boundary(page, site_config)

            screenshot_path = await self._capture_review_artifact(page, site_config.site_id, handoff_session_id)

            handoff_ready = True
            if use_direct_local_browser:
                self._local_handoff_browser = browser
                self._local_handoff_skyvern = skyvern

            return ReviewDraftResult(
                site_id=site_config.site_id,
                review_required=True,
                session_id=handoff_session_id,
                session_url=handoff_session_url,
                current_url=page.url,
                page_title=await page.title(),
                screenshot_path=str(screenshot_path),
                missing_information=source_bundle.missing_information,
                notes=self._build_handoff_notes(use_direct_local_browser),
                source_summary=source_bundle.as_prompt_context(),
            )
        finally:
            if use_direct_local_browser and handoff_ready:
                pass
            elif use_direct_local_browser and browser is not None:
                try:
                    await browser.close()
                except Exception:
                    pass

            if not handoff_ready and session is not None:
                try:
                    await skyvern.close_browser_session(session.browser_session_id)
                except Exception:
                    pass
            if not (use_direct_local_browser and handoff_ready):
                await skyvern.aclose()

    def has_open_local_handoff(self) -> bool:
        return self._local_handoff_browser is not None and self._local_handoff_skyvern is not None

    async def close_local_handoff(self) -> None:
        browser = self._local_handoff_browser
        skyvern = self._local_handoff_skyvern
        self._local_handoff_browser = None
        self._local_handoff_skyvern = None

        if browser is not None:
            try:
                await browser.close()
            except Exception:
                pass

        if skyvern is not None:
            await skyvern.aclose()

    async def _wait_for_browser_session_ready(self, skyvern: Skyvern, browser_session_id: str) -> Any:
        deadline = time.monotonic() + SESSION_STARTUP_TIMEOUT_SECONDS
        last_status = "unknown"

        while True:
            session = await skyvern.get_browser_session(browser_session_id)
            if session.browser_address:
                return session
            if self._uses_local_skyvern_backend() and session.started_at is not None:
                return session

            if session.completed_at is not None:
                raise RuntimeError(
                    f"Browser session {browser_session_id} completed before it became connectable "
                    f"(status={session.status or 'unknown'})."
                )

            last_status = session.status or last_status
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Timed out waiting for browser session {browser_session_id} to become connectable "
                    f"after {SESSION_STARTUP_TIMEOUT_SECONDS} seconds (last status={last_status})."
                )

            await asyncio.sleep(SESSION_STARTUP_POLL_INTERVAL_SECONDS)

    async def _connect_to_browser_session(self, skyvern: Skyvern, session: Any) -> Any:
        if session.browser_address:
            return await skyvern.connect_to_cloud_browser_session(session.browser_session_id)

        if self._uses_local_skyvern_backend():
            return await self._connect_via_local_cdp_proxy(skyvern, session.browser_session_id)

        raise RuntimeError(
            f"Browser session {session.browser_session_id} became ready without a connectable browser address."
        )

    async def _connect_via_local_cdp_proxy(self, skyvern: Skyvern, browser_session_id: str) -> Any:
        from skyvern.library.skyvern_browser import SkyvernBrowser
        from skyvern.webeye.cdp_connection import prepare_persistent_browser_cdp_connect

        cdp_url = os.getenv("BROWSER_REMOTE_DEBUGGING_URL", "http://127.0.0.1:9222").strip()
        connect_url, headers = prepare_persistent_browser_cdp_connect(
            cdp_url,
            browser_session_id=browser_session_id,
            x_api_key=self.app_config.skyvern_api_key,
        )

        try:
            playwright = await skyvern._get_playwright()
            browser = await playwright.chromium.connect_over_cdp(connect_url, headers=headers)
        except Exception as exc:
            raise RuntimeError(
                "Skyvern created a local browser session, but this process could not attach to its CDP endpoint. "
                f"Tried {connect_url!r} for session {browser_session_id}. "
                "If you are using a self-hosted local Skyvern backend, set BROWSER_REMOTE_DEBUGGING_URL to a "
                "host-reachable CDP proxy URL or use a deployment that returns browser_address on the session API."
            ) from exc

        browser_context = browser.contexts[0] if browser.contexts else await browser.new_context()
        return SkyvernBrowser(
            skyvern,
            browser_context,
            browser_session_id=browser_session_id,
            browser_address=connect_url,
        )

    async def _launch_direct_local_browser(self, skyvern: Skyvern) -> Any:
        local_cdp_port = int(os.getenv("LOCAL_REVIEW_CDP_PORT", "9333"))
        return await skyvern.launch_local_browser(
            headless=False,
            port=local_cdp_port,
        )

    def _uses_local_skyvern_backend(self) -> bool:
        host = urlsplit(self.app_config.skyvern_api_url).hostname or ""
        return host in {"127.0.0.1", "localhost"}

    def _should_use_direct_local_browser(self) -> bool:
        mode = os.getenv("SKYVERN_LOCAL_REVIEW_MODE", "direct").strip().lower()
        return self._uses_local_skyvern_backend() and mode != "persistent"

    def _build_handoff_notes(self, use_direct_local_browser: bool) -> list[str]:
        notes = [
            "Submission was intentionally blocked by prompt guardrails and review-boundary validation.",
        ]
        if use_direct_local_browser:
            notes.append("Local review mode is active. This browser stays open until you close the CLI prompt.")
        else:
            notes.append("Reconnect to the same browser session for human review or a future explicit submit step.")
        return notes

    async def _login(self, page: Any, site_config: SiteConfig, credentials: dict[str, Any]) -> None:
        assert site_config.login is not None

        username = credentials.get(site_config.login.username_key)
        password = credentials.get(site_config.login.password_key)
        if not username or not password:
            raise ValueError(
                f"Missing login credentials. Required keys: "
                f"{site_config.login.username_key}, {site_config.login.password_key}"
            )

        await page.locator(site_config.login.username_selector).fill(str(username))
        await page.locator(site_config.login.password_selector).fill(str(password))
        await page.locator(site_config.login.submit_selector).click()

        if site_config.login.success_url_patterns:
            await self._wait_for_any_url_pattern(page, site_config.login.success_url_patterns, timeout_ms=30000)

        if site_config.login.success_validation_prompt:
            is_valid = await page.validate(site_config.login.success_validation_prompt)
            if not is_valid:
                raise RuntimeError("Login validation failed for the configured target site.")

    async def _navigate_to_form(self, page: Any, site_config: SiteConfig) -> None:
        if site_config.navigation.form_url:
            await page.goto(site_config.navigation.form_url, wait_until="domcontentloaded")
            return

        if site_config.navigation.form_navigation_prompt:
            await page.agent.run_task(
                prompt=f"{site_config.navigation.form_navigation_prompt}\n{SUBMIT_GUARD_PROMPT}",
                max_steps=10,
                title=f"{site_config.site_id}:navigate-to-form",
            )

    async def _prepare_form_draft(
        self,
        page: Any,
        site_config: SiteConfig,
        request: AutomationRequest,
        source_bundle: SourceBundle,
    ) -> None:
        if not site_config.fill:
            return

        source_data = dict(source_bundle.merged_fields)
        source_data.update(request.source_data)
        source_data.update(request.target_payload)

        prompt = site_config.fill.task_prompt_template.format(
            source_data_json=json.dumps(source_data, indent=2, sort_keys=True, ensure_ascii=True),
            source_context=source_bundle.as_prompt_context(),
            field_hints_json=json.dumps(site_config.fill.field_hints, indent=2, sort_keys=True, ensure_ascii=True),
        )

        guarded_prompt = f"{prompt}\n{SUBMIT_GUARD_PROMPT}"

        await page.agent.run_task(
            prompt=guarded_prompt,
            max_steps=site_config.fill.max_steps,
            title=f"{site_config.site_id}:prepare-draft",
        )

    async def _move_to_review_boundary(self, page: Any, site_config: SiteConfig) -> None:
        if not site_config.navigation.review_handoff_prompt:
            return

        await page.agent.run_task(
            prompt=f"{site_config.navigation.review_handoff_prompt}\n{SUBMIT_GUARD_PROMPT}",
            max_steps=8,
            title=f"{site_config.site_id}:review-handoff",
        )

    async def _validate_review_boundary(self, page: Any, site_config: SiteConfig) -> None:
        if site_config.review.url_patterns and not self._matches_any_pattern(page.url, site_config.review.url_patterns):
            raise RuntimeError(f"Review boundary validation failed. Current URL {page.url!r} did not match config.")

        body_text = await page.text_content("body") or ""
        for forbidden in site_config.review.forbidden_text:
            if forbidden.lower() in body_text.lower():
                raise RuntimeError(f"Detected forbidden post-submit text on the page: {forbidden}")

        if site_config.review.validation_prompt:
            is_valid = await page.validate(site_config.review.validation_prompt)
            if not is_valid:
                raise RuntimeError("AI validation determined the page is not safe for review handoff.")

        for selector in site_config.review.submit_selectors:
            locator = page.locator(selector).first
            if await locator.count():
                await locator.evaluate(
                    """element => {
                        element.style.outline = "3px solid #d97706";
                        element.setAttribute("data-review-boundary", "true");
                    }"""
                )

    async def _resolve_source_bundle(self, browser: Any, request: AutomationRequest) -> SourceBundle:
        merged_fields: dict[str, Any] = dict(request.source_data)
        summary_lines: list[str] = []
        missing_information: list[str] = []

        for document in request.source_documents:
            if document.doc_type == "text":
                if document.content:
                    summary_lines.append(f"{document.label}: {document.content}")
                continue

            if document.doc_type == "file":
                if not document.path or not document.path.exists():
                    missing_information.append(f"Missing source file: {document.label}")
                    continue
                file_payload = self._load_file_payload(document.path)
                if isinstance(file_payload, dict):
                    merged_fields.update(file_payload)
                    summary_lines.append(f"{document.label}: loaded structured data from {document.path.name}.")
                else:
                    summary_lines.append(f"{document.label}: {file_payload}")
                continue

            if document.doc_type == "url":
                extracted = await self._extract_from_url(browser, document)
                if extracted:
                    summary_lines.append(f"{document.label}: {extracted['summary']}")
                    for item in extracted.get("fields", []):
                        merged_fields[item["name"]] = item["value"]
                else:
                    missing_information.append(f"Unable to extract source website: {document.label}")
                continue

            missing_information.append(f"Unsupported source document type: {document.doc_type}")

        return SourceBundle(
            merged_fields=merged_fields,
            summary_lines=summary_lines,
            missing_information=missing_information,
        )

    async def _extract_from_url(self, browser: Any, document: SourceDocument) -> dict[str, Any] | None:
        if not document.url:
            return None

        page = await browser.new_page()
        try:
            await page.goto(document.url, wait_until="domcontentloaded")
            result = await page.extract(
                prompt=document.extraction_prompt or DEFAULT_URL_SOURCE_PROMPT,
                schema=SOURCE_EXTRACTION_SCHEMA,
            )
            if isinstance(result, dict):
                return result
            return None
        finally:
            await page.close()

    async def _capture_review_artifact(self, page: Any, site_id: str, session_id: str) -> Path:
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        artifact_dir = self.app_config.review_artifact_directory / site_id
        artifact_dir.mkdir(parents=True, exist_ok=True)
        screenshot_path = artifact_dir / f"{timestamp}-{session_id}.png"
        await page.screenshot(path=str(screenshot_path), full_page=True)
        return screenshot_path

    def _load_file_payload(self, path: Path) -> dict[str, Any] | str:
        if path.suffix.lower() == ".json":
            return json.loads(path.read_text())
        return path.read_text().strip()

    def _matches_any_pattern(self, url: str, patterns: list[str]) -> bool:
        return any(pattern in url for pattern in patterns)

    async def _wait_for_any_url_pattern(self, page: Any, patterns: list[str], timeout_ms: int) -> None:
        deadline = asyncio.get_running_loop().time() + (timeout_ms / 1000)
        while asyncio.get_running_loop().time() < deadline:
            if self._matches_any_pattern(page.url, patterns):
                return
            await page.wait_for_timeout(250)
        raise RuntimeError(f"Timed out waiting for URL to match one of the configured patterns: {patterns}")
