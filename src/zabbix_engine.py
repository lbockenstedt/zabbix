"""Zabbix engine: owns the API client and runs inventory syncs."""
import logging
from typing import Any, Dict, List

from zabbix_api import ZabbixApi
from zabbix_sync import HostSync

logger = logging.getLogger("ZabbixEngine")


class ZabbixEngine:
    def __init__(self, config: Dict[str, Any] = None):
        self.config: Dict[str, Any] = {}
        self._api = None
        self.last_sync: Dict[str, Any] = {}
        self.set_config(config or {})

    def set_config(self, config: Dict[str, Any]) -> None:
        """Config keys: url, token, verify_tls, plus sync settings
        (stale_days, delete_days, max_age_out_pct, templates, tenant_proxies)."""
        self.config = config
        self._api = None  # rebuilt lazily with the new url/token

    def _client(self) -> ZabbixApi:
        if self._api is None:
            if not self.config.get("url") or not self.config.get("token"):
                raise RuntimeError("zabbix url/token not configured")
            self._api = ZabbixApi(self.config["url"], self.config["token"],
                                  verify=self.config.get("verify_tls", True))
        return self._api

    async def status(self) -> Dict[str, Any]:
        """Reachability + version + managed host count (read-only)."""
        api = self._client()
        version = await api.call("apiinfo.version")
        managed = await api.call("host.get", {
            "countOutput": True,
            "tags": [{"tag": "lm-managed", "value": "1", "operator": 1}]})
        return {"version": version, "managed_hosts": int(managed),
                "last_sync": self.last_sync}

    async def sync(self, devices: List[Dict[str, Any]], dry_run: bool = False) -> Dict[str, Any]:
        """Reconcile LM's device list into Zabbix. Also serves as the rebuild:
        a fresh Zabbix repopulates entirely from the inventory."""
        result = await HostSync(self._client(), self.config).run(devices, dry_run=dry_run)
        if not dry_run:
            self.last_sync = {k: result[k] for k in
                              ("created", "refreshed", "disabled", "deleted", "held", "errors")}
        return result
