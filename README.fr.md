# Installation 

Un paquet .deb est fourni pour l'installation à l'aide de dpkg.

Vous devez générer une clé SSH pour root 
```
ssh-keygen -t ed25519 -f /root/.ssh/id_pbs_backup
``` 
Installer la clé publique (.pub) sur la machine à sauvegarder.


# pbs_backup_orchestrator — Fichier de configuration d'un job

*[English version](README.md)*

`pbs_backup_orchestrator.py` exécute des sauvegardes [Proxmox Backup Server
(PBS)](https://pbs.proxmox.com/) pour des machines distantes. Pour chaque
machine à sauvegarder, il lit un **fichier de configuration** (un "job"),
génère un script bash, le pousse via SSH sur la machine cible et l'exécute
avec `proxmox-backup-client`.

Ce document décrit le format de ce fichier de configuration.

## Emplacement et nommage

- Les fichiers de job vivent dans le répertoire de configuration
  (`CONF_DIR`, par défaut `/etc/pbs-backup/conf.d`).
- Formats acceptés : `.yaml` / `.yml` ou `.json`.
- **Le nom du job est le nom du fichier, sans extension.** Par exemple
  `web01.yaml` définit le job `web01`.
- Si deux fichiers définissent le même nom de job (ex: `web01.json` et
  `web01.yaml`), le second est ignoré avec un avertissement.
- Ces fichiers peuvent contenir des secrets (mot de passe / token PBS) :
  appliquer `chmod 600`.

Des exemples commentés sont fournis dans `conf.d/example.yaml.sample` et
`conf.d/example.json.sample`.

## Structure

Le fichier est un objet/mapping avec six sections : `ssh`, `pbs`, `backup`,
`nobackup_marker`, `hooks` et `notify`.

### `ssh` — accès à la machine cible

| Clé    | Type   | Obligatoire | Défaut | Description                                    |
|--------|--------|-------------|--------|-------------------------------------------------|
| `host` | string | oui         | —      | Hôte SSH de la machine à sauvegarder            |
| `port` | int    | non         | `22`   | Port SSH                                        |
| `user` | string | non         | `root` | Utilisateur SSH                                 |
| `key`  | string | non         | —      | Chemin de la clé privée SSH (`ssh -i`)          |

La connexion se fait en mode `BatchMode=yes` (pas de prompt interactif) :
l'authentification par clé doit être déjà en place.

### `pbs` — cible Proxmox Backup Server

| Clé             | Type   | Obligatoire | Défaut | Description                                                        |
|-----------------|--------|-------------|--------|----------------------------------------------------------------------|
| `repository`    | string | oui         | —      | Dépôt PBS, format `user@realm!token@host:datastore`                |
| `password`      | string | non         | —      | Secret PBS (mot de passe ou valeur du token) en clair               |
| `password_file` | string | non         | —      | Fichier contenant le secret PBS (alternative à `password`)          |
| `fingerprint`   | string | non         | —      | Empreinte TLS du serveur PBS                                        |
| `namespace`     | string | non         | —      | Namespace PBS cible                                                  |

Un **token API** est recommandé plutôt qu'un mot de passe utilisateur.
Si `password_file` est renseigné, il est préféré à `password` (le fichier
est lu au moment de l'exécution, ce qui évite de stocker le secret en clair
dans le fichier de config). Ces valeurs sont exportées comme variables
d'environnement (`PBS_REPOSITORY`, `PBS_PASSWORD`, `PBS_FINGERPRINT`,
`PBS_NAMESPACE`) dans le script exécuté sur la machine cible.

### `backup` — sources à sauvegarder

| Clé              | Type          | Obligatoire | Défaut               | Description                                                        |
|------------------|---------------|-------------|-----------------------|----------------------------------------------------------------------|
| `sources`        | liste[string] | oui         | —                     | Sources à sauvegarder, format `nom-archive.pxar:/chemin` (ou `nom.img:/dev/xxx` pour un disque) |
| `backup_id`      | string        | non         | —                     | Identifiant de sauvegarde passé à `--backup-id`                     |
| `extra_opts`     | liste[string] | non         | `[]`                  | Options supplémentaires ajoutées telles quelles à la commande `proxmox-backup-client backup` |
| `remote_tmp_dir` | string        | non         | `/root/.pbs-backup`   | Répertoire temporaire sur la machine cible pour y déposer le script généré |

`sources` doit contenir au moins une entrée.

### `nobackup_marker` — exclusion par fichier marqueur

| Clé       | Type   | Obligatoire | Défaut       | Description                                                              |
|-----------|--------|-------------|--------------|----------------------------------------------------------------------------|
| `enabled` | bool   | non         | `true`       | Si activé, tout répertoire contenant ce fichier marqueur est exclu        |
| `name`    | string | non         | `.nobackup`  | Nom du fichier marqueur recherché sur la machine cible                    |

La recherche est effectuée sur la machine cible au moment de la sauvegarde,
pour chaque chemin source déclaré dans `backup.sources`.

### `hooks` — commandes pré/post sauvegarde

| Clé           | Type   | Obligatoire | Défaut | Description                                       |
|---------------|--------|-------------|--------|-----------------------------------------------------|
| `pre_backup`  | string | non         | —      | Commande exécutée avant la sauvegarde (`bash -c`)   |
| `post_backup` | string | non         | —      | Commande exécutée après la sauvegarde (`bash -c`)   |

- `pre_backup` doit réussir pour que la sauvegarde démarre.
- `post_backup` s'exécute **toujours** (succès ou échec de la sauvegarde ou
  du hook `pre_backup`) et n'affecte pas le code de sortie du job.

Ces commandes sont exécutées **sur la machine cible**, pas sur la machine
qui héberge l'orchestrateur.

### `notify` — notifications de succès/échec

Envoyées par l'**orchestrateur** (pas par la machine cible) après chaque
job, via le même type de canal que les cibles de notification propres à
PBS (gotify / webhook / sendmail).

| Clé                 | Type   | Obligatoire | Défaut  | Description                                                          |
|----------------------|--------|-------------|---------|--------------------------------------------------------------------------|
| `when`               | string | non         | `error` | `always` (succès et échec), `error` (échec seulement) ou `never`     |
| `type`               | string | non         | —       | `gotify`, `webhook` ou `sendmail`. Vide = notifications désactivées. |
| `gotify.server`      | string | si utilisé  | —       | URL de base du serveur Gotify                                        |
| `gotify.token`       | string | si utilisé  | —       | Token d'application Gotify                                            |
| `gotify.priority`    | int    | non         | `5`     | Priorité du message Gotify                                            |
| `webhook.url`        | string | si utilisé  | —       | URL du webhook, appelée avec un corps JSON (`job`, `host`, `status`, `subject`, `message`) |
| `webhook.method`     | string | non         | `POST`  | Méthode HTTP                                                          |
| `webhook.headers`    | object | non         | `{}`    | En-têtes HTTP supplémentaires (ex: un token `Authorization`)          |
| `sendmail.mailto`    | liste[string] | si utilisé | — | Adresse(s) destinataire(s), transmises au binaire `sendmail` local  |
| `sendmail.mailfrom`  | string | non         | `pbs-backup@<hostname>` | Adresse expéditeur                                    |

Remarque : la clé de config est `when`, pas `on` — en YAML 1.1, une clé nue
`on:` est interprétée comme le booléen `true` et non comme la chaîne
`"on"`, d'où ce choix volontaire.

Seule la sous-section correspondant à `type` est utilisée. Un champ
manquant/inutilisable (ex: pas de `webhook.url`) déclenche un avertissement
et ignore cette notification, sans jamais faire échouer le job de
sauvegarde lui-même. Aucune notification n'est envoyée en `--dry-run`.

## Exemple minimal (YAML)

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

## Utilisation

```bash
# Exécuter tous les jobs du répertoire de config
pbs-backup-orchestrator

# Ne traiter qu'un job précis
pbs-backup-orchestrator --job web01

# Générer les scripts sans les pousser/exécuter (vérification)
pbs-backup-orchestrator --dry-run

# Utiliser un autre répertoire de configuration
pbs-backup-orchestrator --conf-dir /chemin/vers/conf.d
```
