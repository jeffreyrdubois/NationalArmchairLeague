"""Docker HEALTHCHECK probe.

Dependency-free on purpose: the slim image has no curl, and adding one just for
this would be silly. Uses only the standard library, so it runs identically
inside the image and under `docker exec`:

    docker exec nal python /app/healthcheck.py; echo $?
"""

import os
import sys
import urllib.request

port = os.environ.get("PORT", "8000")

try:
    with urllib.request.urlopen(
        f"http://127.0.0.1:{port}/health", timeout=4
    ) as response:
        sys.exit(0 if response.status == 200 else 1)
except Exception:
    sys.exit(1)
