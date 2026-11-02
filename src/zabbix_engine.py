"""Zabbix engine: owns the API client and runs inventory syncs."""
import logging
from typing import Any, Dict, List

from zabbix_api import ZabbixApi
from zabbix_sync import HostSync, host_name

AGENT_TEMPLATE = "Linux by Zabbix agent active"

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

    async def hosts(self) -> Dict[str, Any]:
        """Managed hosts with availability and open problems (read-only)."""
        api = self._client()
        found = await api.call("host.get", {
            "output": ["hostid", "name", "status"], "selectTags": "extend",
            "selectInterfaces": ["type", "ip", "available", "error"],
            "selectParentTemplates": ["name"],
            "tags": [{"tag": "lm-managed", "value": "1", "operator": 1}]})
        problems = await api.call("problem.get", {
            "output": ["eventid", "objectid", "name", "severity", "clock"],
            "recent": False, "suppressed": False, "sortfield": "eventid", "sortorder": "DESC",
            "limit": 500})
        trig = {}
        if problems:
            for t in await api.call("trigger.get", {
                    "output": ["triggerid"], "selectHosts": ["hostid"],
                    "triggerids": [p["objectid"] for p in problems]}):
                trig[t["triggerid"]] = [h["hostid"] for h in t.get("hosts", [])]
        by_host: Dict[str, List[Dict[str, Any]]] = {}
        for p in problems:
            for hid in trig.get(p["objectid"], []):
                by_host.setdefault(hid, []).append(
                    {"name": p["name"], "severity": int(p["severity"]), "clock": int(p["clock"])})
        rows = []
        for h in found:
            tags = {t["tag"]: t["value"] for t in h.get("tags", [])}
            key = tags.get("lm-key", "")
            ifs = h.get("interfaces", [])
            snmp = [i for i in ifs if str(i.get("type")) == "2"]
            iface = snmp[0] if snmp else (ifs[0] if ifs else {})
            avail = {"0": "unknown", "1": "available", "2": "unavailable"}.get(
                str(iface.get("available", "0")), "unknown")
            probs = by_host.get(h["hostid"], [])
            rows.append({
                "name": key.split("/", 1)[-1] if key else h["name"],
                "tenant": key.split("/", 1)[0] if "/" in key else "default",
                "ip": iface.get("ip", ""), "method": "SNMP" if snmp else "ICMP",
                "availability": avail, "error": (iface.get("error") or "")[:160],
                "enabled": str(h["status"]) == "0",
                "templates": [t["name"] for t in h.get("parentTemplates", [])],
                "problems": sorted(probs, key=lambda x: -x["severity"])[:100],
                "problem_count": len(probs)})
        return {"hosts": rows}

    async def register_proxy(self, name: str) -> Dict[str, Any]:
        """Create the (active-mode) proxy a freshly installed zbxproxy reports as;
        Zabbix ignores a proxy it has no record of. Idempotent."""
        api = self._client()
        found = await api.call("proxy.get", {"output": ["proxyid"], "filter": {"name": [name]}})
        if found:
            return {"proxy": name, "proxyid": found[0]["proxyid"], "created": False}
        res = await api.call("proxy.create", {"name": name, "operating_mode": 0})
        return {"proxy": name, "proxyid": res["proxyids"][0], "created": True}

    async def register_agent_host(self, tenant: str, hostname: str) -> Dict[str, Any]:
        """Host record for an active zabbix-agent2 that reports to the tenant's proxy.
        Not tagged lm-managed, so the inventory sync never reaps it. Idempotent."""
        sync = HostSync(self._client(), self.config)
        name = host_name(f"{tenant}/{hostname}")
        api = sync.api
        if await api.call("host.get", {"output": ["hostid"], "filter": {"host": [name]}}):
            return {"host": name, "created": False}
        params = {"host": name, "name": hostname,
                  "groups": [{"groupid": await sync._group_id(tenant)}],
                  "tags": [{"tag": "lm-agent", "value": "1"}]}
        templates = await sync._ids("template.get", [AGENT_TEMPLATE], "templateid")
        if templates:
            params["templates"] = templates
        proxyid = await sync._proxy_id(tenant)
        if proxyid:
            params.update({"monitored_by": 1, "proxyid": proxyid})
        await api.call("host.create", params)
        return {"host": name, "created": True, "proxied": bool(proxyid),
                "templated": bool(templates)}

    async def sync(self, devices: List[Dict[str, Any]], dry_run: bool = False) -> Dict[str, Any]:
        """Reconcile LM's device list into Zabbix. Also serves as the rebuild:
        a fresh Zabbix repopulates entirely from the inventory."""
        result = await HostSync(self._client(), self.config).run(devices, dry_run=dry_run)
        if not dry_run:
            self.last_sync = {k: result[k] for k in
                              ("created", "refreshed", "disabled", "deleted", "held", "errors")}
        return result
