"""Zabbix engine: owns the API client and runs inventory syncs."""
# Standard library imports.
import logging
from typing import Any, Dict, List

# API client and inventory sync helpers.
from zabbix_api import ZabbixApi
from zabbix_sync import HostSync, host_name

# Template linked to hosts running the Zabbix agent.
AGENT_TEMPLATE = "Linux by Zabbix agent active"

logger = logging.getLogger("ZabbixEngine")


class ZabbixEngine:
    """Holds the Zabbix config and API client; runs reads, registrations and syncs."""
    def __init__(self, config: Dict[str, Any] = None):
        """Start with no client or sync result, then apply the config."""
        self.config: Dict[str, Any] = {}
        self._api = None
        self.last_sync: Dict[str, Any] = {}
        self.set_config(config or {})

    def set_config(self, config: Dict[str, Any]) -> None:
        """Config keys: url, token, verify_tls, plus sync settings
        (stale_days, delete_days, max_age_out_pct, templates, tenant_proxies)."""
        # Store the config and drop the old client.
        self.config = config
        self._api = None  # rebuilt lazily with the new url/token

    def _client(self) -> ZabbixApi:
        """Return the API client, creating it from the config on first use."""
        # Create the client; URL and token are required.
        if self._api is None:
            if not self.config.get("url") or not self.config.get("token"):
                raise RuntimeError("zabbix url/token not configured")
            self._api = ZabbixApi(self.config["url"], self.config["token"],
                                  verify=self.config.get("verify_tls", True))
        return self._api

    async def status(self) -> Dict[str, Any]:
        """Reachability + version + managed host count (read-only)."""
        # Ask for the API version.
        api = self._client()
        version = await api.call("apiinfo.version")
        # Count hosts tagged as managed.
        managed = await api.call("host.get", {
            "countOutput": True,
            "tags": [{"tag": "lm-managed", "value": "1", "operator": 1}]})
        # Return the version, host count and last sync summary.
        return {"version": version, "managed_hosts": int(managed),
                "last_sync": self.last_sync}

    async def hosts(self) -> Dict[str, Any]:
        """Managed hosts with availability and open problems (read-only)."""
        # Fetch managed hosts with tags, interfaces and templates.
        api = self._client()
        found = await api.call("host.get", {
            "output": ["hostid", "name", "status"], "selectTags": "extend",
            "selectInterfaces": ["type", "ip", "available", "error"],
            "selectParentTemplates": ["name"],
            "tags": [{"tag": "lm-managed", "value": "1", "operator": 1}]})
        # Fetch up to 500 current problems.
        problems = await api.call("problem.get", {
            "output": ["eventid", "objectid", "name", "severity", "clock"],
            "recent": False, "suppressed": False, "sortfield": "eventid", "sortorder": "DESC",
            "limit": 500})
        # Map each problem's trigger to its hosts.
        trig = {}
        if problems:
            for t in await api.call("trigger.get", {
                    "output": ["triggerid"], "selectHosts": ["hostid"],
                    "triggerids": [p["objectid"] for p in problems]}):
                trig[t["triggerid"]] = [h["hostid"] for h in t.get("hosts", [])]
        # Group problems by host.
        by_host: Dict[str, List[Dict[str, Any]]] = {}
        for p in problems:
            for hid in trig.get(p["objectid"], []):
                by_host.setdefault(hid, []).append(
                    {"name": p["name"], "severity": int(p["severity"]), "clock": int(p["clock"])})
        # Build one row per host.
        rows = []
        for h in found:
            # Tenant and name come from the lm-key tag.
            tags = {t["tag"]: t["value"] for t in h.get("tags", [])}
            key = tags.get("lm-key", "")
            # Prefer the SNMP interface; otherwise the first one.
            ifs = h.get("interfaces", [])
            snmp = [i for i in ifs if str(i.get("type")) == "2"]
            iface = snmp[0] if snmp else (ifs[0] if ifs else {})
            # Turn the availability code into a word.
            avail = {"0": "unknown", "1": "available", "2": "unavailable"}.get(
                str(iface.get("available", "0")), "unknown")
            # Add the row with up to 100 problems, most severe first.
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

    async def register_proxy(self, name: str, tls_subject: str = "") -> Dict[str, Any]:
        """Create the (active-mode) proxy a freshly installed zbxproxy reports as;
        Zabbix ignores a proxy it has no record of. Idempotent."""
        # Optional TLS certificate settings.
        api = self._client()
        tls = {"tls_accept": 4, "tls_subject": f"CN={tls_subject}"} if tls_subject else {}
        # Look up an existing proxy by name.
        found = await api.call("proxy.get", {"output": ["proxyid"], "filter": {"name": [name]}})
        # Found: update its TLS settings if given and return it.
        if found:
            if tls:
                await api.call("proxy.update", {"proxyid": found[0]["proxyid"], **tls})
            return {"proxy": name, "proxyid": found[0]["proxyid"], "created": False}
        # Otherwise create an active-mode proxy.
        res = await api.call("proxy.create", {"name": name, "operating_mode": 0, **tls})
        return {"proxy": name, "proxyid": res["proxyids"][0], "created": True}

    async def register_agent_host(self, tenant: str, hostname: str, tls_subject: str = "") -> Dict[str, Any]:
        """Host record for an active zabbix-agent2 that reports to the tenant's proxy.
        Not tagged lm-managed, so the inventory sync never reaps it. Idempotent."""
        # Sync helper, the full host name and the API client.
        sync = HostSync(self._client(), self.config)
        name = host_name(f"{tenant}/{hostname}")
        api = sync.api
        # Optional TLS certificate settings.
        tls = ({"tls_connect": 4, "tls_accept": 4, "tls_subject": f"CN={tls_subject}"}
               if tls_subject else {})
        # Look up an existing host.
        existing = await api.call("host.get", {"output": ["hostid"], "filter": {"host": [name]}})
        # Found: update its TLS settings if given and return it.
        if existing:
            if tls:
                await api.call("host.update", {"hostid": existing[0]["hostid"], **tls})
            return {"host": name, "created": False}
        # New host in the tenant's group, tagged as an agent host.
        params = {"host": name, "name": hostname, **tls,
                  "groups": [{"groupid": await sync._group_id(tenant)}],
                  "tags": [{"tag": "lm-agent", "value": "1"}]}
        # Link the agent template if it exists.
        templates = await sync._ids("template.get", [AGENT_TEMPLATE], "templateid")
        if templates:
            params["templates"] = templates
        # Monitor through the tenant's proxy if there is one.
        proxyid = await sync._proxy_id(tenant)
        if proxyid:
            params.update({"monitored_by": 1, "proxyid": proxyid})
        # Create the host.
        await api.call("host.create", params)
        return {"host": name, "created": True, "proxied": bool(proxyid),
                "templated": bool(templates)}

    async def sync(self, devices: List[Dict[str, Any]], dry_run: bool = False) -> Dict[str, Any]:
        """Reconcile LM's device list into Zabbix. Also serves as the rebuild:
        a fresh Zabbix repopulates entirely from the inventory."""
        # Run the sync.
        result = await HostSync(self._client(), self.config).run(devices, dry_run=dry_run)
        # Record the counts unless this was a dry run.
        if not dry_run:
            self.last_sync = {k: result[k] for k in
                              ("created", "refreshed", "disabled", "deleted", "held", "errors")}
        return result
