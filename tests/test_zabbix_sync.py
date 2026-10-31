"""Tests for the inventory -> Zabbix reconcile (plan rules + apply against a fake API)."""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import zabbix_sync as zs  # noqa: E402

NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)
CFG = {**zs.DEFAULTS, "templates": {"cx_switch": "Aruba CX by SNMP"}}


def run(coro):
    return asyncio.run(coro)


class FakeApi:
    """In-memory Zabbix: just enough of host/hostgroup/template/proxy."""

    def __init__(self, templates=("ICMP Ping", "Aruba CX by SNMP")):
        self.templates = {n: str(i) for i, n in enumerate(templates, 1)}
        self.groups, self.hosts, self.calls = {}, {}, []
        self._next = 100

    def _id(self):
        self._next += 1
        return str(self._next)

    async def call(self, method, params=None):
        self.calls.append(method)
        if method == "template.get":
            return [{"templateid": self.templates[n], "name": n}
                    for n in params["filter"]["name"] if n in self.templates]
        if method == "hostgroup.get":
            return [{"groupid": self.groups[n], "name": n}
                    for n in params["filter"]["name"] if n in self.groups]
        if method == "hostgroup.create":
            self.groups[params["name"]] = gid = self._id()
            return {"groupids": [gid]}
        if method == "host.create":
            hid = self._id()
            gname = next(n for n, g in self.groups.items() if g == params["groups"][0]["groupid"])
            tname = next(n for n, t in self.templates.items() if t == params["templates"][0]["templateid"])
            self.hosts[hid] = {"hostid": hid, "status": "0", "tags": params["tags"],
                               "hostgroups": [{"name": gname}],
                               "parentTemplates": [{"name": tname}]}
            return {"hostids": [hid]}
        if method == "host.update":
            h = self.hosts[params["hostid"]]
            h["status"] = str(params["status"])
            h["tags"] = params["tags"]
            if "templates" in params:
                n = next(n for n, t in self.templates.items() if t == params["templates"][0]["templateid"])
                h["parentTemplates"] = [{"name": n}]
            return {"hostids": [params["hostid"]]}
        if method == "host.delete":
            for hid in params:
                del self.hosts[hid]
            return {"hostids": params}
        if method == "host.get":
            return [h for h in self.hosts.values()
                    if {"tag": zs.TAG_MANAGED, "value": "1"} in h["tags"]]
        raise AssertionError(method)


def dev(name, tenant="lrb", type_="cx_switch", ip="10.0.0.1"):
    return {"name": name, "tenant": tenant, "type": type_, "ip": ip, "snmp": True}


def test_dedupe_collapses_repeated_entries_for_one_device():
    devices = [dev("CRSW1"), dev("crsw1", ip="10.0.0.2"), dev("CRSW2")]
    assert [d["key"] for d in zs.dedupe_devices(devices)] == ["lrb/crsw1", "lrb/crsw2"]


def test_create_then_refresh_keeps_one_host_and_updates_last_seen():
    api = FakeApi()
    sync = zs.HostSync(api, CFG)
    first = run(sync.run([dev("CRSW1")], now=NOW))
    second = run(sync.run([dev("CRSW1")], now=NOW + timedelta(days=1)))
    assert first["created"] == 1 and second["refreshed"] == 1 and len(api.hosts) == 1
    tags = {t["tag"]: t["value"] for t in next(iter(api.hosts.values()))["tags"]}
    assert tags[zs.TAG_LAST_SEEN] == "2026-11-02T00:00:00Z"
    assert "LM/lrb" in api.groups


def test_unknown_type_uses_fallback_template():
    api = FakeApi()
    run(zs.HostSync(api, CFG).run([dev("OPN1", type_="opnsense")], now=NOW))
    assert next(iter(api.hosts.values()))["parentTemplates"] == [{"name": "ICMP Ping"}]


def test_missing_template_is_reported_not_raised():
    api = FakeApi(templates=("ICMP Ping",))
    result = run(zs.HostSync(api, CFG).run([dev("CRSW1")], now=NOW))
    assert result["created"] == 0 and "Aruba CX by SNMP" in result["errors"][0]


def test_age_out_disables_then_deletes_and_reenables_on_return():
    api = FakeApi()
    sync = zs.HostSync(api, {**CFG, "max_age_out_pct": 100})
    keep, gone = dev("KEEP"), dev("GONE", ip="10.0.0.2")
    run(sync.run([keep, gone, dev("C", ip="10.0.0.3")], now=NOW))
    day8 = NOW + timedelta(days=8)
    r = run(sync.run([keep, dev("C", ip="10.0.0.3")], now=day8))
    assert r["disabled"] == 1
    assert sorted(h["status"] for h in api.hosts.values()) == ["0", "0", "1"]
    back = run(sync.run([keep, gone, dev("C", ip="10.0.0.3")], now=day8 + timedelta(days=1)))
    assert back["refreshed"] == 3 and all(h["status"] == "0" for h in api.hosts.values())
    run(sync.run([keep, dev("C", ip="10.0.0.3")], now=day8 + timedelta(days=10)))
    purged = run(sync.run([keep, dev("C", ip="10.0.0.3")], now=day8 + timedelta(days=41)))
    assert purged["deleted"] == 1 and len(api.hosts) == 2


def test_hand_made_hosts_are_never_touched():
    api = FakeApi()
    api.hosts["1"] = {"hostid": "1", "status": "0", "tags": [], "hostgroups": [], "parentTemplates": []}
    run(zs.HostSync(api, CFG).run([], now=NOW + timedelta(days=100)))
    assert "1" in api.hosts and "host.delete" not in api.calls


def test_stalled_feed_holds_all_age_out():
    api = FakeApi()
    sync = zs.HostSync(api, CFG)
    fleet = [dev(f"SW{i}", ip=f"10.0.0.{i}") for i in range(5)]
    run(sync.run(fleet, now=NOW))
    r = run(sync.run([], now=NOW + timedelta(days=60)))
    assert r["disabled"] == 0 and "stalled feed" in r["held"][0]


def test_mass_age_out_over_percent_limit_is_held_per_tenant():
    api = FakeApi()
    sync = zs.HostSync(api, CFG)
    fleet = [dev(f"SW{i}", ip=f"10.0.0.{i}") for i in range(20)]
    run(sync.run(fleet, now=NOW))
    r = run(sync.run(fleet[:10], now=NOW + timedelta(days=8)))
    assert r["disabled"] == 0 and "exceeds 5%" in r["held"][0]


def test_dry_run_changes_nothing():
    api = FakeApi()
    r = run(zs.HostSync(api, CFG).run([dev("CRSW1")], now=NOW, dry_run=True))
    assert r["planned"]["create"] == 1 and not api.hosts
