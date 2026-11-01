"""Spoke command dispatch: masking, config push, sync envelope, error logging."""
import asyncio
import logging
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

_core, _src, _base = (types.ModuleType(n) for n in ("core", "core.src", "core.src.base_spoke"))


class _BaseSpoke:
    def __init__(self, spoke_id, config):
        self.spoke_id, self.config = spoke_id, config

    def get_version(self):
        return "test"


_base.BaseSpoke = _BaseSpoke
sys.modules.update({"core": _core, "core.src": _src, "core.src.base_spoke": _base})

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
