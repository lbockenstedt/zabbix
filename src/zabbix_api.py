"""Minimal async Zabbix JSON-RPC client (Zabbix 7.0, API-token auth)."""
import itertools
import logging
from typing import Any

import httpx

logger = logging.getLogger("ZabbixApi")


class ZabbixApiError(Exception):
    """Raised when Zabbix returns a JSON-RPC error."""


class ZabbixApi:
    def __init__(self, url: str, token: str, verify: Any = True, timeout: float = 30.0):
        self.endpoint = url.rstrip("/") + "/api_jsonrpc.php"
        self._auth = "Bearer " + token
        self._client = httpx.AsyncClient(
            verify=verify, timeout=timeout,
            headers={"Content-Type": "application/json-rpc"})
        self._ids = itertools.count(1)

    async def call(self, method: str, params: Any = None) -> Any:
        body = {"jsonrpc": "2.0", "method": method, "params": params or {}, "id": next(self._ids)}
        # Zabbix 7 rejects apiinfo.version if it carries an auth header.
        headers = None if method == "apiinfo.version" else {"Authorization": self._auth}
        resp = await self._client.post(self.endpoint, json=body, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            err = data["error"]
            raise ZabbixApiError(f"{method}: {err.get('message')} {err.get('data', '')}".strip())
        return data["result"]

    async def aclose(self) -> None:
        await self._client.aclose()
