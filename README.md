# pbs_backup — Job configuration file

*[Version française](README.fr.md)*

`pbs_backup_orchestrator.py` runs [Proxmox Backup Server
(PBS)](https://pbs.proxmox.com/) backups for remote machines. For each
machine to back up, it reads a **configuration file** (a "job"), generates
a bash script, pushes it over SSH to the target machine, and runs it with
`proxmox-backup-client`.

This document describes the format of that configuration file.

## Location and naming

- Job files live in the configuration directory (`CONF_DIR`, default
  `/etc/pbs-backup/conf.d`).
- Accepted formats: `.yaml` / `.yml` or `.json`.
- **The job name is the file name without its extension.** For example,
  `web01.yaml` defines the job `web01`.
- If two files define the same job name (e.g. `web01.json` and
  `web01.yaml`), the second one is skipped with a warning.
- These files may contain secrets (PBS password / token): apply
  `chmod 600`.

Commented examples are provided in `conf.d/example.yaml.sample` and
`conf.d/example.json.sample`.

## Structure

The file is an object/mapping with five sections: `ssh`, `pbs`, `backup`,
`nobackup_marker`, and `hooks`.

### `ssh` — access to the target machine

| Key    | Type   | Required | Default | Description                              |
|--------|--------|----------|---------|--------------------------------------------|
| `host` | string | yes      | —       | SSH host of the machine to back up         |
| `port` | int    | no       | `22`    | SSH port                                   |
| `user` | string | no       | `root`  | SSH user                                   |
| `key`  | string | no       | —       | Path to the SSH private key (`ssh -i`)     |

The connection uses `BatchMode=yes` (no interactive prompt): key-based
authentication must already be set up.

### `pbs` — Proxmox Backup Server target

| Key             | Type   | Required | Default | Description                                                       |
|-----------------|--------|----------|---------|----------------------------------------------------------------------|
| `repository`    | string | yes      | —       | PBS repository, format `user@realm!token@host:datastore`          |
| `password`      | string | no       | —       | PBS secret (password or token value) stored in plain text         |
| `password_file` | string | no       | —       | File containing the PBS secret (alternative to `password`)        |
| `fingerprint`   | string | no       | —       | TLS fingerprint of the PBS server                                  |
| `namespace`     | string | no       | —       | Target PBS namespace                                                |

An **API token** is recommended over a user password. If `password_file`
is set, it takes precedence over `password` (the file is read at
execution time, avoiding storing the secret in clear text in the config
file). These values are exported as environment variables
(`PBS_REPOSITORY`, `PBS_PASSWORD`, `PBS_FINGERPRINT`, `PBS_NAMESPACE`) in
the script run on the target machine.

### `backup` — sources to back up

| Key              | Type         | Required | Default             | Description                                                             |
|------------------|--------------|----------|----------------------|----------------------------------------------------------------------------|
| `sources`        | list[string] | yes      | —                    | Sources to back up, format `archive-name.pxar:/path` (or `name.img:/dev/xxx` for a disk) |
| `backup_id`      | string       | no       | —                    | Backup identifier passed to `--backup-id`                              |
| `extra_opts`     | list[string] | no       | `[]`                 | Extra options appended as-is to the `proxmox-backup-client backup` command |
| `remote_tmp_dir` | string       | no       | `/root/.pbs-backup`  | Temporary directory on the target machine to drop the generated script |

`sources` must contain at least one entry.

### `nobackup_marker` — exclusion via marker file

| Key       | Type   | Required | Default      | Description                                                          |
|-----------|--------|----------|--------------|--------------------------------------------------------------------------|
| `enabled` | bool   | no       | `true`       | If enabled, any directory containing this marker file is excluded    |
| `name`    | string | no       | `.nobackup`  | Name of the marker file looked for on the target machine             |

The lookup runs on the target machine at backup time, for each source
path declared in `backup.sources`.

### `hooks` — pre/post backup commands

| Key           | Type   | Required | Default | Description                                    |
|---------------|--------|----------|---------|---------------------------------------------------|
| `pre_backup`  | string | no       | —       | Command run before the backup (`bash -c`)          |
| `post_backup` | string | no       | —       | Command run after the backup (`bash -c`)           |

- `pre_backup` must succeed for the backup to start.
- `post_backup` **always** runs (whether the backup or the `pre_backup`
  hook succeeded or failed) and does not affect the job's exit code.

These commands are executed **on the target machine**, not on the machine
hosting the orchestrator.

## Minimal example (YAML)

```yaml
ssh:
  host: web01.example.com
  key: /root/.ssh/id_pbs_backup

pbs:
  repository: "backup@pbs!token@pbs.example.com:datastore1"
  password_file: /etc/pbs-backup/secrets/web01.token
  fingerprint: "AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99"

backup:
  sources:
    - "root.pxar:/"
    - "etc.pxar:/etc"
  extra_opts:
    - "--exclude /var/tmp"
    - "--exclude /tmp"
```

## Usage

```bash
# Run all jobs in the config directory
pbs-backup-orchestrator

# Run only one job
pbs-backup-orchestrator --job web01

# Generate the scripts without pushing/running them (dry check)
pbs-backup-orchestrator --dry-run

# Use a different configuration directory
pbs-backup-orchestrator --conf-dir /path/to/conf.d
```

See `install.sh` for installation on Debian (dedicated Python venv,
`/usr/local/bin/pbs-backup-orchestrator` launcher).
