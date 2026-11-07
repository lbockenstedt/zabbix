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

Ping-only hosts (no SNMP interface) that LM no longer sends are deleted at
once: the scanner no longer adds them, only hosts LM explicitly sends stay.
"""
# Standard library imports.
import logging
import hashlib
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("ZabbixSync")

# Tag names used on managed hosts, and the SNMP community macro.
TAG_MANAGED = "lm-managed"
TAG_KEY = "lm-key"
TAG_LAST_SEEN = "lm-last-seen"
TAG_DISABLED_AT = "lm-disabled-at"
TAG_CRED = "lm-cred"  # fingerprint of the SNMP secret last applied (the secret itself is unreadable)
SNMP_MACRO = "{$SNMP_COMMUNITY}"

# Default sync settings; the config can override any of them.
DEFAULTS = {
    "stale_days": 7,
    "delete_days": 30,
    "max_age_out_pct": 5,
    "min_fleet": 3,
    # Re-stamp lm-last-seen at most this often: every host update is a Zabbix
    # config change, and rewriting thousands each run floods the changelog.
    "touch_hours": 6,
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
    # Keep the first entry for each key; skip entries with no name.
    for d in devices:
        key = device_key(d.get("tenant") or "default", d.get("name") or "")
        if key.endswith("/") or key in seen:
            continue
        seen[key] = {**d, "key": key, "tenant": d.get("tenant") or "default"}
    # Return the kept entries.
    return list(seen.values())


def proxy_name_for(tenant: str) -> str:
    """Convention shared with the hub's proxy installer: ``lm-<tenant>``."""
    return "lm-" + re.sub(r"[^A-Za-z0-9._-]+", "-", str(tenant or "default"))


def cred_fingerprint(secret: Optional[str]) -> str:
    """Short SHA-256 fingerprint of a secret ("" if none), safe to store as a tag."""
    return hashlib.sha256(secret.encode()).hexdigest()[:12] if secret else ""


def host_name(key: str) -> str:
    """Zabbix technical host names only allow a limited character set."""
    return re.sub(r"[^A-Za-z0-9._ -]", "_", key.replace("/", "--"))


def to_iso(dt: datetime) -> str:
    """Format a datetime as a UTC ISO string ending in Z."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str) -> Optional[datetime]:
    """Parse a UTC ISO string ending in Z; None if invalid."""
    try:
        return datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def templates_for(device: Dict[str, Any], cfg: Dict[str, Any]) -> List[str]:
    """Once an SNMP credential is known: the type's template, else the generic
    SNMP template (it already carries the ICMP check, so the two clash).
    Without one: ICMP Ping only — type templates poll over SNMP and Zabbix
    refuses to link them to a host that has no SNMP interface."""
    # With SNMP: the type's template, else the generic SNMP template.
    if device.get("snmp"):
        return [cfg["templates"].get(device.get("type") or "") or cfg["snmp_template"]]
    # Without SNMP: ICMP only.
    return [cfg["fallback_template"]]


def build_plan(existing: Dict[str, Dict[str, Any]], devices: List[Dict[str, Any]],
               now: datetime, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Decide what to do. ``existing`` maps lm-key -> managed host:
    ``{hostid, status (0 on / 1 off), last_seen, disabled_at, tenant}``.
    Pure function: no API calls, so every rule is unit-testable."""
    # Age limits and the keys seen in this run.
    stale = timedelta(days=cfg["stale_days"])
    purge = timedelta(days=cfg["delete_days"])
    seen_keys = {d["key"] for d in devices}

    # Known devices are refreshed; new ones are created unless they moved tenant.
    create, refresh = [], [(existing[d["key"]], d) for d in devices if d["key"] in existing]
    for d in devices:
        if d["key"] in existing:
            continue
        # LM moved the device to another tenant: re-key its existing host
        # (name, group, proxy) instead of creating a duplicate beside it.
        old = device_key(d.get("moved_from") or "", d.get("name") or "") if d.get("moved_from") else ""
        if old and old in existing and old not in seen_keys:
            refresh.append((existing[old], d))
            seen_keys.add(old)
        else:
            create.append(d)

    # Unseen ping-only hosts (no SNMP interface) were auto-added by the old
    # scanner feed; LM no longer sends them, so delete them now (no age-out).
    # Only with a non-empty feed, so an outage cannot wipe hosts LM still wants.
    purge_hosts = [{**h, "key": k} for k, h in existing.items() if seen_keys and k not in seen_keys
                   and not any(str(i.get("type")) == "2" for i in h.get("interfaces") or [])]
    purged = {h["key"] for h in purge_hosts}

    # Unseen hosts: disable after stale_days, delete after delete_days disabled.
    disable: List[Dict[str, Any]] = []
    delete: List[Dict[str, Any]] = []
    for key, host in existing.items():
        if key in seen_keys or key in purged or not host.get("last_seen"):
            continue  # no last_seen means never swept
        if host["status"] == 0 and now - host["last_seen"] > stale:
            disable.append({**host, "key": key})
        elif host["status"] == 1 and host.get("disabled_at") \
                and now - host["disabled_at"] > purge:
            delete.append({**host, "key": key})

    # Guard 1: a large fleet with nothing seen recently holds all age-out.
    held: List[str] = []
    enabled = [h for h in existing.values() if h["status"] == 0 and h.get("last_seen")]
    refreshed_recently = bool(seen_keys) or any(now - h["last_seen"] <= stale for h in enabled)
    if len(enabled) >= cfg["min_fleet"] and not refreshed_recently:
        held.append("stalled feed: no host seen within "
                    f"{cfg['stale_days']}d, age-out held for the whole fleet")
        disable, delete = [], []

    # Guard 2: hold any tenant where age-out would touch too many hosts.
    for tenant in {h["tenant"] for h in disable + delete}:
        fleet = sum(1 for k, h in existing.items() if h["tenant"] == tenant and k not in purged)
        touched = sum(1 for h in disable + delete if h["tenant"] == tenant)
        limit = max(1, fleet * cfg["max_age_out_pct"] // 100)
        if touched > limit:
            held.append(f"tenant {tenant}: age-out of {touched}/{fleet} hosts exceeds "
                        f"{cfg['max_age_out_pct']}%, held")
            disable = [h for h in disable if h["tenant"] != tenant]
            delete = [h for h in delete if h["tenant"] != tenant]

    # Return the plan.
    return {"create": create, "refresh": refresh, "disable": disable,
            "delete": delete, "purge": purge_hosts, "held": held}


class HostSync:
    """Applies a plan to Zabbix through the API wrapper."""

    def __init__(self, api, cfg: Dict[str, Any]):
        """Store the API client and merge the config over the defaults."""
        # Defaults first; template maps are merged separately.
        self.api = api
        self.cfg = {**DEFAULTS, **(cfg or {})}
        self.cfg["templates"] = {**DEFAULTS["templates"], **(cfg or {}).get("templates", {})}

    async def load_existing(self) -> Dict[str, Dict[str, Any]]:
        """Load managed hosts from Zabbix, keyed by their lm-key tag."""
        # Fetch hosts tagged as managed, with tags, groups, templates and interfaces.
        hosts = await self.api.call("host.get", {
            "output": ["hostid", "status", "monitored_by", "proxyid"], "selectTags": "extend",
            "selectHostGroups": ["name"], "selectParentTemplates": ["name"],
            "selectInterfaces": ["interfaceid", "type", "ip"],
            "tags": [{"tag": TAG_MANAGED, "value": "1", "operator": 1}]})
        # Build a record per host; skip hosts without a key.
        existing: Dict[str, Dict[str, Any]] = {}
        for h in hosts:
            tags = {t["tag"]: t["value"] for t in h.get("tags", [])}
            key = tags.get(TAG_KEY)
            if not key:
                continue
            # Tenant comes from the first group's name ("LM/<tenant>").
            groups = [g["name"] for g in h.get("hostgroups", [])]
            existing[key] = {
                "key": key, "hostid": h["hostid"], "status": int(h["status"]),
                "last_seen": parse_iso(tags.get(TAG_LAST_SEEN)),
                "disabled_at": parse_iso(tags.get(TAG_DISABLED_AT)),
                "cred": tags.get(TAG_CRED, ""),
                "tenant": groups[0].split("/", 1)[-1] if groups else "default",
                "templates": {t["name"] for t in h.get("parentTemplates", [])},
                "interfaces": h.get("interfaces", []),
                "proxyid": h.get("proxyid", "0") if str(h.get("monitored_by")) == "1" else "0",
            }
        # Return the records.
        return existing

    async def _proxy_id(self, tenant: str) -> Optional[str]:
        """The tenant's proxy, if it is registered in Zabbix: an explicit
        ``tenant_proxies`` entry, else the ``lm-<tenant>`` name the hub gives
        proxies it installs. A tenant without one is polled by the server."""
        # Look up and cache each tenant's proxy (None if not registered).
        cache = self.__dict__.setdefault("_proxy_cache", {})
        if tenant not in cache:
            name = (self.cfg.get("tenant_proxies") or {}).get(tenant) or proxy_name_for(tenant)
            found = await self.api.call("proxy.get", {"output": ["proxyid"],
                                                      "filter": {"name": [name]}})
            cache[tenant] = found[0]["proxyid"] if found else None
        # Return the cached proxy id.
        return cache[tenant]

    async def _device_proxy_id(self, device: Dict[str, Any]) -> Optional[str]:
        """The device tenant's proxy, else the first registered proxy of the
        tenants the hub lists in ``proxy_tenants`` (a tenant group's members)."""
        # Try the device's tenant, then any group-member tenants.
        for tenant in [device.get("tenant") or "default", *(device.get("proxy_tenants") or [])]:
            proxyid = await self._proxy_id(tenant)
            if proxyid:
                return proxyid
        # None found.
        return None

    async def _ids(self, method: str, names: List[str], key: str) -> List[Dict[str, str]]:
        """Look up objects by name and return their ids as [{key: id}, ...]."""
        found = await self.api.call(method, {"output": [key, "name"], "filter": {"name": names}})
        return [{key: f[key]} for f in found]

    async def _group_id(self, tenant: str) -> str:
        """Return the id of the tenant's "LM/<tenant>" host group, creating it if needed."""
        # Use the existing group if there is one.
        name = f"LM/{tenant}"
        found = await self._ids("hostgroup.get", [name], "groupid")
        if found:
            return found[0]["groupid"]
        # Otherwise create it.
        created = await self.api.call("hostgroup.create", {"name": name})
        return created["groupids"][0]

    def _tags(self, key: str, now: datetime, disabled_at: Optional[datetime] = None,
              cred: str = ""):
        """Build the managed-host tags, plus optional credential and disable-time tags."""
        # Always: managed flag, key and last-seen time.
        tags = [{"tag": TAG_MANAGED, "value": "1"}, {"tag": TAG_KEY, "value": key},
                {"tag": TAG_LAST_SEEN, "value": to_iso(now)}]
        # Optional credential fingerprint and disable time.
        if cred:
            tags.append({"tag": TAG_CRED, "value": cred})
        if disabled_at:
            tags.append({"tag": TAG_DISABLED_AT, "value": to_iso(disabled_at)})
        # Return the tags.
        return tags

    async def _create(self, device: Dict[str, Any], now: datetime) -> None:
        """Create a Zabbix host for a new device."""
        # Pick templates and fingerprint the SNMP secret.
        wanted = templates_for(device, self.cfg)
        community = device.get("snmp_community") or ""
        cred = cred_fingerprint(community)
        # Look up template ids; all must exist.
        templates = await self._ids("template.get", wanted, "templateid")
        if len(templates) < len(set(wanted)):
            raise RuntimeError(f"template(s) {wanted} not all found in Zabbix")
        # SNMP interface if SNMP works, otherwise an agent interface.
        interface = {"type": 2 if device.get("snmp") else 1, "main": 1, "useip": 1,
                     "ip": device["ip"], "dns": "",
                     "port": "161" if device.get("snmp") else "10050"}
        if device.get("snmp"):
            interface["details"] = {"version": 2, "bulk": 1, "community": SNMP_MACRO}
        # Host settings: name, interface, tenant group, templates and tags.
        params = {"host": host_name(device["key"]), "name": device["name"],
                  "interfaces": [interface],
                  "groups": [{"groupid": await self._group_id(device["tenant"])}],
                  "templates": templates,
                  "tags": self._tags(device["key"], now, cred=cred)}
        # Store the SNMP community as a secret macro.
        if community:
            params["macros"] = [{"macro": SNMP_MACRO, "value": community, "type": 1}]
        # Use the tenant's proxy if there is one.
        proxyid = await self._device_proxy_id(device)
        if proxyid:
            params.update({"monitored_by": 1, "proxyid": proxyid})
        # Create the host. Visible names are unique across all of Zabbix, but
        # tenants may reuse names and IPs (e.g. 10.0.0.2 behind two proxies):
        # on a clash, add the tenant to the visible name.
        try:
            await self.api.call("host.create", params)
        except Exception as e:  # noqa: BLE001 - only the name clash is retried
            if "same visible name" not in str(e):
                raise
            params["name"] = f"{device['name']} ({device['tenant']})"
            logger.info("zabbix sync: visible name %r taken, using %r",
                        device["name"], params["name"])
            await self.api.call("host.create", params)

    async def _refresh(self, host: Dict[str, Any], device: Dict[str, Any], now: datetime) -> None:
        """Update an existing host from the latest device data."""
        # Fingerprint the current SNMP secret and start the update with fresh tags.
        community = device.get("snmp_community") or ""
        cred = cred_fingerprint(community)  # no credential for this tenant: no label
        params: Dict[str, Any] = {"hostid": host["hostid"], "status": 0,
                                  "tags": self._tags(device["key"], now, cred=cred)}
        # LM's credential vault is the source of truth: push the secret whenever
        # the credential that worked for this device changes.
        if community and cred != host.get("cred"):
            params["macros"] = [{"macro": SNMP_MACRO, "value": community, "type": 1}]
        elif not community and host.get("cred"):
            params["macros"] = []  # drop a secret that no longer belongs to this device
        # Device moved tenant: change its name and group.
        if host.get("key") and host["key"] != device["key"]:
            # Moved to another tenant in LM: follow it (technical name, group, proxy).
            logger.warning("zabbix sync: re-keying host %s -> %s", host["key"], device["key"])
            params.update({"host": host_name(device["key"]),
                           "groups": [{"groupid": await self._group_id(device["tenant"])}]})
            host = {**host, "tenant": device["tenant"]}
        # Set the proxy, or clear it if the tenant no longer has one.
        proxyid = await self._device_proxy_id({**device, "tenant": host.get("tenant") or device.get("tenant")})
        if proxyid and host.get("proxyid") != proxyid:
            params.update({"monitored_by": 1, "proxyid": proxyid})
        elif not proxyid and host.get("proxyid", "0") != "0":
            params.update({"monitored_by": 0, "proxyid": 0})
        # Update templates if they changed.
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
        # Skip the update when nothing changed and last-seen is still fresh.
        fresh = host.get("last_seen") and \
            now - host["last_seen"] < timedelta(hours=self.cfg["touch_hours"])
        unchanged = host.get("status") == 0 and cred == host.get("cred", "") \
            and set(params) == {"hostid", "status", "tags"}
        if not (fresh and unchanged):
            await self.api.call("host.update", params)
        # Then remove the old agent interface if SNMP was already set up.
        if snmp_if:  # only once the SNMP interface already existed before this run
            await self._drop_agent_interface(host, device)

    async def _drop_agent_interface(self, host: Dict[str, Any], device: Dict[str, Any]) -> None:
        """An SNMP-monitored device must not keep the agent-type (ZBX) interface it
        was created with before SNMP was verified. Zabbix refuses while items
        still use it, so a failure is only logged and retried next sync."""
        # Only for SNMP devices that already have an SNMP interface.
        if not device.get("snmp"):
            return
        if not any(str(i.get("type")) == "2" for i in host.get("interfaces", [])):
            return
        # Try to delete each agent interface; log failures.
        for i in host.get("interfaces", []):
            if str(i.get("type")) == "1":
                try:
                    await self.api.call("hostinterface.delete", [i["interfaceid"]])
                except Exception as e:  # noqa: BLE001
                    logger.warning("zabbix sync: cannot drop agent interface of %s: %s",
                                   device.get("key"), e)

    async def run(self, devices: List[Dict[str, Any]], now: Optional[datetime] = None,
                  dry_run: bool = False) -> Dict[str, Any]:
        """Reconcile the device list into Zabbix; returns counts, errors and holds."""
        # Dedupe devices, load hosts and build the plan.
        now = now or datetime.now(timezone.utc)
        devices = dedupe_devices(devices)
        existing = await self.load_existing()
        plan = build_plan(existing, devices, now, self.cfg)
        # Summary of planned and completed actions.
        summary = {"created": 0, "refreshed": 0, "disabled": 0, "deleted": 0, "purged": 0,
                   "errors": [], "held": plan["held"], "dry_run": dry_run,
                   "planned": {k: len(plan[k]) for k in
                               ("create", "refresh", "disable", "delete", "purge")}}
        # Log any holds.
        for reason in plan["held"]:
            logger.warning("zabbix sync hold: %s", reason)
        # Dry run: report the plan only.
        if dry_run:
            return summary

        async def attempt(counter: str, label: str, coro) -> None:
            """Run one action and count it; on error log it and record the message."""
            try:
                await coro
                summary[counter] += 1
            except Exception as e:  # noqa: BLE001 - one bad host must not stop the run
                logger.error("zabbix sync error: %s: %s", label, e)
                summary["errors"].append(f"{label}: {e}")

        # Create, refresh, disable and delete hosts.
        for d in plan["create"]:
            await attempt("created", f"create {d['key']}", self._create(d, now))
        for host, d in plan["refresh"]:
            await attempt("refreshed", f"refresh {d['key']}", self._refresh(host, d, now))
        # Disabling keeps the last-seen time and records when it was disabled.
        for h in plan["disable"]:
            await attempt("disabled", f"disable {h['key']}", self.api.call("host.update", {
                "hostid": h["hostid"], "status": 1,
                "tags": self._tags(h["key"], h["last_seen"], disabled_at=now,
                                   cred=h.get("cred", ""))}))
        for h in plan["delete"]:
            await attempt("deleted", f"delete {h['key']}", self.api.call("host.delete", [h["hostid"]]))
        # Purge ping-only hosts in batches (there can be thousands).
        for i in range(0, len(plan["purge"]), 100):
            batch = plan["purge"][i:i + 100]
            try:
                await self.api.call("host.delete", [h["hostid"] for h in batch])
                summary["purged"] += len(batch)
            except Exception as e:  # noqa: BLE001 - one bad batch must not stop the run
                logger.error("zabbix sync error: purge %d ping-only hosts: %s", len(batch), e)
                summary["errors"].append(f"purge batch {i // 100}: {e}")
        # Log the counts and return the summary.
        logger.info("zabbix sync done: %s", {k: summary[k] for k in
                                             ("created", "refreshed", "disabled", "deleted", "purged")})
        return summary
