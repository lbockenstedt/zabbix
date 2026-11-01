# zabbix

LM spoke that mirrors LM's device inventory into a multi-tenant Zabbix server.

- LM discovers devices and pushes them with `MON_SYNC`; the spoke creates, refreshes, disables and deletes Zabbix hosts.
- Hosts are grouped `LM/<tenant>` and monitored through the tenant's proxy when one is configured.
- A device not seen for 7 days is disabled; 30 days later it is deleted. Safety holds protect against feed outages and mass changes.
- `MON_REBUILD` repopulates a fresh Zabbix entirely from the inventory, so Zabbix can be deleted and reinstalled cheaply.

## Commands

| Command | Data | Purpose |
|---|---|---|
| `UPDATE_CONFIG` | `url`, `token`, `verify_tls`, `templates`, `tenant_proxies`, `stale_days`, `delete_days`, `max_age_out_pct` | Store connection and sync settings |
| `MON_STATUS` | – | Zabbix version, managed host count, last sync |
| `MON_SYNC` | `devices`, `dry_run` | Reconcile inventory (dry run reports the plan only) |
| `MON_REBUILD` | `devices` | Same reconcile, used to repopulate a fresh server |

A device is `{name, tenant, type, ip, snmp}`.

## Install

    sudo ./install_zabbix.sh --hub <hub> --id <spoke-id> --secret <secret>

### Reinstalling the Zabbix server

Deploy the `zabbix-server` role (hub `load-role`; add `force: true` to re-run) or run `install_zabbix_server.sh` on the VM (flags: `--db-*`, `--timescale`, `--self-signed`, `--server-name`). The script is idempotent. It:

1. Adds the Zabbix 7.0 repo and installs the server, nginx frontend, `zabbix-agent2` and `php-pgsql`.
2. Installs `locales` and generates `en_US.UTF-8`. Without it the web UI shows "Locale for language en_US is not found on the web server". Manual fix: `locale-gen en_US.UTF-8`, then restart php-fpm and nginx.
3. With `--timescale`, pins TimescaleDB to 2.30.x (Zabbix 7.0 refuses newer).
4. Prepares the database, imports the schema, and configures nginx (self-signed HTTPS with `--self-signed`).

The installer does not set the web Admin password or create the API token. After installing:

1. Log in as `Admin` (default password `zabbix`) and change the password immediately.
2. Create an API token for a service user and send it to the hub with `UPDATE_CONFIG` (`url`, `token`, `verify_tls`, `templates`, `tenant_proxies`).
3. Re-register each proxy under the same name (for example `mipbe-default`).
4. Run `MON_REBUILD` to repopulate hosts from the LM inventory.

## Test

    pip install pytest httpx && pytest -q tests
