"""Minimal async Zabbix JSON-RPC client (Zabbix 7.0, API-token auth)."""
# Standard library imports.
import itertools
import logging
from typing import Any

# HTTP client.
import httpx

logger = logging.getLogger("ZabbixApi")


class ZabbixApiError(Exception):
    """Raised when Zabbix returns a JSON-RPC error."""


class ZabbixApi:
    """Async client for the Zabbix JSON-RPC API using an API token."""
    def __init__(self, url: str, token: str, verify: Any = True, timeout: float = 30.0):
        """Set the endpoint and auth header and create the HTTP client."""
        # JSON-RPC endpoint and bearer token.
        self.endpoint = url.rstrip("/") + "/api_jsonrpc.php"
        self._auth = "Bearer " + token
        # Shared HTTP client with TLS and timeout settings.
        self._client = httpx.AsyncClient(
            verify=verify, timeout=timeout,
            headers={"Content-Type": "application/json-rpc"})
        # Request ids start at 1.
        self._ids = itertools.count(1)

    async def call(self, method: str, params: Any = None) -> Any:
        """Call a JSON-RPC method; return its result or raise ZabbixApiError."""
        # Build the request with the next id.
        body = {"jsonrpc": "2.0", "method": method, "params": params or {}, "id": next(self._ids)}
        # Zabbix 7 rejects apiinfo.version if it carries an auth header.
        headers = None if method == "apiinfo.version" else {"Authorization": self._auth}
        # Send it; HTTP errors raise.
        resp = await self._client.post(self.endpoint, json=body, headers=headers)
        resp.raise_for_status()
        # A JSON-RPC error becomes ZabbixApiError.
        data = resp.json()
        if "error" in data:
            err = data["error"]
            raise ZabbixApiError(f"{method}: {err.get('message')} {err.get('data', '')}".strip())
        return data["result"]

    async def aclose(self) -> None:
        """Close the HTTP client."""
        await self._client.aclose()
