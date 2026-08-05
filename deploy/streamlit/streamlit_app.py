"""Secret-free Streamlit Community Cloud entrypoint for the M18 judge release."""

# ruff: noqa: E402, I001 -- hosted-mode variables must precede the app import.

from __future__ import annotations

import os


_HOSTED_DEMO_ENVIRONMENT = {
    "SCHEMABRIDGE_ENVIRONMENT": "hosted-demo",
    "SCHEMABRIDGE_AUTH_MODE": "local-demo",
    "SCHEMABRIDGE_CATALOG_MODE": "recorded",
    "SCHEMABRIDGE_PUBLICATION_MODE": "fake",
    "SCHEMABRIDGE_JUDGE_EXECUTION": "recorded",
    "SCHEMABRIDGE_LOCAL_WORKSPACE": "public-judge-demo",
    "SCHEMABRIDGE_LOCAL_ROLES": '["analyst","publisher"]',
    "SCHEMABRIDGE_DRAFT_STORE_PATH": "/tmp/schemabridge-streamlit.db",
    "SCHEMABRIDGE_RELEASE_REF": "c5817af6d01b8a98cd7f1950d57e1be667614696",
}

for variable, value in _HOSTED_DEMO_ENVIRONMENT.items():
    os.environ[variable] = value

from schemabridge.entrypoints.streamlit.app import main


main()
