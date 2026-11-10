"""Regenerate docs/api/openapi.json from the app (FR-PUB-08).

Usage: uv run python scripts/gen_openapi.py
The drift test (tests/unit/server/test_http_api.py) fails when the committed file differs.
"""

import json
import tempfile
from pathlib import Path

from lakeloom.config import Settings
from lakeloom.server.http_app import build_deps
from lakeloom.server.mcp_app import create_server_app

OUT = Path(__file__).resolve().parents[1] / "docs" / "api" / "openapi.json"


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        settings = Settings(  # pyright: ignore[reportCallIssue]
            om_url="http://om.invalid",  # pyright: ignore[reportArgumentType]
            oidc_issuer="https://idp.invalid",
            public_url="https://lakeloom.invalid",  # pyright: ignore[reportArgumentType]
            db_url=f"sqlite:///{tmp}/openapi.db",
            data_dir=Path(tmp),
        )
        app = create_server_app(build_deps(settings))
        OUT.write_text(json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
