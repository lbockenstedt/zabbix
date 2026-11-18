import asyncio
import json
import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from zabbix_api import ZabbixApi, ZabbixReadOnlyError  # noqa: E402


def test_apiinfo_version_is_sent_without_auth_header():
    seen = {}

    def handler(req):
        body = req.read().decode()
        seen["apiinfo" if "apiinfo.version" in body else "other"] = req.headers.get("authorization")
        return httpx.Response(200, json={"result": "7.0"})

    async def run():
        api = ZabbixApi("http://zbx", "tok")
        api._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), headers=api._client.headers)
        await api.call("apiinfo.version")
        await api.call("host.get")

    asyncio.run(run())
    assert seen["apiinfo"] is None
    assert seen["other"] == "Bearer tok"


def test_read_only_client_sends_reads_and_refuses_writes_unsent():
    sent = []

    def handler(req):
        sent.append(json.loads(req.read())["method"])
        return httpx.Response(200, json={"result": []})

    async def run():
        api = ZabbixApi("http://zbx", "tok",
                        write_refusal=lambda: "Zabbix is read-only on this environment")
        api._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), headers=api._client.headers)
        await api.call("apiinfo.version")
        await api.call("host.get")
        for method in ("host.create", "host.update", "host.delete", "hostgroup.create",
                       "proxy.create", "configuration.import"):
            with pytest.raises(ZabbixReadOnlyError, match=method):
                await api.call(method)

    asyncio.run(run())
    assert sent == ["apiinfo.version", "host.get"]
