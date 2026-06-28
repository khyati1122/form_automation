from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


@dataclass(frozen=True)
class LoginConfig:
    username_selector: str
    password_selector: str
    submit_selector: str
    username_key: str = "username"
    password_key: str = "password"
    success_url_patterns: list[str] = field(default_factory=list)
    success_validation_prompt: str | None = None

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "LoginConfig":
        return cls(
            username_selector=payload["username_selector"],
            password_selector=payload["password_selector"],
            submit_selector=payload["submit_selector"],
            username_key=payload.get("username_key", "username"),
            password_key=payload.get("password_key", "password"),
            success_url_patterns=list(payload.get("success_url_patterns", [])),
            success_validation_prompt=payload.get("success_validation_prompt"),
        )


@dataclass(frozen=True)
class NavigationConfig:
    form_url: str | None = None
    form_navigation_prompt: str | None = None
    review_handoff_prompt: str | None = None

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "NavigationConfig":
        return cls(
            form_url=payload.get("form_url"),
            form_navigation_prompt=payload.get("form_navigation_prompt"),
            review_handoff_prompt=payload.get("review_handoff_prompt"),
        )


@dataclass(frozen=True)
class FillConfig:
    task_prompt_template: str
    max_steps: int = 25
    field_hints: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "FillConfig":
        return cls(
            task_prompt_template=payload["task_prompt_template"],
            max_steps=int(payload.get("max_steps", 25)),
            field_hints=dict(payload.get("field_hints", {})),
        )


@dataclass(frozen=True)
class ReviewConfig:
    url_patterns: list[str] = field(default_factory=list)
    validation_prompt: str | None = None
    forbidden_text: list[str] = field(default_factory=list)
    submit_selectors: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ReviewConfig":
        return cls(
            url_patterns=list(payload.get("url_patterns", [])),
            validation_prompt=payload.get("validation_prompt"),
            forbidden_text=list(payload.get("forbidden_text", [])),
            submit_selectors=list(payload.get("submit_selectors", [])),
        )


@dataclass(frozen=True)
class SiteConfig:
    site_id: str
    label: str
    start_url: str
    login: LoginConfig | None = None
    navigation: NavigationConfig = field(default_factory=NavigationConfig)
    fill: FillConfig | None = None
    review: ReviewConfig = field(default_factory=ReviewConfig)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "SiteConfig":
        return cls(
            site_id=payload["site_id"],
            label=payload["label"],
            start_url=payload["start_url"],
            login=LoginConfig.from_dict(payload["login"]) if payload.get("login") else None,
            navigation=NavigationConfig.from_dict(payload.get("navigation", {})),
            fill=FillConfig.from_dict(payload["fill"]) if payload.get("fill") else None,
            review=ReviewConfig.from_dict(payload.get("review", {})),
        )

    @classmethod
    def from_json_path(cls, path: Path) -> "SiteConfig":
        return cls.from_dict(_read_json(path))


@dataclass(frozen=True)
class SourceDocument:
    doc_type: str
    label: str
    content: str | None = None
    path: Path | None = None
    url: str | None = None
    extraction_prompt: str | None = None

    @classmethod
    def from_dict(cls, payload: dict[str, Any], base_dir: Path) -> "SourceDocument":
        raw_type = payload["type"]
        raw_path = payload.get("path")
        path = (base_dir / raw_path).resolve() if raw_path else None
        return cls(
            doc_type=raw_type,
            label=payload.get("label", raw_type),
            content=payload.get("content"),
            path=path,
            url=payload.get("url"),
            extraction_prompt=payload.get("extraction_prompt"),
        )


@dataclass(frozen=True)
class ReviewSessionRequest:
    timeout_minutes: int | None = None
    browser_profile_id: str | None = None

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ReviewSessionRequest":
        return cls(
            timeout_minutes=payload.get("timeout_minutes"),
            browser_profile_id=payload.get("browser_profile_id"),
        )


@dataclass(frozen=True)
class AutomationRequest:
    site_id: str
    credentials: dict[str, Any]
    source_data: dict[str, Any] = field(default_factory=dict)
    target_payload: dict[str, Any] = field(default_factory=dict)
    source_documents: list[SourceDocument] = field(default_factory=list)
    session: ReviewSessionRequest = field(default_factory=ReviewSessionRequest)

    @classmethod
    def from_dict(cls, payload: dict[str, Any], base_dir: Path) -> "AutomationRequest":
        return cls(
            site_id=payload["site_id"],
            credentials=dict(payload.get("credentials", {})),
            source_data=dict(payload.get("source_data", {})),
            target_payload=dict(payload.get("target_payload", {})),
            source_documents=[
                SourceDocument.from_dict(item, base_dir) for item in payload.get("source_documents", [])
            ],
            session=ReviewSessionRequest.from_dict(payload.get("session", {})),
        )


@dataclass(frozen=True)
class ReviewDraftResult:
    site_id: str
    review_required: bool
    session_id: str
    session_url: str | None
    current_url: str
    page_title: str
    screenshot_path: str
    missing_information: list[str]
    notes: list[str]
    source_summary: str
    next_action: str = "Human review is required before any submission path may be triggered."

    def to_dict(self) -> dict[str, Any]:
        return {
            "site_id": self.site_id,
            "review_required": self.review_required,
            "session_id": self.session_id,
            "session_url": self.session_url,
            "current_url": self.current_url,
            "page_title": self.page_title,
            "screenshot_path": self.screenshot_path,
            "missing_information": self.missing_information,
            "notes": self.notes,
            "source_summary": self.source_summary,
            "next_action": self.next_action,
        }
