"""OpenMetadata integration (FR-OM-*)."""

from lakeloom.om.client import OMClient, OMError, OMUnavailable
from lakeloom.om.models import OMCapabilities, OMVersion, RdfStatus

__all__ = ["OMCapabilities", "OMClient", "OMError", "OMUnavailable", "OMVersion", "RdfStatus"]
