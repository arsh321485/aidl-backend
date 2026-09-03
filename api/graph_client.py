"""Shared Microsoft Graph helpers (no circular imports)."""

GRAPH_ME_URL = "https://graph.microsoft.com/v1.0/me"
GRAPH_BASE = "https://graph.microsoft.com/v1.0"


def graph_headers(access_token: str) -> dict:
    return {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
