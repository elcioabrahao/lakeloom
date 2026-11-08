"""Guard: every endpoint OMClient uses exists in the committed OM spec (FR-OM-01, §0.4)."""

import json
from importlib import resources

from lakeloom.om.client import OMClient


def test_fr_om_01_client_paths_exist_in_spec() -> None:
    spec_file = resources.files("lakeloom.om") / "openapi" / "openmetadata-2.1.0-rc1.json"
    spec = json.loads(spec_file.read_text(encoding="utf-8"))

    assert OMClient.PATHS
    for name, (method, path) in OMClient.PATHS.items():
        assert path in spec["paths"], f"{name}: {path} missing from spec"
        assert method.lower() in spec["paths"][path], f"{name}: {method} {path} missing"
