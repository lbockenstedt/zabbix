"""Zabbix spoke: turns hub MON_* commands into Zabbix engine calls.

Masks credentials in logs, always answers the hub with a status envelope and
logs each command's outcome.
"""
# Standard library imports.
import logging
from typing import Dict, Any

# Base spoke (core package or flat layout) and the Zabbix engine.
try:
    from core.src.base_spoke import BaseSpoke
except ImportError:
    from base_spoke import BaseSpoke
from zabbix_engine import ZabbixEngine

logger = logging.getLogger("ZabbixSpoke")

# Credential fields are fully masked in logs.
_SENSITIVE = {
    "token", "password", "secret", "hub_secret", "snmp_community",
    "api_token", "community", "privkey", "private_key", "bearer", "auth"
}

# Commands that change Zabbix. Only the sync commands have a dry run that
# writes nothing; registering a proxy or an agent always writes.
_WRITE_COMMANDS = {"MON_PROXY_REGISTER", "MON_AGENT_REGISTER", "MON_SYNC", "MON_REBUILD"}
_DRY_RUN_COMMANDS = {"MON_SYNC", "MON_REBUILD"}
READ_ONLY_MESSAGE = "Zabbix is read-only on this environment; only reads and sync dry runs are allowed"
# The hub's read-only switch and the time it was saved (see _drop_older_read_only).
_READ_ONLY_KEYS = ("read_only", "read_only_saved_at")


class ZabbixSpoke(BaseSpoke):
    """Zabbix monitoring spoke. Turns hub MON_* commands into Zabbix API work.

    The hub pushes the Zabbix connection and sync settings with UPDATE_CONFIG;
    the device list arrives with each MON_SYNC.
    """

    def __init__(self, spoke_id: str, config: Dict[str, Any]):
        """Create the engine from the config and finish base spoke setup."""
        self.engine = ZabbixEngine(config if isinstance(config, dict) else {})
        super().__init__(spoke_id, config)

    async def get_status(self) -> Dict[str, Any]:
        """Return SUCCESS with Zabbix status, or ERROR if it can't be reached."""
        try:
            return {"status": "SUCCESS", **await self.engine.status()}
        except Exception as e:  # noqa: BLE001
            sanitized = self.engine._sanitize_err(str(e))
            logger.debug("Zabbix get_status failed: %s", sanitized)
            return {"status": "ERROR", "message": sanitized}

    @staticmethod
    def _mask(data: Any) -> Any:
        """Recursively redact sensitive credential keys from dicts and lists."""
        if isinstance(data, dict):
            out = {}
            for k, v in data.items():
                k_lower = str(k).lower()
                if (k_lower in _SENSITIVE or
                        any(k_lower.endswith(sfx) for sfx in ("_secret", "_password", "_token", "_key", "password", "secret", "token", "community"))):
                    out[k] = "********"
                else:
                    out[k] = ZabbixSpoke._mask(v)
            return out
        elif isinstance(data, list):
            return [ZabbixSpoke._mask(item) for item in data]
        return data

    def _drop_older_read_only(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Return ``data`` without the read-only switch when the hub saved it
        before the switch already applied.

        The hub stamps the switch with the time it was saved
        (``read_only_saved_at``). An older value can still arrive: the hub's
        mailbox replays a push the spoke did not answer in time, and a
        reconnect sends the current config before that backlog. Dropping it
        means a replay never undoes a newer switch. A push without the stamp
        (an older hub) applies as before."""
        if not isinstance(data, dict):
            return {}
        # Compare the push's stamp with the one applied last.
        sent = data.get("read_only_saved_at")
        applied = (self.config or {}).get("read_only_saved_at")
        if not (isinstance(sent, (int, float)) and isinstance(applied, (int, float))
                and sent < applied):
            return data
        # Older: keep the rest of the push, drop the switch.
        logger.info("Ignored an older Zabbix read-only value from the hub (saved at %s; "
                    "the applied one was saved at %s)", sent, applied)
        return {k: v for k, v in data.items() if k not in _READ_ONLY_KEYS}

    async def handle_command(self, command_type: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """Commands: UPDATE_CONFIG, GET_VERSION, MON_STATUS, MON_HOSTS,
        MON_SYNC (devices, dry_run), MON_REBUILD (devices; full repopulate)."""
        # Ensure data is treated safely before accessing .get()
        safe_data = data if isinstance(data, dict) else {}
        cmd = str(command_type or "").upper()
        logger.info("Handling Zabbix command: %s with data %s", command_type,
                    self._mask(safe_data) if cmd not in ("MON_SYNC", "MON_REBUILD")
                    else {"devices": len(safe_data.get("devices", []) if isinstance(safe_data.get("devices"), list) else []),
                          "dry_run": safe_data.get("dry_run", False)})
        # Dispatch; any exception becomes an error envelope.
        try:
            res = await self._dispatch(cmd, safe_data)
        except Exception as e:  # noqa: BLE001 - always answer the hub with an envelope
            sanitized = self.engine._sanitize_err(str(e))
            logger.error("Zabbix handle_command failed for %s: %s", command_type, sanitized)
            res = {"status": "ERROR", "message": sanitized}
        # Log the outcome and return the result.
        self._log_result(command_type, res)
        return res

    @staticmethod
    def _log_result(command_type: str, res: Dict[str, Any]) -> None:
        """INFO on success, ERROR (containing "error") on failure so it shows
        in the hub's error-log view."""
        # Read status and any per-part errors.
        status = str(res.get("status", "")).upper()
        errors = res.get("errors") or []
        # Errors log as ERROR (with up to three of them); everything else as INFO.
        if status == "ERROR" or errors:
            logger.error("zabbix command %s result: error - %s%s", command_type,
                         res.get("message") or "failed",
                         f" ({len(errors)} sub-error(s): {errors[:3]})" if errors else "")
        else:
            logger.info("zabbix command %s result: %s", command_type, status.lower() or "ok")

    async def _dispatch(self, cmd: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """Run one normalized command and return its result envelope."""
        if cmd == "UPDATE_CONFIG":
            # Merge: the hub's pushed config may omit the role-load config (url/token).
            # read_only merges like any other key, unless it is older than the
            # one applied. The hub sends it in the UPDATE_CONFIG it pushes each
            # time this spoke connects; the spoke has no background loops, so it
            # only writes when the hub asks. No await here: one push applies at a time.
            self.config = {**(self.config or {}), **self._drop_older_read_only(data)}
            self.engine.set_config(self.config)
            reply = {"status": "SUCCESS", "message": "zabbix configuration updated from Hub"}
            # Echo the flag in force (the newer one if this push was older), so
            # the hub knows this spoke understood it.
            if "read_only" in data:
                reply["read_only"] = self.engine.read_only
            return reply
        # Read-only (a Zabbix shared with another environment): refuse writes
        # with one clear message instead of one error per host. The message is
        # also the one entry in "errors", the list a sync reply carries, so a
        # hub that judges a sync by its errors records the refusal as failed.
        dry_run = cmd in _DRY_RUN_COMMANDS and bool(data.get("dry_run"))
        if self.engine.read_only and cmd in _WRITE_COMMANDS and not dry_run:
            return {"status": "ERROR", "message": READ_ONLY_MESSAGE, "errors": [READ_ONLY_MESSAGE]}
        # Version query.
        if cmd in ("GET_VERSION", "GET-VERSION"):
            return {"status": "SUCCESS", "version": self.get_version()}
        # Read-only status and host list.
        if cmd == "MON_STATUS":
            return {"status": "SUCCESS", **await self.engine.status()}
        if cmd == "MON_HOSTS":
            return {"status": "SUCCESS", **await self.engine.hosts()}
        # Register a proxy; a name is required.
        if cmd == "MON_PROXY_REGISTER":
            name = str(data.get("name") or "").strip()
            if not name:
                return {"status": "ERROR", "message": "name required"}
            return {"status": "SUCCESS", **await self.engine.register_proxy(name, str(data.get("tls_subject") or ""))}
        # Register an agent host; tenant and hostname are required.
        if cmd == "MON_AGENT_REGISTER":
            tenant = str(data.get("tenant") or "").strip()
            hostname = str(data.get("hostname") or "").strip()
            if not (tenant and hostname):
                return {"status": "ERROR", "message": "tenant and hostname required"}
            return {"status": "SUCCESS", **await self.engine.register_agent_host(tenant, hostname, str(data.get("tls_subject") or ""))}
        # Sync or rebuild from the given device list; any error marks the result ERROR.
        if cmd in ("MON_SYNC", "MON_REBUILD"):
            devices = data.get("devices")
            if not isinstance(devices, list):
                return {"status": "ERROR", "message": "devices list required"}
            result = await self.engine.sync(devices, dry_run=bool(data.get("dry_run")))
            return {"status": "ERROR" if result["errors"] else "SUCCESS", **result}
        # Unknown command.
        return {"status": "ERROR", "message": f"unknown command {cmd or '<empty>'}"}

