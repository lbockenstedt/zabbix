"""Inventory -> Zabbix host reconcile.

LM discovers devices and pushes each sighting here. Every host this module
creates carries the tags ``lm-managed``, ``lm-key`` and ``lm-last-seen``; hosts
without ``lm-managed`` are hand-made and never touched.

Age-out mirrors the NetBox staleness sweep: a managed host not seen for
``stale_days`` is disabled, and a host disabled for ``delete_days`` is deleted.
Two guards stop a source outage from wiping the fleet:

* stalled feed: nothing in a large fleet was seen recently -> hold all age-out.
* mass change: age-out would touch more than ``max_age_out_pct`` of a tenant's
  hosts in one run -> hold that tenant and report it.
"""
import logging
import hashlib
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("ZabbixSync")

TAG_MANAGED = "lm-managed"
TAG_KEY = "lm-key"
TAG_LAST_SEEN = "lm-last-seen"
TAG_DISABLED_AT = "lm-disabled-at"
TAG_CRED = "lm-cred"  # fingerprint of the SNMP secret last applied (the secret itself is unreadable)
SNMP_MACRO = "{$SNMP_COMMUNITY}"

DEFAULTS = {
    "stale_days": 7,
    "delete_days": 30,
    "max_age_out_pct": 5,
    "min_fleet": 3,
    "fallback_template": "ICMP Ping",
    "snmp_template": "Network Generic Device by SNMP",
    "templates": {},
}


def device_key(tenant: str, name: str) -> str:
    """Stable identity for a device. LM can list one device several times (one
    entry per management address), so identity is tenant + name, not entry id."""
    return f"{tenant}/{name}".strip().lower()


def dedupe_devices(devices: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One entry per device_key; the first entry seen wins."""
    seen: Dict[str, Dict[str, Any]] = {}
    for d in devices:
        key = device_key(d.get("tenant") or "default", d.get("name") or "")
        if key.endswith("/") or key in seen:
            continue
        seen[key] = {**d, "key": key, "tenant": d.get("tenant") or "default"}
    return list(seen.values())


def cred_fingerprint(secret: Optional[str]) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()[:12] if secret else ""


def host_name(key: str) -> str:
    """Zabbix technical host names only allow a limited character set."""
    return re.sub(r"[^A-Za-z0-9._ -]", "_", key.replace("/", "--"))


def to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str) -> Optional[datetime]:
    try:
        return datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def templates_for(device: Dict[str, Any], cfg: Dict[str, Any]) -> List[str]:
    """The type's template, else ICMP Ping, or the generic SNMP template once a
    credential is known (it already carries the ICMP check, so the two clash)."""
    mapped = cfg["templates"].get(device.get("type") or "")
    if device.get("snmp"):
        return [mapped or cfg["snmp_template"]]
    return [mapped or cfg["fallback_template"]]


def build_plan(existing: Dict[str, Dict[str, Any]], devices: List[Dict[str, Any]],
               now: datetime, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Decide what to do. ``existing`` maps lm-key -> managed host:
    ``{hostid, status (0 on / 1 off), last_seen, disabled_at, tenant}``.
    Pure function: no API calls, so every rule is unit-testable."""
    stale = timedelta(days=cfg["stale_days"])
    purge = timedelta(days=cfg["delete_days"])
    seen_keys = {d["key"] for d in devices}

    create = [d for d in devices if d["key"] not in existing]
    refresh = [(existing[d["key"]], d) for d in devices if d["key"] in existing]

    disable: List[Dict[str, Any]] = []
    delete: List[Dict[str, Any]] = []
    for key, host in existing.items():
        if key in seen_keys or not host.get("last_seen"):
            continue  # no last_seen means never swept
        if host["status"] == 0 and now - host["last_seen"] > stale:
            disable.append({**host, "key": key})
        elif host["status"] == 1 and host.get("disabled_at") \
                and now - host["disabled_at"] > purge:
            delete.append({**host, "key": key})

    held: List[str] = []
    enabled = [h for h in existing.values() if h["status"] == 0 and h.get("last_seen")]
    refreshed_recently = bool(seen_keys) or any(now - h["last_seen"] <= stale for h in enabled)
    if len(enabled) >= cfg["min_fleet"] and not refreshed_recently:
        held.append("stalled feed: no host seen within "
                    f"{cfg['stale_days']}d, age-out held for the whole fleet")
        disable, delete = [], []

    for tenant in {h["tenant"] for h in disable + delete}:
        fleet = sum(1 for h in existing.values() if h["tenant"] == tenant)
        touched = sum(1 for h in disable + delete if h["tenant"] == tenant)
        limit = max(1, fleet * cfg["max_age_out_pct"] // 100)
        if touched > limit:
            held.append(f"tenant {tenant}: age-out of {touched}/{fleet} hosts exceeds "
                        f"{cfg['max_age_out_pct']}%, held")
            disable = [h for h in disable if h["tenant"] != tenant]
            delete = [h for h in delete if h["tenant"] != tenant]

    return {"create": create, "refresh": refresh, "disable": disable,
            "delete": delete, "held": held}


class HostSync:
    """Applies a plan to Zabbix through the API wrapper."""

    def __init__(self, api, cfg: Dict[str, Any]):
        self.api = api
        self.cfg = {**DEFAULTS, **(cfg or {})}
        self.cfg["templates"] = {**DEFAULTS["templates"], **(cfg or {}).get("templates", {})}

    async def load_existing(self) -> Dict[str, Dict[str, Any]]:
        hosts = await self.api.call("host.get", {
            "output": ["hostid", "status"], "selectTags": "extend",
            "selectHostGroups": ["name"], "selectParentTemplates": ["name"],
            "selectInterfaces": ["interfaceid", "type", "ip"],
            "tags": [{"tag": TAG_MANAGED, "value": "1", "operator": 1}]})
        existing: Dict[str, Dict[str, Any]] = {}
        for h in hosts:
            tags = {t["tag"]: t["value"] for t in h.get("tags", [])}
            key = tags.get(TAG_KEY)
            if not key:
                continue
            groups = [g["name"] for g in h.get("hostgroups", [])]
            existing[key] = {
                "hostid": h["hostid"], "status": int(h["status"]),
                "last_seen": parse_iso(tags.get(TAG_LAST_SEEN)),
                "disabled_at": parse_iso(tags.get(TAG_DISABLED_AT)),
                "cred": tags.get(TAG_CRED, ""),
                "tenant": groups[0].split("/", 1)[-1] if groups else "default",
                "templates": {t["name"] for t in h.get("parentTemplates", [])},
                "interfaces": h.get("interfaces", []),
            }
        return existing

    async def _ids(self, method: str, names: List[str], key: str) -> List[Dict[str, str]]:
        found = await self.api.call(method, {"output": [key, "name"], "filter": {"name": names}})
        return [{key: f[key]} for f in found]

    async def _group_id(self, tenant: str) -> str:
        name = f"LM/{tenant}"
        found = await self._ids("hostgroup.get", [name], "groupid")
        if found:
            return found[0]["groupid"]
        created = await self.api.call("hostgroup.create", {"name": name})
        return created["groupids"][0]

    def _tags(self, key: str, now: datetime, disabled_at: Optional[datetime] = None,
              cred: str = ""):
        tags = [{"tag": TAG_MANAGED, "value": "1"}, {"tag": TAG_KEY, "value": key},
                {"tag": TAG_LAST_SEEN, "value": to_iso(now)}]
        if cred:
            tags.append({"tag": TAG_CRED, "value": cred})
        if disabled_at:
            tags.append({"tag": TAG_DISABLED_AT, "value": to_iso(disabled_at)})
        return tags

    async def _create(self, device: Dict[str, Any], now: datetime) -> None:
        wanted = templates_for(device, self.cfg)
        community = device.get("snmp_community") or ""
        cred = cred_fingerprint(community)
        templates = await self._ids("template.get", wanted, "templateid")
        if len(templates) < len(set(wanted)):
            raise RuntimeError(f"template(s) {wanted} not all found in Zabbix")
        interface = {"type": 2 if device.get("snmp") else 1, "main": 1, "useip": 1,
                     "ip": device["ip"], "dns": "",
                     "port": "161" if device.get("snmp") else "10050"}
        if device.get("snmp"):
            interface["details"] = {"version": 2, "bulk": 1, "community": SNMP_MACRO}
        params = {"host": host_name(device["key"]), "name": device["name"],
                  "interfaces": [interface],
                  "groups": [{"groupid": await self._group_id(device["tenant"])}],
                  "templates": templates,
                  "tags": self._tags(device["key"], now, cred=cred)}
        if community:
            params["macros"] = [{"macro": SNMP_MACRO, "value": community, "type": 1}]
        proxy = self.cfg.get("tenant_proxies", {}).get(device["tenant"])
        if proxy:
            found = await self.api.call("proxy.get", {"output": ["proxyid"],
                                                      "filter": {"name": [proxy]}})
            if found:
                params.update({"monitored_by": 1, "proxyid": found[0]["proxyid"]})
        await self.api.call("host.create", params)

    async def _refresh(self, host: Dict[str, Any], device: Dict[str, Any], now: datetime) -> None:
        community = device.get("snmp_community") or ""
        cred = cred_fingerprint(community) or host.get("cred", "")
        params: Dict[str, Any] = {"hostid": host["hostid"], "status": 0,
                                  "tags": self._tags(device["key"], now, cred=cred)}
        # LM's credential vault is the source of truth: push the secret whenever
        # the credential that worked for this device changes.
        if community and cred != host.get("cred"):
            params["macros"] = [{"macro": SNMP_MACRO, "value": community, "type": 1}]
        want = templates_for(device, self.cfg)
        if set(want) != host["templates"]:
            params["templates"] = await self._ids("template.get", want, "templateid")
        # An existing ICMP/agent interface is linked to items and cannot change
        # type, so the SNMP interface is added next to it.
        snmp_if = [i for i in host.get("interfaces", []) if str(i.get("type")) == "2"]
        if device.get("snmp") and snmp_if:
            # The address that answers SNMP may change (e.g. a verified one is preferred).
            if snmp_if[0].get("ip") and snmp_if[0]["ip"] != device["ip"]:
                await self.api.call("hostinterface.update", {
                    "interfaceid": snmp_if[0]["interfaceid"], "ip": device["ip"]})
        elif device.get("snmp"):
            await self.api.call("hostinterface.create", {
                "hostid": host["hostid"], "type": 2, "main": 1, "useip": 1,
                "ip": device["ip"], "dns": "", "port": "161",
                "details": {"version": 2, "bulk": 1, "community": SNMP_MACRO}})
        await self.api.call("host.update", params)
        if snmp_if:  # only once the SNMP interface already existed before this run
            await self._drop_agent_interface(host, device)

    async def _drop_agent_interface(self, host: Dict[str, Any], device: Dict[str, Any]) -> None:
        """An SNMP-monitored device must not keep the agent-type (ZBX) interface it
        was created with before SNMP was verified. Zabbix refuses while items
        still use it, so a failure is only logged and retried next sync."""
        if not device.get("snmp"):
            return
        if not any(str(i.get("type")) == "2" for i in host.get("interfaces", [])):
            return
        for i in host.get("interfaces", []):
            if str(i.get("type")) == "1":
                try:
                    await self.api.call("hostinterface.delete", [i["interfaceid"]])
                except Exception as e:  # noqa: BLE001
                    logger.warning("zabbix sync: cannot drop agent interface of %s: %s",
                                   device.get("key"), e)

    async def run(self, devices: List[Dict[str, Any]], now: Optional[datetime] = None,
                  dry_run: bool = False) -> Dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        devices = dedupe_devices(devices)
        existing = await self.load_existing()
        plan = build_plan(existing, devices, now, self.cfg)
        summary = {"created": 0, "refreshed": 0, "disabled": 0, "deleted": 0,
                   "errors": [], "held": plan["held"], "dry_run": dry_run,
                   "planned": {k: len(plan[k]) for k in ("create", "refresh", "disable", "delete")}}
        for reason in plan["held"]:
            logger.warning("zabbix sync hold: %s", reason)
        if dry_run:
            return summary

        async def attempt(counter: str, label: str, coro) -> None:
            try:
                await coro
                summary[counter] += 1
            except Exception as e:  # noqa: BLE001 - one bad host must not stop the run
                logger.error("zabbix sync error: %s: %s", label, e)
                summary["errors"].append(f"{label}: {e}")

        for d in plan["create"]:
            await attempt("created", f"create {d['key']}", self._create(d, now))
        for host, d in plan["refresh"]:
            await attempt("refreshed", f"refresh {d['key']}", self._refresh(host, d, now))
        for h in plan["disable"]:
            await attempt("disabled", f"disable {h['key']}", self.api.call("host.update", {
                "hostid": h["hostid"], "status": 1,
                "tags": self._tags(h["key"], h["last_seen"], disabled_at=now,
                                   cred=h.get("cred", ""))}))
        for h in plan["delete"]:
            await attempt("deleted", f"delete {h['key']}", self.api.call("host.delete", [h["hostid"]]))
        logger.info("zabbix sync done: %s", {k: summary[k] for k in
                                             ("created", "refreshed", "disabled", "deleted")})
        return summary
