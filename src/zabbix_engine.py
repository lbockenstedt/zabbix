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
        # Counts changes of URL, token or TLS setting; each client is built
        # for one count (see _write_refusal).
        self._conn_gen = 0
        # Writes still on their way to Zabbix, from any client (see
        # ZabbixApi._send_write), so a change that refuses them stops them.
        self._writes = set()
        self.last_sync: Dict[str, Any] = {}
        self.set_config(config or {})

    @staticmethod
    def _connection(config: Dict[str, Any]) -> tuple:
        """The settings a client is built from: URL, token and TLS verification."""
        return config.get("url"), config.get("token"), config.get("verify_tls", True)

    def set_config(self, config: Dict[str, Any]) -> None:
        """Config keys: url, token, verify_tls, read_only, plus sync settings
        (stale_days, delete_days, max_age_out_pct, templates, tenant_proxies)."""
        # A new URL, token or TLS setting stops every older client's writes,
        # before anything else changes. The same values, which the hub re-sends
        # on every reconnect, leave older clients as they were.
        repointed = self._connection(config) != self._connection(self.config)
        if repointed:
            self._conn_gen += 1
        # Store the config and drop the old client.
        self.config = config
        self._api = None  # rebuilt lazily with the new url/token
        # Writes refused from now on that are already on their way stop too:
        # each may still be waiting for its connection, with nothing sent.
        # No await here, so none of them sends anything before it is stopped.
        if repointed or self.read_only:
            for task in list(self._writes):
                task.cancel()

    @property
    def read_only(self) -> bool:
        """True when the hub marked this Zabbix read-only (shared with another
        environment, hub System → Sync)."""
        return bool(self.config.get("read_only"))

    def _client(self) -> ZabbixApi:
        """Return the API client, creating it from the config on first use."""
        # Create the client; URL and token are required.
        if self._api is None:
            url, token = self.config.get("url"), self.config.get("token")
            if not url or not token:
                raise RuntimeError("zabbix url/token not configured")
            # The client asks _write_refusal before every write, for the
            # connection settings it was built for, so a sync still holding a
            # replaced client stops writing when read-only is switched on.
            gen = self._conn_gen
            self._api = ZabbixApi(url, token, verify=self.config.get("verify_tls", True),
                                  write_refusal=lambda: self._write_refusal(gen),
                                  writes=self._writes)
        return self._api

    def _write_refusal(self, gen: int) -> str:
        """Why a client built for connection count ``gen`` may not write now, or "".

        A client built before the URL, token or TLS setting last changed never
        writes again: not once read-only is off, and not after the config
        moves back to the old values. One push can point a dev spoke away from
        a shared Zabbix and turn read-only off, and a sync still holding the
        old client must not then write anywhere with it. That reason is
        checked first because turning read-only off would not end it.
        Otherwise read-only refuses every write. The hub re-sends the same URL
        and token on every reconnect, which leaves older clients as they were
        (see ``set_config``)."""
        # A client left over from before a change of URL, token or TLS setting.
        if gen != self._conn_gen:
            return "This spoke now uses a different Zabbix URL, token or TLS setting"
        # Read-only: nothing writes.
        if self.read_only:
            return "Zabbix is read-only on this environment"
        return ""

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
                              ("created", "refreshed", "disabled", "deleted", "purged", "held", "errors")}
        return result
