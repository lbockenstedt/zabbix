# zabbix

LM spoke that mirrors LM's device inventory into a multi-tenant Zabbix server.

- LM discovers devices and pushes them with `MON_SYNC`; the spoke creates, refreshes, disables and deletes Zabbix hosts.
- Hosts are grouped `LM/<tenant>` and monitored through the tenant's proxy when one is configured.
- A device not seen for 7 days is disabled; 30 days later it is deleted. Safety holds protect against feed outages and mass changes.
- `MON_REBUILD` repopulates a fresh Zabbix entirely from the inventory, so Zabbix can be deleted and reinstalled cheaply.

## Repository layout

| Path | What it holds |
| :--- | :--- |
| `.claude/` | Fleet-owned skills, synced from lm; edit them in lm. |
| `.github/` | CI (`workflows/ci.yml`), the thin `promote`/`backmerge` callers, promotion scripts and fleet-owned agents. |
| `src/` | The spoke's Python source (entry point, engine, control plane). |
| `templates/` | Zabbix templates the spoke imports (TrueNAS SNMP). |
| `tests/` | pytest suite; CI runs it in the `test` job. |
| `AGENTS.md` | Orientation for people and AI agents; the fleet rules block is synced from lm. |
| `install_zabbix.sh` | Spoke installer (idempotent; safe to re-run). |
| `install_zabbix_server.sh` | Installs a Zabbix server. |
| `install_zbxagent.sh` | Installs a Zabbix agent on a host. |
| `install_zbxproxy.sh` | Installs a Zabbix proxy. |
| `README.md` | This file. |
| `requirements.txt` | Runtime Python dependencies. |
| `VERSION` | Bot-owned version; promotion bumps it. Never edit by hand. |

## Commands

| Command | Data | Purpose |
|---|---|---|
| `UPDATE_CONFIG` | `url`, `token`, `verify_tls`, `templates`, `tenant_proxies`, `stale_days`, `delete_days`, `max_age_out_pct`, `read_only`, `read_only_saved_at` | Store connection and sync settings |
| `MON_STATUS` | – | Zabbix version, managed host count, last sync |
| `MON_SYNC` | `devices`, `dry_run` | Reconcile inventory (dry run reports the plan only) |
| `MON_REBUILD` | `devices` | Same reconcile, used to repopulate a fresh server |
| `MON_TEMPLATE_FIND` | `sys_descr` | Template names in Zabbix matching the device's vendor/model words, plus the Zabbix version (read-only) |
| `MON_TEMPLATE_INSTALL` | `template_yaml` | Import an LLM-written template. Refused unless it is one template whose items are all SNMP/dependent/calculated; never overwrites an existing template. Blocked in read-only mode |

A device is `{name, tenant, type, ip, snmp}`. An optional `template` (chosen by LM for a new device type) overrides the type's template for an SNMP device; if Zabbix does not have it the generic SNMP template is used.

**Read-only** (`read_only: true`, a dev/QA hub sharing another environment's Zabbix, set by the hub's System → Sync **Read-only NetBox & Monitoring** switch): every Zabbix API method except `*.get` and `apiinfo.version` is refused before it is sent, `MON_PROXY_REGISTER` and `MON_AGENT_REGISTER` return an ERROR, and so do `MON_SYNC` and `MON_REBUILD` unless `dry_run` is set. A client built before the Zabbix URL, token or TLS setting last changed never writes again, even once read-only is off or the spoke is pointed back at the old values ("This spoke now uses a different Zabbix URL, token or TLS setting: <method> refused", given even while read-only is on), so a sync still running against a shared Zabbix cannot write to it after the spoke is pointed elsewhere; the same URL, token and TLS setting, re-sent on every reconnect, change nothing. The refusal gives its reason as `message` and as the one entry in `errors`, so a hub that judges a sync by its `errors` records it as failed. The spoke keeps no local copy; the hub re-sends the flag on every reconnect. The hub stamps the switch with the time it was saved (`read_only_saved_at`): a value older than the one already applied (the hub's mailbox replaying a push the spoke did not answer in time, after a newer one) is ignored, and the reply echoes the flag in force. A push without the stamp (an older hub) applies as before. `read_only: null` (the hub's **Each spoke's own setting**) means writable, since this spoke has no install-time setting. A write already past that check but still on its way when read-only comes on or the connection changes (httpx waiting for a pooled connection, DNS, or the TCP and TLS handshake) is stopped and never sent ("<reason>: <method> stopped"); one stopped after it was sent may still be applied by Zabbix. A sync already running when read-only comes on lists every host whose write was refused or stopped in `errors`; none counts as done. A read-only Zabbix API token is still the real guard.

## Templates

A device's `type` picks its template through the `templates` map. The built-in default maps `truenas` to `TrueNAS SCALE by SNMP`, and a host with no mapped type gets `Network Generic Device by SNMP`. Templates Zabbix doesn't ship live in `templates/*.yaml`. Each real sync imports any that are missing from Zabbix before linking hosts. When a host's template changes, the old template is cleared, so its items stop polling.

`TrueNAS SCALE by SNMP` is built from Zabbix's `TrueNAS CORE by SNMP` and remapped to SCALE's TRUENAS-MIB. SCALE's MIB has no pool sizes and no dataset table, so pool capacity comes from LM's TrueNAS API instead.

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
