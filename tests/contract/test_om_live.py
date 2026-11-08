"""Live OpenMetadata contract test (needs the compose dev stack)."""

import os

import pytest
from pydantic import SecretStr

from lakeloom.om.client import OMClient

pytestmark = pytest.mark.contract


async def test_fr_om_01_live_get_version() -> None:
    url, token = os.environ.get("OM_URL"), os.environ.get("OM_BOT_TOKEN")
    if not url or not token:
        pytest.skip("OM_URL / OM_BOT_TOKEN not set")
    client = OMClient(url, SecretStr(token))
    try:
        assert (await client.get_version()).version.startswith("2.1")
    finally:
        await client.aclose()
