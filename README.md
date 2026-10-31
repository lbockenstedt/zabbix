# zabbix

LM spoke that mirrors LM's device inventory into a multi-tenant Zabbix server.

- LM discovers devices and pushes them with `ZBX_SYNC`; the spoke creates, refreshes, disables and deletes Zabbix hosts.
- Hosts are grouped `LM/<tenant>` and monitored through the tenant's proxy when one is configured.
- A device not seen for 7 days is disabled; 30 days later it is deleted. Safety holds protect against feed outages and mass changes.
- `ZBX_REBUILD` repopulates a fresh Zabbix entirely from the inventory, so Zabbix can be deleted and reinstalled cheaply.

## Commands

| Command | Data | Purpose |
|---|---|---|
| `UPDATE_CONFIG` | `url`, `token`, `verify_tls`, `templates`, `tenant_proxies`, `stale_days`, `delete_days`, `max_age_out_pct` | Store connection and sync settings |
| `ZBX_STATUS` | – | Zabbix version, managed host count, last sync |
| `ZBX_SYNC` | `devices`, `dry_run` | Reconcile inventory (dry run reports the plan only) |
| `ZBX_REBUILD` | `devices` | Same reconcile, used to repopulate a fresh server |

A device is `{name, tenant, type, ip, snmp}`.

## Install

    sudo ./install_zabbix.sh --hub <hub> --id <spoke-id> --secret <secret>

## Test

    pip install pytest httpx && pytest -q tests
