"""Public service locations used by compress."""

from __future__ import annotations

import os


API_ENDPOINT = os.environ.get(
    "COMPRESS_ENDPOINT", "https://api.everestagi.com/v1"
).rstrip("/")
DOWNLOAD_BASE_URL = os.environ.get(
    "COMPRESS_DOWNLOAD_BASE_URL",
    "https://raw.githubusercontent.com/spenmcke/compress/refs/heads/main/releases",
).rstrip("/")
