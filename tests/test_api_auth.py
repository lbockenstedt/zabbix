import asyncio
import os
import sys

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from zabbix_api import ZabbixApi  # noqa: E402


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
