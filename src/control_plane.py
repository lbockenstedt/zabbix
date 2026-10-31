# Dependency self-heal — must run before third-party imports (see truenas/control_plane.py).
import os as _os
try:
    try:
        from core.src.dep_guard import ensure_requirements as _ensure_requirements
    except ImportError:
        from dep_guard import ensure_requirements as _ensure_requirements
    _ensure_requirements(_os.path.join(
        _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "requirements.txt"))
except Exception:
    pass

import argparse
import asyncio
import logging
from typing import Dict, Any
try:
    from core.src.messaging.control_plane import BaseControlPlane
except ImportError:
    from messaging.control_plane import BaseControlPlane
from zabbix_spoke import ZabbixSpoke

try:
    from logging_setup import configure_logging
except ImportError:
    try:
        from core.src.logging_setup import configure_logging
    except ImportError:
        def configure_logging(default_level=logging.INFO, **_):
            logging.basicConfig(level=default_level, force=True,
                                format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
configure_logging()
logger = logging.getLogger("ZabbixControlPlane")


class ZabbixControlPlane(BaseControlPlane):
    """Control plane for the Zabbix (monitoring) module."""

    def get_service_name(self) -> str:
        return "lm-zabbix"

    def __init__(self, spoke_id: str, secret: str, hub_secret: str = None,
                 hub_url: str = None, config: Dict[str, Any] = None):
        self.config = config or {}
        super().__init__(spoke_id, secret, hub_secret, hub_url)
        self.module_type = "monitoring"

    async def run_hub_mode(self):
        logger.info("Starting Zabbix module in HUB MODE -> %s", self.hub_url)
        self.register_module("monitoring", ZabbixSpoke(self.spoke_id, self.config))
        await self.run()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--id", required=True, help="Spoke ID")
    parser.add_argument("--secret", default=None,
                        help="Authentication secret (required; or set SPOKE_SECRET)")
    parser.add_argument("--hub-secret", nargs='?', default="", const="",
                        help="Hub authentication secret for mutual auth")
    parser.add_argument("--hub", required=True, help="Hub WebSocket URL")
    args = parser.parse_args()
    secret = args.secret or _os.getenv("SPOKE_SECRET")
    if not secret:
        parser.error("authentication secret required: pass --secret or set SPOKE_SECRET")
    cp = ZabbixControlPlane(args.id, secret, args.hub_secret, args.hub)
    try:
        asyncio.run(cp.run_hub_mode())
    except KeyboardInterrupt:
        pass
