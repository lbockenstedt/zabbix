"""Checks on Zabbix templates the monitoring spoke is asked to install.

A template for a new device type can come from the LLM, so it is treated as
untrusted input before it reaches ``configuration.import``: one template, SNMP
polling only (an item without an explicit SNMP-style type would default to a
Zabbix agent check, and external, script, SSH, Telnet or HTTP items would run
on the server or proxy), and a size cap. Pure functions, no Zabbix calls.
"""

import re
from typing import Any, Dict, List, Optional, Tuple

MAX_BYTES = 200_000
# Item types that only read data over SNMP or from other items; anything else is refused.
ALLOWED_ITEM_TYPES = {"SNMP_AGENT", "SNMP_TRAP", "DEPENDENT", "CALCULATED"}
# Sections of a Zabbix export the install accepts; hosts, maps, media types etc. are refused.
ALLOWED_TOP_KEYS = {"version", "template_groups", "templates"}
# Words in a sysDescr that say nothing about the vendor or model.
_STOP = {"snmp", "web", "card", "management", "version", "system", "device", "network",
         "series", "the", "and", "for", "inc", "corp", "ltd", "firmware", "software", "build"}


def search_terms(sys_descr: str, limit: int = 4) -> List[str]:
    """Distinctive words of a sysDescr (vendor, model) to search template names by."""
    out: List[str] = []
    for w in re.findall(r"[A-Za-z][A-Za-z0-9-]{2,}", str(sys_descr or "")):
        if w.lower() not in _STOP and w.lower() not in (o.lower() for o in out):
            out.append(w)
        if len(out) >= limit:
            break
    return out


def _walk_items(node: Any, path: str = "") -> List[Tuple[str, Dict[str, Any]]]:
    """Every item, discovery rule and item prototype in the export, with its path."""
    found: List[Tuple[str, Dict[str, Any]]] = []
    if isinstance(node, dict):
        for key, val in node.items():
            if key in ("items", "discovery_rules", "item_prototypes") and isinstance(val, list):
                for i, it in enumerate(val):
                    if isinstance(it, dict):
                        found.append((f"{path}/{key}[{i}]", it))
                        found.extend(_walk_items(it, f"{path}/{key}[{i}]"))
            else:
                found.extend(_walk_items(val, f"{path}/{key}"))
    elif isinstance(node, list):
        for i, val in enumerate(node):
            found.extend(_walk_items(val, f"{path}[{i}]"))
    return found


def validate_template(text: str) -> Tuple[Optional[str], Optional[str]]:
    """``(template_name, None)`` when ``text`` is a safe single-template export,
    else ``(None, reason)``."""
    if not isinstance(text, str) or not text.strip():
        return None, "empty template"
    if len(text.encode("utf-8")) > MAX_BYTES:
        return None, f"template larger than {MAX_BYTES} bytes"
    try:
        import yaml  # lazy: only the template install needs it
    except ImportError:
        return None, "PyYAML is not installed on the monitoring spoke"
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as e:
        return None, f"not valid YAML: {str(e)[:120]}"
    export = doc.get("zabbix_export") if isinstance(doc, dict) else None
    if not isinstance(export, dict):
        return None, "missing zabbix_export"
    extra = set(export) - ALLOWED_TOP_KEYS
    if extra:
        return None, f"sections not allowed: {', '.join(sorted(map(str, extra)))}"
    templates = export.get("templates")
    if not isinstance(templates, list) or len(templates) != 1 or not isinstance(templates[0], dict):
        return None, "exactly one template is required"
    name = str(templates[0].get("template") or "").strip()
    if not name or len(name) > 128:
        return None, "template name missing or too long"
    for where, item in _walk_items(templates[0]):
        # No type means a Zabbix agent check in an export, so it is refused too.
        if str(item.get("type") or "") not in ALLOWED_ITEM_TYPES:
            return None, f"{where}: item type {item.get('type')!r} is not SNMP-only"
    return name, None
