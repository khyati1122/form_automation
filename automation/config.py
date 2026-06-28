import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from skyvern import SkyvernEnvironment

from automation.models import SiteConfig

load_dotenv()


@dataclass(frozen=True)
class AppConfig:
    skyvern_api_url: str
    skyvern_api_key: str
    skyvern_environment: SkyvernEnvironment
    sites_directory: Path
    review_artifact_directory: Path
    default_session_timeout_minutes: int = 60


def _resolve_environment(api_url: str) -> SkyvernEnvironment:
    normalized = api_url.strip().lower()
    if "localhost" in normalized or "127.0.0.1" in normalized:
        return SkyvernEnvironment.STAGING
    return SkyvernEnvironment.CLOUD


def load_app_config() -> AppConfig:
    skyvern_api_url = os.getenv("SKYVERN_API_URL", "http://localhost:8000").strip()
    skyvern_api_key = os.getenv("SKYVERN_API_KEY", "").strip()
    if not skyvern_api_key:
        raise ValueError("SKYVERN_API_KEY is required for the hybrid automation flow.")

    root = Path(__file__).resolve().parent.parent
    return AppConfig(
        skyvern_api_url=skyvern_api_url,
        skyvern_api_key=skyvern_api_key,
        skyvern_environment=_resolve_environment(skyvern_api_url),
        sites_directory=root / "config" / "sites",
        review_artifact_directory=root / "review_artifacts",
        default_session_timeout_minutes=int(os.getenv("SESSION_TIMEOUT_MINUTES", "60")),
    )


def load_site_config(sites_directory: Path, site_id: str) -> SiteConfig:
    config_path = sites_directory / f"{site_id}.json"
    if not config_path.exists():
        raise FileNotFoundError(f"Site configuration not found: {config_path}")
    return SiteConfig.from_json_path(config_path)
