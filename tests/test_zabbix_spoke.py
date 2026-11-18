"""Spoke command dispatch: masking, config push, sync envelope, error logging."""
import asyncio
import json
import logging
import os
import sys
import types

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

_core, _src, _base = (types.ModuleType(n) for n in ("core", "core.src", "core.src.base_spoke"))


class _BaseSpoke:
    def __init__(self, spoke_id, config):
        self.spoke_id, self.config = spoke_id, config

    def get_version(self):
        return "test"


_base.BaseSpoke = _BaseSpoke
sys.modules.update({"core": _core, "core.src": _src, "core.src.base_spoke": _base})

from zabbix_api import ZabbixReadOnlyError  # noqa: E402
from zabbix_spoke import ZabbixSpoke  # noqa: E402


def run(coro):
    return asyncio.run(coro)


def test_mask_hides_credentials():
    assert ZabbixSpoke._mask({"token": "abc", "url": "u"}) == {"token": "********", "url": "u"}


def test_sync_requires_devices_and_logs_error(caplog):
    spoke = ZabbixSpoke("z1", {})
    with caplog.at_level(logging.ERROR, logger="ZabbixSpoke"):
        res = run(spoke.handle_command("MON_SYNC", {}))
    assert res["status"] == "ERROR" and "error" in caplog.text


def test_unconfigured_sync_returns_error_envelope():
    res = run(ZabbixSpoke("z1", {}).handle_command("MON_SYNC", {"devices": []}))
    assert res["status"] == "ERROR" and "not configured" in res["message"]


def test_update_config_then_unknown_command():
    spoke = ZabbixSpoke("z1", {})
    assert run(spoke.handle_command("update_config", {"url": "http://z", "token": "t"}))["status"] == "SUCCESS"
    assert run(spoke.handle_command("BOGUS", {}))["status"] == "ERROR"


def test_update_config_applies_and_echoes_read_only():
    spoke = ZabbixSpoke("z1", {})
    res = run(spoke.handle_command("UPDATE_CONFIG", {"url": "http://z", "token": "t", "read_only": True}))
    assert res["status"] == "SUCCESS" and res["read_only"] is True
    assert spoke.engine.read_only is True
    # A later push without the flag keeps it (config merge) and does not echo it.
    res = run(spoke.handle_command("UPDATE_CONFIG", {"stale_days": 5}))
    assert "read_only" not in res and spoke.engine.read_only is True
    res = run(spoke.handle_command("UPDATE_CONFIG", {"read_only": False}))
    assert res["read_only"] is False and spoke.engine.read_only is False


def test_an_older_read_only_value_never_undoes_a_newer_one():
    """The hub's mailbox can replay an older push after a newer one."""
    spoke = ZabbixSpoke("z1", {"url": "http://z", "token": "t"})
    run(spoke.handle_command("UPDATE_CONFIG", {"read_only": True, "read_only_saved_at": 2.0}))
    res = run(spoke.handle_command("UPDATE_CONFIG", {"read_only": False, "read_only_saved_at": 1.0,
                                                     "stale_days": 9}))
    # Ignored, and the reply reports the flag in force; the rest still applies.
    assert res["status"] == "SUCCESS" and res["read_only"] is True
    assert spoke.engine.read_only is True and spoke.engine.config["stale_days"] == 9
    # The same or a newer stamp applies, and so does a push without one (an older hub).
    for flag, stamp in ((False, 2.0), (True, 3.0), (False, None)):
        push = {"read_only": flag} if stamp is None else {"read_only": flag, "read_only_saved_at": stamp}
        assert run(spoke.handle_command("UPDATE_CONFIG", push))["read_only"] is flag
    assert spoke.engine.read_only is False


def test_a_running_sync_stops_writing_once_read_only():
    # A sync started before the switch keeps the client it already holds;
    # UPDATE_CONFIG replaces the engine's client, not that one.
    sent = []

    def handler(req):
        sent.append(json.loads(req.read())["method"])
        return httpx.Response(200, json={"result": []})

    async def scenario():
        spoke = ZabbixSpoke("z1", {"url": "http://z", "token": "t"})
        held = spoke.engine._client()
        held._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        await held.call("host.create")
        await spoke.handle_command("UPDATE_CONFIG", {"read_only": True})
        with pytest.raises(ZabbixReadOnlyError):
            await held.call("host.update")
        await held.call("host.get")
        # Switching read-only off lets the held client write again.
        await spoke.handle_command("UPDATE_CONFIG", {"read_only": False})
        await held.call("host.delete")

    run(scenario())
    assert sent == ["host.create", "host.get", "host.delete"]


@pytest.mark.parametrize("pushes, refused, new_host", [
    ([{"url": "http://dev"}], True, "dev"),
    ([{"token": "t2"}], True, "prod"),
    ([{"verify_tls": False}], True, "prod"),
    # Back to the Zabbix the client came from: still a client from before.
    ([{"url": "http://dev"}, {"url": "http://prod"}], True, "prod"),
    # The hub re-sends the same URL and token on every reconnect.
    ([{"url": "http://prod", "token": "t"}], False, "prod"),
], ids=["other-url", "other-token", "tls-change", "away-and-back", "same-config"])
def test_a_client_held_across_a_repoint_writes_only_while_the_connection_is_unchanged(
        pushes, refused, new_host):
    # One push can point a dev spoke away from a shared Zabbix and turn
    # read-only off: a sync still holding the old client must not then write
    # anywhere, even once the spoke moves back. A client built after the push
    # writes as normal.
    sent = []

    def handler(req):
        sent.append((req.url.host, json.loads(req.read())["method"]))
        return httpx.Response(200, json={"result": []})

    async def scenario():
        spoke = ZabbixSpoke("z1", {"url": "http://prod", "token": "t", "read_only": True})
        held = spoke.engine._client()
        held._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        for push in pushes:
            await spoke.handle_command("UPDATE_CONFIG", push)
        if refused:
            # The reason is the connection change, which turning read-only off
            # does not end, both while read-only is on and after.
            for read_only in (True, False):
                await spoke.handle_command("UPDATE_CONFIG", {"read_only": read_only})
                with pytest.raises(ZabbixReadOnlyError,
                                   match="different Zabbix URL, token or TLS setting: host.delete"):
                    await held.call("host.delete")
        else:
            with pytest.raises(ZabbixReadOnlyError, match="read-only on this environment: host.delete"):
                await held.call("host.delete")
            await spoke.handle_command("UPDATE_CONFIG", {"read_only": False})
            await held.call("host.delete")
        await held.call("host.get")
        new = spoke.engine._client()
        new._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        await new.call("host.delete")

    run(scenario())
    expected = [("prod", "host.get"), (new_host, "host.delete")]
    assert sent == (expected if refused else [("prod", "host.delete")] + expected)


def _gated_client(api, sent):
    """Give ``api`` a transport that waits at a gate before it sends, the way
    httpx can wait for a pooled connection, DNS or a TCP/TLS handshake after
    the write check. Returns (waiting, gate): set once a request waits, and
    the event that lets it go out (recorded in ``sent``)."""
    waiting, gate = asyncio.Event(), asyncio.Event()

    async def handler(req):
        waiting.set()
        await gate.wait()
        sent.append((req.url.host, json.loads(req.content)["method"]))
        return httpx.Response(200, json={"result": []})

    api._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return waiting, gate


@pytest.mark.parametrize("push, stopped", [
    ({"read_only": True}, "read-only on this environment"),
    ({"url": "http://dev"}, "different Zabbix URL, token or TLS setting"),
    # The hub re-sends the same config on every reconnect: the write goes on.
    ({"url": "http://prod", "token": "t"}, None),
], ids=["read-only-on", "repoint", "same-config"])
def test_a_write_still_waiting_to_be_sent_stops_when_writes_are_refused(push, stopped):
    # The write passed its check before the push, but had not gone out yet.
    sent = []

    async def scenario():
        spoke = ZabbixSpoke("z1", {"url": "http://prod", "token": "t"})
        api = spoke.engine._client()
        waiting, gate = _gated_client(api, sent)
        write = asyncio.ensure_future(api.call("host.delete", ["1"]))
        await waiting.wait()
        await spoke.handle_command("UPDATE_CONFIG", push)
        gate.set()
        if stopped:
            with pytest.raises(ZabbixReadOnlyError, match=f"{stopped}: host.delete stopped"):
                await write
        else:
            await write
        assert not spoke.engine._writes

    run(scenario())
    assert sent == ([] if stopped else [("prod", "host.delete")])


def test_a_cancelled_caller_still_sees_its_own_cancellation():
    # Only the engine's stop becomes ZabbixReadOnlyError: a sync cancelled by
    # its caller (a timeout, a shutdown) is still cancelled.
    sent = []

    async def scenario():
        spoke = ZabbixSpoke("z1", {"url": "http://z", "token": "t"})
        api = spoke.engine._client()
        waiting, _gate = _gated_client(api, sent)
        write = asyncio.ensure_future(api.call("host.delete"))
        await waiting.wait()
        write.cancel()
        with pytest.raises(asyncio.CancelledError):
            await write
        assert not spoke.engine._writes

    run(scenario())
    assert sent == []


def test_read_only_null_from_the_hub_means_writable():
    # The hub sends read_only: null for "each spoke's own setting". This spoke
    # has no install-time setting, so null means writable.
    spoke = ZabbixSpoke("z1", {"url": "http://z", "token": "t", "read_only": True,
                               "read_only_saved_at": 1.0})
    res = run(spoke.handle_command("UPDATE_CONFIG", {"read_only": None, "read_only_saved_at": 2.0}))
    assert res["status"] == "SUCCESS" and res["read_only"] is False
    assert spoke.engine.read_only is False


def test_read_only_spoke_refuses_writes_but_runs_a_dry_run():
    spoke = ZabbixSpoke("z1", {"url": "http://z", "token": "t", "read_only": True})
    calls = []

    async def sync(devices, dry_run=False):
        calls.append(dry_run)
        return {"errors": [], "dry_run": dry_run}
    spoke.engine.sync = sync
    # Registration has no dry run, so dry_run never lets it through.
    for cmd, data in (("MON_SYNC", {"devices": []}), ("MON_REBUILD", {"devices": []}),
                      ("MON_PROXY_REGISTER", {"name": "p", "dry_run": True}),
                      ("MON_AGENT_REGISTER", {"tenant": "t", "hostname": "h", "dry_run": True})):
        res = run(spoke.handle_command(cmd, data))
        assert res["status"] == "ERROR" and "read-only" in res["message"], cmd
        # The hub judges a sync by its "errors" list; an empty one reads as success.
        assert res["errors"] == [res["message"]], cmd
    assert calls == []
    res = run(spoke.handle_command("MON_SYNC", {"devices": [], "dry_run": True}))
    assert res["status"] == "SUCCESS" and calls == [True]
