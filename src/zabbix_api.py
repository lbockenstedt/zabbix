"""Minimal async Zabbix JSON-RPC client (Zabbix 7.0, API-token auth)."""
# Standard library imports.
import asyncio
import itertools
import logging
from typing import Any, Callable, Optional, Set

# HTTP client.
import httpx

logger = logging.getLogger("ZabbixApi")


class ZabbixApiError(Exception):
    """Raised when Zabbix returns a JSON-RPC error."""


class ZabbixReadOnlyError(ZabbixApiError):
    """A Zabbix write was refused or stopped: this environment is read-only,
    or the client was built before the spoke's Zabbix URL, token or TLS
    setting last changed (see ``ZabbixEngine._write_refusal``). The message
    says which. A refused write was never sent. A stopped one was still on
    its way when the setting changed (see ``ZabbixApi._send_write``).

    A dev or QA environment that shares another environment's Zabbix runs
    read-only (hub System → Sync)."""


def _is_read(method: str) -> bool:
    """True for JSON-RPC methods that never change Zabbix."""
    return method.endswith(".get") or method == "apiinfo.version"


class ZabbixApi:
    """Async client for the Zabbix JSON-RPC API using an API token."""
    def __init__(self, url: str, token: str, verify: Any = True, timeout: float = 30.0,
                 write_refusal: Optional[Callable[[], str]] = None,
                 writes: Optional[Set["asyncio.Task"]] = None,
                 engine_cancelled: Optional[Set[int]] = None):
        """Set the endpoint and auth header and create the HTTP client.
        ``write_refusal`` (a callable) returns why writes are refused now, or
        "" to allow them. It is asked before every write, so a sync that still
        holds this client stops writing as soon as read-only is switched on.
        ``writes`` is the engine's set of writes still on their way, so the
        engine can stop those too (``ZabbixEngine.set_config``)."""
        # JSON-RPC endpoint and bearer token.
        self.endpoint = url.rstrip("/") + "/api_jsonrpc.php"
        self._auth = "Bearer " + token
        self._write_refusal = write_refusal
        self._writes = set() if writes is None else writes
        self._engine_cancelled = set() if engine_cancelled is None else engine_cancelled
        # Shared HTTP client with TLS and timeout settings.
        self._client = httpx.AsyncClient(
            verify=verify, timeout=timeout,
            headers={"Content-Type": "application/json-rpc"})
        # Request ids start at 1.
        self._ids = itertools.count(1)

    async def call(self, method: str, params: Any = None) -> Any:
        """Call a JSON-RPC method; return its result or raise ZabbixApiError."""
        # Only reads reach Zabbix while the engine refuses writes.
        write = self._write_refusal is not None and not _is_read(method)
        if write:
            reason = self._write_refusal()
            if reason:
                raise ZabbixReadOnlyError(f"{reason}: {method} refused")
        # Build the request with the next id.
        body = {"jsonrpc": "2.0", "method": method, "params": params or {}, "id": next(self._ids)}
        # Zabbix 7 rejects apiinfo.version if it carries an auth header.
        headers = None if method == "apiinfo.version" else {"Authorization": self._auth}
        # Send it (a write so the engine can stop it); HTTP errors raise.
        post = self._client.post(self.endpoint, json=body, headers=headers)
        resp = await (self._send_write(method, post) if write else post)
        resp.raise_for_status()
        # A JSON-RPC error becomes ZabbixApiError.
        data = resp.json()
        if "error" in data:
            err = data["error"]
            raise ZabbixApiError(f"{method}: {err.get('message')} {err.get('data', '')}".strip())
        return data["result"]

    async def _send_write(self, method: str, post) -> httpx.Response:
        """Send a write (the ``post`` coroutine) from its own task, kept in
        ``_writes`` while it runs, so the engine can stop it when read-only is
        switched on or the connection changes.

        Passing the write check is not sending: httpx can first wait for a
        pooled connection, DNS, or the TCP and TLS handshake. A write stopped
        during that wait is never sent. One stopped later was already sent,
        and Zabbix may still apply it. Either way it raises
        ZabbixReadOnlyError, so the sync reports it as failed."""
        task = asyncio.ensure_future(post)
        self._writes.add(task)
        try:
            return await task
        except asyncio.CancelledError:
            # Stopped by the engine. When our own caller is being cancelled,
            # the cancellation goes on up instead.
            curr = asyncio.current_task()
            is_cancelling = curr.cancelling() if (curr and hasattr(curr, "cancelling")) else False
            is_engine = id(task) in self._engine_cancelled
            if is_engine or (task.cancelled() and not is_cancelling and hasattr(curr, "cancelling")):
                reason = self._write_refusal() or "The Zabbix settings changed"
                raise ZabbixReadOnlyError(f"{reason}: {method} stopped") from None
            raise
        finally:
            self._writes.discard(task)
            self._engine_cancelled.discard(id(task))

    async def aclose(self) -> None:
        """Close the HTTP client."""
        await self._client.aclose()
