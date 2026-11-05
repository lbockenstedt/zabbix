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
_SENSITIVE = {"token", "password", "secret", "hub_secret", "snmp_community"}


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
            return {"status": "ERROR", "message": str(e)}

    @staticmethod
    def _mask(data: Any) -> Any:
        """Return a copy of data with sensitive values masked."""
        # Leave non-dict values unchanged.
        if not isinstance(data, dict):
            return data
        # Replace sensitive values with a fixed mask.
        return {k: ("********" if k in _SENSITIVE else v) for k, v in data.items()}

    async def handle_command(self, command_type: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """Commands: UPDATE_CONFIG, GET_VERSION, MON_STATUS, MON_HOSTS,
        MON_SYNC (devices, dry_run), MON_REBUILD (devices; full repopulate)."""
        # Normalize the command; for syncs log only the device count, not the list.
        cmd = (command_type or "").upper()
        logger.info("Handling Zabbix command: %s with data %s", command_type,
                    self._mask(data) if cmd != "MON_SYNC" and cmd != "MON_REBUILD"
                    else {"devices": len((data or {}).get("devices", [])),
                          "dry_run": (data or {}).get("dry_run", False)})
        # Dispatch; any exception becomes an error envelope.
        try:
            res = await self._dispatch(cmd, data if isinstance(data, dict) else {})
        except Exception as e:  # noqa: BLE001 - always answer the hub with an envelope
            res = {"status": "ERROR", "message": str(e)}
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
            self.config = {**(self.config or {}), **data}
            self.engine.set_config(self.config)
            return {"status": "SUCCESS", "message": "zabbix configuration updated from Hub"}
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

