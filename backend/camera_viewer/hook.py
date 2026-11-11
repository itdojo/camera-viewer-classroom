"""The Relay's recording hooks: tell the backend a segment was created or completed.

The Recorder sets ``runOnRecordSegmentCreate`` and ``runOnRecordSegmentComplete``
on every Relay path to

    python -m camera_viewer.hook <backend url> <token> create|complete

The Relay runs it with ``MTX_PATH`` and ``MTX_SEGMENT_PATH`` in the
environment; this posts them to the backend's own listener (``app.hook_url``).
Standard library only, so it starts fast.
"""

from __future__ import annotations

import json
import os
import ssl
import sys
import urllib.request

TOKEN_HEADER = "X-Camera-Viewer-Hook"


def main(argv: list[str]) -> int:
    url, token, event = argv
    body = json.dumps({
        "event": event,
        "path": os.environ.get("MTX_PATH", ""),
        "segment": os.environ.get("MTX_SEGMENT_PATH", ""),
    }).encode()
    request = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json", TOKEN_HEADER: token})
    context = None
    if url.startswith("https://"):
        # The backend's own UI listener, reached on this host's address: its
        # certificate names the instance's public name, not this address, so
        # the hostname can't match. Trust exactly the certificate the UI was
        # given (CV_TLS_CERT, in the environment the Relay passes on) instead.
        context = ssl.create_default_context(cafile=os.environ["CV_TLS_CERT"])
        context.check_hostname = False
        context.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
    with urllib.request.urlopen(request, timeout=30, context=context) as response:
        return 0 if response.status < 300 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
