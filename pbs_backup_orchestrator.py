#!/usr/bin/env python3
"""Orchestrateur de sauvegardes proxmox-backup-client.

Pour chaque fichier de configuration présent dans le répertoire de
configuration :
  1. génère un script de sauvegarde (bash) dédié
  2. le pousse via SSH sur la machine cible (celle qui détient les données)
  3. l'exécute à distance, puis nettoie

ATTENTION: les fichiers de configuration ne contiennent pas de code exécuté
localement (contrairement à une version "source" en bash), mais ils peuvent
contenir des secrets (mot de passe / token PBS) : permissions 600 recommandées.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

DEFAULT_CONF_DIR = Path("/etc/pbs-backup-orchestrator/conf.d")
DEFAULT_LOCK_FILE = Path("/var/run/pbs-backup-orchestrator.lock")

log = logging.getLogger("pbs-backup")


class ConfigError(Exception):
    pass


@dataclass
class NotifyConfig:
    # "always": notifie succès et échec, "error": échec seulement, "never": jamais.
    # Nommé "when" (et non "on") car en YAML 1.1 la clé nue "on:" est
    # interprétée comme le booléen True, pas comme la chaîne "on".
    when: str = "error"
    type: str = ""  # "gotify" | "webhook" | "sendmail" | ""
    gotify: dict = field(default_factory=dict)
    webhook: dict = field(default_factory=dict)
    sendmail: dict = field(default_factory=dict)


@dataclass
class JobConfig:
    name: str

    ssh_host: str
    ssh_port: int = 22
    ssh_user: str = "root"
    ssh_key: str = ""

    pbs_repository: str = ""
    pbs_password: str = ""
    pbs_password_file: str = ""
    pbs_fingerprint: str = ""
    pbs_namespace: str = ""

    backup_sources: list[str] = field(default_factory=list)
    backup_id: str = ""
    extra_opts: list[str] = field(default_factory=list)

    exclude_nobackup_marker: bool = True
    nobackup_marker_name: str = ".nobackup"

    pre_backup_cmd: str = ""
    post_backup_cmd: str = ""

    remote_tmp_dir: str = "/root/.pbs-backup"

    notify: NotifyConfig = field(default_factory=NotifyConfig)

    @property
    def target(self) -> str:
        return f"{self.ssh_user}@{self.ssh_host}"

    def resolved_password(self) -> str:
        if self.pbs_password_file:
            return Path(self.pbs_password_file).read_text().strip()
        return self.pbs_password


def _section(data: dict, name: str) -> dict:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"la section '{name}' doit être un objet/mapping")
    return value


def _load_data(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")

    if path.suffix.lower() in (".yml", ".yaml"):
        try:
            import yaml
        except ImportError as exc:
            raise ConfigError(
                f"{path}: PyYAML n'est pas installé (pip install pyyaml) "
                "pour lire un fichier YAML"
            ) from exc
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ConfigError(f"{path}: YAML invalide ({exc})") from exc
    else:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{path}: JSON invalide ({exc})") from exc

    if not isinstance(data, dict):
        raise ConfigError(f"{path}: le document doit être un objet/mapping au premier niveau")
    return data


def load_job_config(path: Path) -> JobConfig:
    data = _load_data(path)

    ssh = _section(data, "ssh")
    pbs = _section(data, "pbs")
    backup = _section(data, "backup")
    nobackup = _section(data, "nobackup_marker")
    hooks = _section(data, "hooks")
    notify = _section(data, "notify")

    ssh_host = str(ssh.get("host", "")).strip()
    pbs_repository = str(pbs.get("repository", "")).strip()
    if not ssh_host:
        raise ConfigError(f"{path}: ssh.host manquant")
    if not pbs_repository:
        raise ConfigError(f"{path}: pbs.repository manquant")

    backup_sources = [str(s).strip() for s in backup.get("sources", []) if str(s).strip()]
    if not backup_sources:
        raise ConfigError(f"{path}: backup.sources est vide")

    extra_opts = [str(o).strip() for o in backup.get("extra_opts", []) if str(o).strip()]

    notify_when = str(notify.get("when", "error")).strip().lower() or "error"
    if notify_when not in ("always", "error", "never"):
        raise ConfigError(
            f"{path}: notify.when invalide ({notify_when!r}), attendu 'always', 'error' ou 'never'"
        )
    notify_type = str(notify.get("type", "")).strip().lower()
    if notify_type and notify_type not in ("gotify", "webhook", "sendmail"):
        raise ConfigError(
            f"{path}: notify.type invalide ({notify_type!r}), attendu 'gotify', 'webhook' ou 'sendmail'"
        )
    notify_cfg = NotifyConfig(
        when=notify_when,
        type=notify_type,
        gotify=_section(notify, "gotify"),
        webhook=_section(notify, "webhook"),
        sendmail=_section(notify, "sendmail"),
    )

    return JobConfig(
        name=path.stem,
        ssh_host=ssh_host,
        ssh_port=int(ssh.get("port", 22)),
        ssh_user=str(ssh.get("user", "root")),
        ssh_key=str(ssh.get("key", "")),
        pbs_repository=pbs_repository,
        pbs_password=str(pbs.get("password", "")),
        pbs_password_file=str(pbs.get("password_file", "")),
        pbs_fingerprint=str(pbs.get("fingerprint", "")),
        pbs_namespace=str(pbs.get("namespace", "")),
        backup_sources=backup_sources,
        backup_id=str(backup.get("backup_id", "")),
        extra_opts=extra_opts,
        exclude_nobackup_marker=bool(nobackup.get("enabled", True)),
        nobackup_marker_name=str(nobackup.get("name", ".nobackup")),
        pre_backup_cmd=str(hooks.get("pre_backup", "")),
        post_backup_cmd=str(hooks.get("post_backup", "")),
        remote_tmp_dir=str(backup.get("remote_tmp_dir", "/root/.pbs-backup")),
        notify=notify_cfg,
    )


# Fonction bash (portable BSD/GNU) injectée telle quelle dans le script
# distant : exclut tout répertoire contenant le fichier marqueur.
_NOBACKUP_FUNCTION = """\
add_nobackup_excludes() {
    local root="$1" dir rel
    [ -d "$root" ] || return 0
    while IFS= read -r dir; do
        rel="${dir#$root}"
        rel="${rel%/}"
        if [ -z "$rel" ]; then
            EXCLUDE_ARGS+=(--exclude "/")
        else
            EXCLUDE_ARGS+=(--exclude "${rel}/")
        fi
    done < <(find "$root" -type f -name "$NOBACKUP_MARKER" -exec dirname {} \\; | sort -u)
}\
"""


def generate_remote_script(cfg: JobConfig) -> str:
    lines: list[str] = ["#!/usr/bin/env bash", "set -euo pipefail", ""]

    lines.append(f"export PBS_REPOSITORY={shlex.quote(cfg.pbs_repository)}")
    password = cfg.resolved_password()
    if password:
        lines.append(f"export PBS_PASSWORD={shlex.quote(password)}")
    if cfg.pbs_fingerprint:
        lines.append(f"export PBS_FINGERPRINT={shlex.quote(cfg.pbs_fingerprint)}")
    if cfg.pbs_namespace:
        lines.append(f"export PBS_NAMESPACE={shlex.quote(cfg.pbs_namespace)}")
    lines.append("")

    # Le hook post-sauvegarde s'exécute toujours à la sortie du script (succès,
    # échec de la sauvegarde ou du hook pre-sauvegarde), sans masquer le code
    # de sortie d'origine.
    if cfg.post_backup_cmd:
        lines.append(f"POST_BACKUP_CMD={shlex.quote(cfg.post_backup_cmd)}")
        lines.append(
            'trap \'rc=$?; bash -c "$POST_BACKUP_CMD" '
            '|| echo "WARNING: la commande post-sauvegarde a échoué" >&2; exit $rc\' EXIT'
        )
        lines.append("")

    if cfg.pre_backup_cmd:
        lines.append(f"PRE_BACKUP_CMD={shlex.quote(cfg.pre_backup_cmd)}")
        lines.append('bash -c "$PRE_BACKUP_CMD"')
        lines.append("")

    lines.append("declare -a EXCLUDE_ARGS=()")
    if cfg.exclude_nobackup_marker:
        lines.append(f"NOBACKUP_MARKER={shlex.quote(cfg.nobackup_marker_name)}")
        lines.append(_NOBACKUP_FUNCTION)
        for src in cfg.backup_sources:
            _, _, src_path = src.partition(":")
            lines.append(f"add_nobackup_excludes {shlex.quote(src_path)}")
    lines.append("")

    cmd = ["proxmox-backup-client", "backup"]
    cmd.extend(cfg.backup_sources)
    if cfg.backup_id:
        cmd.extend(["--backup-id", cfg.backup_id])
    quoted_cmd = " \\\n  ".join(shlex.quote(part) for part in cmd)
    lines.append(f"{quoted_cmd} \\")
    exclude_expansion = '  ${EXCLUDE_ARGS[@]+"${EXCLUDE_ARGS[@]}"}'
    if cfg.extra_opts:
        lines.append(exclude_expansion + " \\")
        lines.append("  " + " ".join(cfg.extra_opts))
    else:
        lines.append(exclude_expansion)

    return "\n".join(lines) + "\n"


def ssh_base_args(cfg: JobConfig) -> list[str]:
    args = ["ssh", "-p", str(cfg.ssh_port), "-o", "BatchMode=yes"]
    if cfg.ssh_key:
        args.extend(["-i", cfg.ssh_key])
    return args


def push_and_run(cfg: JobConfig, script_text: str) -> tuple[bool, str]:
    remote_dir = cfg.remote_tmp_dir
    remote_script = f"{remote_dir}/{cfg.name}-{Path(tempfile.mktemp()).name}.sh"

    ssh_args = ssh_base_args(cfg)
    target = cfg.target

    push_cmd = (
        f"mkdir -p {shlex.quote(remote_dir)} && chmod 700 {shlex.quote(remote_dir)} && "
        f"cat > {shlex.quote(remote_script)} && chmod 700 {shlex.quote(remote_script)}"
    )
    try:
        subprocess.run(
            [*ssh_args, target, push_cmd],
            input=script_text.encode(),
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        log.error("[%s] échec du push SSH: %s", cfg.name, exc)
        return False, f"Échec du push SSH vers {target}: {exc}"

    exec_cmd = f"{shlex.quote(remote_script)}; rc=$?; rm -f {shlex.quote(remote_script)}; exit $rc"
    result = subprocess.run([*ssh_args, target, exec_cmd])
    if result.returncode != 0:
        log.error("[%s] échec de la sauvegarde (code %s)", cfg.name, result.returncode)
        return False, f"proxmox-backup-client a échoué sur {target} (code retour {result.returncode})."
    return True, f"Sauvegarde terminée avec succès sur {target}."


def _notify_gotify(cfg: JobConfig, subject: str, body: str) -> None:
    server = str(cfg.notify.gotify.get("server", "")).rstrip("/")
    token = str(cfg.notify.gotify.get("token", ""))
    if not server or not token:
        log.warning("[%s] notification gotify ignorée: server/token manquant", cfg.name)
        return
    priority = int(cfg.notify.gotify.get("priority", 5))
    payload = json.dumps({"title": subject, "message": body, "priority": priority}).encode()
    url = f"{server}/message?token={urllib.parse.quote(token)}"
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except (urllib.error.URLError, OSError) as exc:
        log.error("[%s] échec de la notification gotify: %s", cfg.name, exc)


def _notify_webhook(cfg: JobConfig, subject: str, body: str, ok: bool) -> None:
    url = str(cfg.notify.webhook.get("url", ""))
    if not url:
        log.warning("[%s] notification webhook ignorée: url manquante", cfg.name)
        return
    method = str(cfg.notify.webhook.get("method", "POST")).upper()
    headers = {str(k): str(v) for k, v in cfg.notify.webhook.get("headers", {}).items()}
    headers.setdefault("Content-Type", "application/json")
    payload = json.dumps(
        {
            "job": cfg.name,
            "host": cfg.ssh_host,
            "status": "ok" if ok else "error",
            "subject": subject,
            "message": body,
        }
    ).encode()
    req = urllib.request.Request(url, data=payload, headers=headers, method=method)
    try:
        urllib.request.urlopen(req, timeout=10)
    except (urllib.error.URLError, OSError) as exc:
        log.error("[%s] échec de la notification webhook: %s", cfg.name, exc)


def _notify_sendmail(cfg: JobConfig, subject: str, body: str) -> None:
    mailto = cfg.notify.sendmail.get("mailto", [])
    if isinstance(mailto, str):
        mailto = [mailto]
    mailto = [str(m).strip() for m in mailto if str(m).strip()]
    if not mailto:
        log.warning("[%s] notification sendmail ignorée: mailto manquant", cfg.name)
        return
    mailfrom = str(cfg.notify.sendmail.get("mailfrom", f"pbs-backup@{socket.gethostname()}"))

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = mailfrom
    msg["To"] = ", ".join(mailto)
    msg.set_content(body)

    sendmail_bin = shutil.which("sendmail") or "/usr/sbin/sendmail"
    try:
        subprocess.run([sendmail_bin, "-t", "-oi"], input=msg.as_bytes(), check=True)
    except (subprocess.CalledProcessError, FileNotFoundError, OSError) as exc:
        log.error("[%s] échec de la notification sendmail: %s", cfg.name, exc)


def send_notification(cfg: JobConfig, ok: bool, message: str) -> None:
    if cfg.notify.type in ("", "none"):
        return
    if cfg.notify.when == "never":
        return
    if cfg.notify.when == "error" and ok:
        return

    subject = f"[pbs-backup] {cfg.name}: {'OK' if ok else 'ECHEC'}"
    body = (
        f"Job: {cfg.name}\n"
        f"Hôte: {cfg.ssh_host}\n"
        f"Statut: {'succès' if ok else 'échec'}\n"
        f"Horodatage: {datetime.now().isoformat(timespec='seconds')}\n\n"
        f"{message}\n"
    )

    if cfg.notify.type == "gotify":
        _notify_gotify(cfg, subject, body)
    elif cfg.notify.type == "webhook":
        _notify_webhook(cfg, subject, body, ok)
    elif cfg.notify.type == "sendmail":
        _notify_sendmail(cfg, subject, body)


def run_job(conf_file: Path, dry_run: bool) -> bool:
    try:
        cfg = load_job_config(conf_file)
    except ConfigError as exc:
        log.error(str(exc))
        return False

    script_text = generate_remote_script(cfg)

    if dry_run:
        print(f"---- [{cfg.name}] script généré (dry-run, non exécuté) ----")
        print(script_text)
        return True

    ok, message = push_and_run(cfg, script_text)
    send_notification(cfg, ok, message)
    return ok


def acquire_lock(lock_file: Path):
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    fh = lock_file.open("w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.error("Une autre exécution est déjà en cours (verrou %s)", lock_file)
        sys.exit(1)
    return fh


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("-c", "--conf-dir", type=Path, default=DEFAULT_CONF_DIR,
                         help=f"Répertoire des fichiers *.json/*.yml/*.yaml (def: {DEFAULT_CONF_DIR})")
    parser.add_argument("-j", "--job", default=None,
                         help="Ne traiter que ce job (nom du fichier sans extension)")
    parser.add_argument("-n", "--dry-run", action="store_true",
                         help="Générer les scripts sans les pousser/exécuter")
    parser.add_argument("--lock-file", type=Path, default=DEFAULT_LOCK_FILE)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%F %T")

    acquire_lock(args.lock_file)

    all_conf_files = sorted(
        (*args.conf_dir.glob("*.json"), *args.conf_dir.glob("*.yml"), *args.conf_dir.glob("*.yaml")),
        key=lambda p: p.name,
    )
    if not all_conf_files:
        log.info("Aucun fichier de configuration trouvé dans %s", args.conf_dir)
        return 0

    conf_files: list[Path] = []
    seen_names: dict[str, Path] = {}
    for conf_file in all_conf_files:
        job_name = conf_file.stem
        if job_name in seen_names:
            log.warning(
                "Job '%s' ignoré (%s) : déjà défini par %s",
                job_name, conf_file.name, seen_names[job_name].name,
            )
            continue
        seen_names[job_name] = conf_file
        conf_files.append(conf_file)

    overall_ok = True
    for conf_file in conf_files:
        job_name = conf_file.stem
        if args.job and job_name != args.job:
            continue
        log.info("=== Démarrage du job: %s ===", job_name)
        ok = run_job(conf_file, args.dry_run)
        if ok:
            log.info("=== Job %s OK ===", job_name)
        else:
            log.error("=== Job %s EN ECHEC ===", job_name)
            overall_ok = False

    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main())
