#!/usr/bin/env python3
"""
Provisioner Semaphore untuk project ansible-adit
================================================

Membuat/menyelaraskan seluruh objek dashboard yang dibutuhkan untuk
backup / restore / verify MySQL, secara IDEMPOTEN (aman dijalankan berulang):

    Project  -> Key "None"
             -> Repository "local"      (bind-mount /home/semaphore/ansible)
             -> Inventory  "cek-backup" (file: inventory/hosts.yaml)
             -> Environment "teleport-ssh"
                   { "ANSIBLE_SSH_ARGS": "-F /home/semaphore/ansible/teleport-docker.cfg" }
             -> Template "Backup MySQL"  -> playbook/backups/backup_mysql.yml
             -> Template "Verify Backup" -> playbook/backups/verify_backup.yml
             -> Template "Restore MySQL" -> playbook/backups/restore_mysql.yml
             -> Schedule harian untuk Backup (default NONAKTIF)

Environment 'teleport-ssh' WAJIB ter-attach ke setiap template: Semaphore tidak
mewariskan environment OS ke proses ansible, dan tanpa ANSIBLE_SSH_ARGS container
akan memakai teleport.cfg (path host) yang tidak ada di dalam container
-> "failed to parse identity file".

Jalankan dari host:
    SEMAPHORE_URL=http://localhost:3001/api python3 provision/provision-semaphore.py
atau lewat service 'provisioner' di docker-compose (default URL http://semaphore:3000/api).
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

SEMAPHORE_URL = os.getenv("SEMAPHORE_URL", "http://semaphore:3000/api").rstrip("/")
ADMIN_USER = os.getenv("SEMAPHORE_ADMIN", "admin")
ADMIN_PASS = os.getenv("SEMAPHORE_ADMIN_PASSWORD", "admin")

PROJECT_NAME = os.getenv("SEMAPHORE_PROJECT", "backup")
REPO_NAME = "local"
REPO_PATH = os.getenv("SEMAPHORE_REPO_PATH", "/home/semaphore/ansible")
INVENTORY_NAME = "cek-backup"
INVENTORY_FILE = "inventory/hosts.yaml"
ENV_NAME = "teleport-ssh"
SSH_CONFIG_IN_CONTAINER = f"{REPO_PATH}/teleport-docker.cfg"

# Jadwal backup harian. Dibuat NONAKTIF supaya tidak ada job kejutan;
# aktifkan lewat UI (tab Schedule) atau set SEMAPHORE_SCHEDULE_ACTIVE=true.
BACKUP_CRON = os.getenv("SEMAPHORE_BACKUP_CRON", "0 2 * * *")
SCHEDULE_ACTIVE = os.getenv("SEMAPHORE_SCHEDULE_ACTIVE", "false").lower() == "true"

TEMPLATES = [
    {
        "name": "Backup MySQL",
        "playbook": "playbook/backups/backup_mysql.yml",
        "description": (
            "mysqldump semua database di mysql_instances (client versi-aware), "
            "assert footer 'Dump completed' + jumlah CREATE TABLE, tulis .sha256. "
            "Hasil di /opt/backups/mysql/<host> pada node target. File > N hari "
            "(default 5) dihapus otomatis. Satu DB gagal tidak menghentikan DB lain."
        ),
        "survey_vars": [
            {
                "name": "backup_databases",
                "title": "Database (kosongkan = ikut inventory)",
                "description": "'all' atau daftar dipisah koma, mis. db1,db2",
                "type": "",
                "required": False,
            },
            {
                "name": "backup_retention_days",
                "title": "Retensi hari (kosongkan = 5)",
                "description": "File backup lebih tua dari N hari dihapus. 0 = jangan hapus.",
                "type": "int",
                "required": False,
            },
        ],
        "schedule": BACKUP_CRON,
    },
    {
        "name": "Verify Backup",
        "playbook": "playbook/backups/verify_backup.yml",
        "description": (
            "Restore backup terbaru ke container MySQL sementara, lalu bandingkan "
            "1:1 (struktur tabel, jumlah baris, CHECKSUM bila versi sama) dengan produksi."
        ),
        "survey_vars": [],
        "schedule": None,
    },
    {
        "name": "Restore MySQL",
        "playbook": "playbook/backups/restore_mysql.yml",
        "description": (
            "Restore backup ke container MySQL terpisah (BUKAN menimpa produksi). "
            "Kosongkan restore_backup_file untuk memakai backup terbaru. "
            "Container dihapus otomatis 3 hari kemudian."
        ),
        "survey_vars": [
            {
                "name": "restore_backup_file",
                "title": "File backup (kosongkan = terbaru)",
                "description": "Path absolut .sql di node target, mis. /opt/backups/mysql/backup-<db>-3308-<ts>.sql",
                "type": "",
                "required": False,
            }
        ],
        "schedule": None,
    },
]


# ----------------------------------------------------------------------------- API
class Api:
    def __init__(self, base_url):
        self.base = base_url
        self.cookie = None

    def call(self, path, method="GET", data=None, expect_json=True):
        url = f"{self.base}{path}"
        headers = {"Content-Type": "application/json"}
        if self.cookie:
            headers["Cookie"] = self.cookie
        body = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req) as res:
                raw = res.read().decode()
                if res.status == 204 or not raw:
                    return True
                return json.loads(raw) if expect_json else raw
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode()
            except Exception:
                pass
            print(f"    [!] {method} {path} -> HTTP {e.code} {detail.strip()}")
            return None
        except Exception as e:  # noqa: BLE001
            print(f"    [!] {method} {path} -> {e}")
            return None

    def login(self):
        url = f"{self.base}/auth/login"
        body = json.dumps({"auth": ADMIN_USER, "password": ADMIN_PASS}).encode()
        req = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req) as res:
            raw_cookie = res.info().get("Set-Cookie")
        if not raw_cookie:
            raise RuntimeError("login tidak mengembalikan cookie sesi")
        self.cookie = raw_cookie.split(";")[0]

    def wait_ready(self, timeout=60):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                urllib.request.urlopen(f"{self.base}/auth/login", timeout=2)
                return True
            except urllib.error.HTTPError:
                return True  # server hidup (405/400 untuk GET) — cukup
            except Exception:  # noqa: BLE001
                time.sleep(2)
        return False


def find_by(items, key, value):
    return next((i for i in (items or []) if i.get(key) == value), None)


def upsert(api, collection_path, item_path, match_key, payload, label):
    """POST bila belum ada, PUT bila sudah. Mengembalikan objek final."""
    existing = find_by(api.call(collection_path), match_key, payload[match_key])
    if not existing:
        created = api.call(collection_path, method="POST", data=payload)
        if isinstance(created, dict):
            print(f"  + {label} dibuat (id {created['id']})")
            return created
        print(f"  [!] gagal membuat {label}")
        return None
    payload = dict(payload, id=existing["id"])
    api.call(item_path.format(id=existing["id"]), method="PUT", data=payload)
    print(f"  = {label} diperbarui (id {existing['id']})")
    return dict(existing, **payload)


# ----------------------------------------------------------------------------- main
def main():
    api = Api(SEMAPHORE_URL)
    print(f"Menunggu Semaphore di {SEMAPHORE_URL} ...")
    if not api.wait_ready():
        sys.exit("Semaphore tidak merespons.")
    api.login()
    print("Login berhasil.\n")

    # 1. Project ---------------------------------------------------------------
    project = find_by(api.call("/projects"), "name", PROJECT_NAME)
    if not project:
        project = api.call("/projects", method="POST", data={"name": PROJECT_NAME})
        print(f"  + Project '{PROJECT_NAME}' dibuat")
    if not project:
        sys.exit(f"Tidak bisa membuat project '{PROJECT_NAME}'.")
    pid = project["id"]
    print(f"Project '{PROJECT_NAME}' (id {pid})")

    # 2. Key "None" — auth ke target lewat sertifikat Teleport, bukan key Semaphore
    key = find_by(api.call(f"/project/{pid}/keys"), "name", "None")
    if not key:
        key = api.call(
            f"/project/{pid}/keys",
            method="POST",
            data={"project_id": pid, "name": "None", "type": "none"},
        )
        print("  + Key 'None' dibuat")
    key_id = key["id"]

    # 3. Repository — working tree bind-mount, branch dikosongkan
    repo = upsert(
        api,
        f"/project/{pid}/repositories",
        f"/project/{pid}/repositories/{{id}}",
        "name",
        {
            "project_id": pid,
            "name": REPO_NAME,
            "git_url": REPO_PATH,
            "git_branch": "",
            "ssh_key_id": key_id,
        },
        f"Repository '{REPO_NAME}' -> {REPO_PATH}",
    )
    repo_id = repo["id"]

    # 4. Inventory
    inv = upsert(
        api,
        f"/project/{pid}/inventory",
        f"/project/{pid}/inventory/{{id}}",
        "name",
        {
            "project_id": pid,
            "name": INVENTORY_NAME,
            "type": "file",
            "inventory": INVENTORY_FILE,
            "repository_id": repo_id,
            "ssh_key_id": key_id,
            "become_key_id": key_id,
        },
        f"Inventory '{INVENTORY_NAME}' -> {INVENTORY_FILE}",
    )
    inv_id = inv["id"]

    # 5. Environment — menyuntik ssh_config Teleport versi container
    env = upsert(
        api,
        f"/project/{pid}/environment",
        f"/project/{pid}/environment/{{id}}",
        "name",
        {
            "project_id": pid,
            "name": ENV_NAME,
            # backup_bot_url hanya diisi bila bot Telegram dikonfigurasi
            "json": json.dumps(
                {"backup_bot_url": "http://backup-bot:8080"}
                if os.getenv("TELEGRAM_BOT_TOKEN")
                else {}
            ),
            "env": json.dumps({"ANSIBLE_SSH_ARGS": f"-F {SSH_CONFIG_IN_CONTAINER}"}),
        },
        f"Environment '{ENV_NAME}'",
    )
    env_id = env["id"]

    # 6. Templates
    print("\nTask Templates:")
    for spec in TEMPLATES:
        payload = {
            "project_id": pid,
            "name": spec["name"],
            "playbook": spec["playbook"],
            "inventory_id": inv_id,
            "repository_id": repo_id,
            # environment_id DAN environment_ids: bila hanya salah satu, Semaphore
            # mereset attachment-nya ke 0 dan Environment tidak terpakai.
            "environment_id": env_id,
            "environment_ids": [env_id],
            "app": "ansible",
            "type": "",
            "description": spec["description"],
            "arguments": "[]",
            "allow_override_args_in_task": True,
            "survey_vars": spec["survey_vars"],
            "task_params": {"allow_debug": True},
            "vaults": [],
        }
        tpl = upsert(
            api,
            f"/project/{pid}/templates",
            f"/project/{pid}/templates/{{id}}",
            "name",
            payload,
            f"Template '{spec['name']}'",
        )
        if not tpl or not spec.get("schedule"):
            continue

        # 7. Schedule (opsional, per template)
        sched = find_by(api.call(f"/project/{pid}/schedules"), "template_id", tpl["id"])
        sched_payload = {
            "project_id": pid,
            "template_id": tpl["id"],
            "name": f"{spec['name']} (harian)",
            "cron_format": spec["schedule"],
            "active": SCHEDULE_ACTIVE,
            "run_once": False,
        }
        if not sched:
            api.call(f"/project/{pid}/schedules", method="POST", data=sched_payload)
            state = "aktif" if SCHEDULE_ACTIVE else "NONAKTIF"
            print(f"    + Schedule '{spec['schedule']}' dibuat ({state})")
        else:
            sched_payload["id"] = sched["id"]
            # jangan paksa nonaktifkan jadwal yang sudah diaktifkan operator
            sched_payload["active"] = sched.get("active", SCHEDULE_ACTIVE)
            api.call(
                f"/project/{pid}/schedules/{sched['id']}", method="PUT", data=sched_payload
            )
            print(f"    = Schedule diperbarui (id {sched['id']})")

    print("\nSelesai. Buka dashboard, jalankan template dari tab Task Templates.")


if __name__ == "__main__":
    main()
