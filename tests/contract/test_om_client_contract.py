"""OMClient against the real OpenMetadata of the dev stack (FR-OM-08, FR-OM-01)."""

import pytest
from pydantic import SecretStr

from lakeloom.om.client import OMClient

pytestmark = pytest.mark.contract


async def test_fr_om_08_om_client_against_real_om(om_url: str, om_bot_token: str) -> None:
    client = OMClient(om_url, SecretStr(om_bot_token))
    try:
        assert (await client.get_version()).version.startswith("2.1")
    finally:
        await client.aclose()
