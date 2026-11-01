# AGENTS.md — `zabbix`

**Zabbix monitoring module.** Keeps a multi-tenant Zabbix server in step with LM's device inventory. LM is the source of truth; Zabbix is a rebuildable sink.

- **Repo:** `github.com/lbockenstedt/zabbix`
- **Module type:** `module_type = "monitoring"`
- **Pattern mirrored:** the `truenas` spoke (see its `AGENTS.md` for fleet conventions: Python 3.11, branch flow `dev -> qa -> main`, never hand-edit `VERSION`, idempotent installers, one repo per change, PRs need an "Intent & Problem Statement").

## Layout

- `src/zabbix_sync.py` — pure `build_plan()` plus `HostSync` (create / refresh / disable / delete). All age-out rules live here and are unit-tested against an in-memory fake API.
- `src/zabbix_api.py` — async JSON-RPC client (Zabbix 7.0, bearer API token).
- `src/zabbix_engine.py` — config, status and sync entry points.
- `src/zabbix_spoke.py`, `src/control_plane.py` — hub command dispatch (`MON_STATUS`, `MON_SYNC`, `MON_REBUILD`, `UPDATE_CONFIG`).
- `install_zabbix.sh` — spoke installer.

## Rules specific to this repo

- Only hosts tagged `lm-managed=1` are ever modified or deleted. Hand-made hosts are untouched.
- Age-out mirrors NetBox: disable after 7 days unseen, delete 30 days after disabling. A stalled feed or an age-out above 5% of a tenant's fleet is held and reported, never applied.
- Device identity is `tenant/name` (lowercased); LM may list one device several times.
- Every command logs its outcome (INFO on success, an ERROR line containing "error" on failure); credentials are fully masked.
- Tenants never see Zabbix; LM's WebUI is the only front end.
