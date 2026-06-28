import argparse
import asyncio
import json
from pathlib import Path

from automation.config import load_app_config, load_site_config
from automation.models import AutomationRequest
from automation.service import HybridReviewAutomationService


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prepare a browser draft for human review using a Hybrid Playwright + Skyvern flow."
    )
    parser.add_argument(
        "--request",
        default="examples/orangehrm_review_request.json",
        help="Path to the JSON automation request payload.",
    )
    parser.add_argument(
        "--site",
        default=None,
        help="Optional site id override. If omitted, the request payload decides.",
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    request_path = Path(args.request).expanduser().resolve()

    if not request_path.exists():
        raise FileNotFoundError(f"Request file not found: {request_path}")

    request_payload = json.loads(request_path.read_text())
    if args.site:
        request_payload["site_id"] = args.site

    request = AutomationRequest.from_dict(request_payload, request_path.parent)
    app_config = load_app_config()
    site_config = load_site_config(app_config.sites_directory, request.site_id)

    print("Initializing Hybrid Playwright + Skyvern runtime...")
    print(f"Preparing review draft for site: {site_config.site_id}")

    service = HybridReviewAutomationService(app_config=app_config)
    result = await service.prepare_review_draft(site_config=site_config, request=request)

    print("\n=== REVIEW SESSION READY ===")
    print(json.dumps(result.to_dict(), indent=2, sort_keys=True))

    if service.has_open_local_handoff():
        print("\nLocal review browser is open. Press Enter here when you are finished reviewing to close it.")
        try:
            await asyncio.to_thread(input)
        except EOFError:
            print("Input stream closed; shutting down the local review browser automatically.")
        await service.close_local_handoff()


if __name__ == "__main__":
    asyncio.run(main())
