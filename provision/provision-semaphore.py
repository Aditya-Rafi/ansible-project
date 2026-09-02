<<<<<<< HEAD
import json
import os
import time
import urllib.request
import re
import urllib.error
import glob

"""
Semaphore Provisioning Script
=============================

This script automates the initial setup of Ansible Semaphore.
It scans the 'playbooks' directory to create Task Templates for:
1. Setup
2. Service
3. Schedule (with cron)
"""

SEMAPHORE_URL = os.getenv("SEMAPHORE_URL", "http://semaphore:3000/api")
ADMIN_USER = os.getenv("SEMAPHORE_ADMIN", "admin")
ADMIN_PASS = os.getenv("SEMAPHORE_ADMIN_PASSWORD", "admin")

# Default cron schedules for specific playbooks in the 'other' category
DEFAULT_SCHEDULES = {
    "backup-mysql.yml": "0 2 * * *",    # Daily at 2 AM
    "backup-postgres.yml": "0 3 * * *", # Daily at 3 AM
}

VIEWS = ["common", "setup", "service", "configure", "other", "build", "deploy"]

def api_call(path, method="GET", data=None, headers=None):
    if headers is None:
        headers = {"Content-Type": "application/json"}
    
    url = f"{SEMAPHORE_URL}{path}"
    req_data = json.dumps(data).encode("utf-8") if data else None
    
    req = urllib.request.Request(url, data=req_data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as res:
            if res.status == 204:
                return True
            content = res.read().decode("utf-8")
            return json.loads(content) if content else True
    except urllib.error.HTTPError as e:
        print(f"Error calling {url}: {e}")
        try:
            error_body = e.read().decode("utf-8")
            print(f"Response body: {error_body}")
        except:
            pass
        return None
    except Exception as e:
        print(f"Error calling {url}: {e}")
        return None

def wait_for_semaphore():
    print(f"Waiting for Semaphore at {SEMAPHORE_URL}...")
    for _ in range(30):
        try:
            with urllib.request.urlopen(f"{SEMAPHORE_URL}/auth/login", timeout=2):
                print("Semaphore is up!")
                return True
        except:
            time.sleep(2)
    return False

def create_task_template(project_id, name, playbook, inventory_id, repo_id, env_id, key_id, vault_key_id, view_id, headers, category=None):
    """Creates or updates a Task Template and returns its ID."""
    templates = api_call(f"/project/{project_id}/templates", headers=headers) or []
    template = next((t for t in templates if t["name"] == name), None)

    # Determine if this should run with become
    is_setup = "Setup" in name
    
    data = {
        "project_id": project_id,
        "inventory_id": inventory_id,
        "repository_id": repo_id,
        "environment_id": env_id,
        "ssh_key_id": key_id,
        "view_id": view_id,
        "app": "ansible",
        "name": name,
        "playbook": playbook,
        "arguments": "[\"--tags\", \"start\"]" if category == "service" else "[]",
        "allow_override_args_in_task": True,
        "description": f"Tags: start, stop, restart, reload" if category == "service" else f"Automated template for {playbook}",
        "task_params": {"allow_debug": True},
        "vaults": [{"project_id": project_id, "vault_key_id": vault_key_id, "type": "password"}] if vault_key_id else []
    }

    if not template:
        print(f"Creating Task Template: {name}")
        res = api_call(f"/project/{project_id}/templates", method="POST", data=data, headers=headers)
        return res["id"] if res and isinstance(res, dict) else None
    else:
        print(f"Updating Task Template: {name}")
        data["id"] = template["id"]
        if vault_key_id:
            data["vaults"][0]["template_id"] = template["id"]
        api_call(f"/project/{project_id}/templates/{template['id']}", method="PUT", data=data, headers=headers)
        return template["id"]

def create_schedule(project_id, template_id, name, cron, repo_id, headers):
    """Creates or updates a schedule for a template."""
    schedules = api_call(f"/project/{project_id}/schedules", headers=headers) or []
    schedule = next((s for s in schedules if s["template_id"] == template_id), None)
    
    data = {
        "project_id": project_id,
        "template_id": template_id,
        "name": name,
        "cron_format": cron,
        "active": True,
        "run_once": False,
        "delete_after_run": False,
        "task_params": {
            "params": {},
            "environment": "{}"
        },
        "run_at": None,
        "type": ""
    }
    
    if not schedule:
        print(f"Creating Schedule for {name}: {cron}")
        return api_call(f"/project/{project_id}/schedules", method="POST", data=data, headers=headers)
    else:
        # Check if update is needed
        existing_cron = schedule.get("cron_format")
        if existing_cron != cron or schedule.get("name") != name or not schedule.get("active"):
            print(f"Updating Schedule for {name}: {cron}")
            data["id"] = schedule["id"]
            return api_call(f"/project/{project_id}/schedules/{schedule['id']}", method="PUT", data=data, headers=headers)
        return schedule

def main():
    if not wait_for_semaphore():
        print("Semaphore did not start in time. Exiting.")
        return

    # 1. Login
    print("Logging in...")
    auth_req = urllib.request.Request(f"{SEMAPHORE_URL}/auth/login", 
                                      data=json.dumps({"auth": ADMIN_USER, "password": ADMIN_PASS}).encode("utf-8"),
                                      headers={"Content-Type": "application/json"},
                                      method="POST")
    try:
        with urllib.request.urlopen(auth_req) as res:
            cookie = res.info().get("Set-Cookie")
            if not cookie:
                print("No cookie received.")
                return
            session_cookie = cookie.split(";")[0]
            print("Successfully authenticated.")
    except Exception as e:
        print(f"Login failed: {e}")
        return

    headers = {"Content-Type": "application/json", "Cookie": session_cookie}

    # Local discovery of playbooks and inventories
    current_script = os.path.abspath(__file__)
    script_dir = os.path.dirname(current_script)
    
    # Try different possible locations for playbooks
    possible_playbook_roots = [
        os.path.abspath(os.path.join(script_dir, "..", "playbooks")),
        "/home/semaphore/ansible/playbooks",
        "/playbooks"
    ]
    
    playbook_root = None
    for root in possible_playbook_roots:
        if root and os.path.isdir(root):
            playbook_root = root
            break
            
    if not playbook_root:
        print(f"CRITICAL: Could not find playbooks directory. Looked in: {possible_playbook_roots}")
        return

    # Find inventories directory (should be sibling to playbooks)
    inventory_root = os.path.abspath(os.path.join(os.path.dirname(playbook_root), "inventories"))
    if not os.path.isdir(inventory_root):
        print(f"CRITICAL: Could not find inventories directory at {inventory_root}")
        return

    # Discover environments from inventories/ subdirectories
    discovered_envs = [d for d in os.listdir(inventory_root) if os.path.isdir(os.path.join(inventory_root, d)) and not d.startswith(".")]
    if not discovered_envs:
        print(f"CRITICAL: No environments found in {inventory_root}")
        return

    print(f"Found environments: {', '.join(discovered_envs)}")

    for env_name in discovered_envs:
        env_title = env_name.capitalize()
        project_name = f"HomeLab {env_title}"
        print(f"\n{'='*40}")
        print(f"Provisioning Project: {project_name}")
        print(f"{'='*40}")

        # 2. Project Setup
        projects = api_call("/projects", headers=headers) or []
        project = next((p for p in projects if p["name"] == project_name), None)
        if not project:
            print(f"Creating project '{project_name}'...")
            project = api_call("/projects", method="POST", data={"name": project_name}, headers=headers)
        
        if not project:
            print(f"Failed to find or create project '{project_name}'. Skipping.")
            continue
        project_id = project["id"]

        # 3. Keys Setup
        keys = api_call(f"/project/{project_id}/keys", headers=headers) or []
        
        # Repo Key (None for local)
        key_id = next((k["id"] for k in keys if k["name"] == "None"), None)
        if not key_id:
            print("Creating 'None' SSH Key...")
            res = api_call(f"/project/{project_id}/keys", method="POST", 
                            data={"project_id": project_id, "name": "None", "type": "none"}, headers=headers)
            key_id = res["id"] if res else None

        def read_vault_pass():
            # Adjust path to find .env.vault relative to scripts directory
            # homelab/ansible/scripts/provision-semaphore.py -> homelab/ansible/.env.vault
            script_dir = os.path.dirname(os.path.abspath(__file__))
            env_path = os.path.join(os.path.dirname(script_dir), ".env.vault")
            if os.path.exists(env_path):
                with open(env_path, "r") as f:
                    for line in f:
                        if line.startswith("ANSIBLE_VAULT_PASSWORD="):
                            return line.split("=", 1)[1].strip()
            return os.environ.get("ANSIBLE_VAULT_PASSWORD", "change_me")

        vault_password = read_vault_pass()

        # Vault Key
        vault_key_name = "Ansible Vault Password"
        vault_key = next((k for k in keys if k["name"] == vault_key_name), None)
        vault_key_data = {
            "project_id": project_id,
            "name": vault_key_name, 
            "type": "login_password", 
            "login_password": {"password": vault_password, "username": "vault"}
        }
        
        if not vault_key:
            print(f"Creating '{vault_key_name}' Key...")
            res = api_call(f"/project/{project_id}/keys", method="POST", data=vault_key_data, headers=headers)
            vault_key_id = res["id"] if res else None
        else:
            print(f"Updating '{vault_key_name}' Key...")
            vault_key_data["id"] = vault_key["id"]
            api_call(f"/project/{project_id}/keys/{vault_key['id']}", method="PUT", data=vault_key_data, headers=headers)
            vault_key_id = vault_key["id"]

        # 4. Repo Setup
        repos = api_call(f"/project/{project_id}/repositories", headers=headers) or []
        repo = next((r for r in repos if r["name"] == "HomeLab Ansible"), None)
        if not repo:
            print("Registering Ansible Repository...")
            repo = api_call(f"/project/{project_id}/repositories", method="POST",
                            data={"project_id": project_id, "name": "HomeLab Ansible", "git_url": "/home/semaphore/ansible", "git_branch": "master", "ssh_key_id": key_id}, headers=headers)
        repo_id = repo["id"]

        # 5. Views Setup
        print("Setting up Views...")
        existing_views = api_call(f"/project/{project_id}/views", headers=headers) or []
        view_map = {}
        for view_title in VIEWS:
            view = next((v for v in existing_views if v["title"] == view_title), None)
            if not view:
                print(f"Creating View: {view_title}")
                view = api_call(f"/project/{project_id}/views", method="POST", data={"project_id": project_id, "title": view_title, "position": VIEWS.index(view_title)}, headers=headers)
            view_map[view_title] = view["id"]

        # 6. Environments Setup
        inventories = api_call(f"/project/{project_id}/inventory", headers=headers) or []
        env_vars_list = api_call(f"/project/{project_id}/environment", headers=headers) or []

        print(f"Found playbooks at: {playbook_root}")
        
        categories = {
            "common": "Common",
            "setup": "Setup",
            "service": "Service",
            "configure": "Configure",
            "other": "Backup",
            "deploy": "Deploy",
            "build": "Build"
        }

        # Inventory
        inv_name = f"HomeLab {env_title} Inventory"
        inventory = next((i for i in inventories if i["name"] == inv_name), None)
        inv_data = {"project_id": project_id, "name": inv_name, "type": "file", "inventory": f"inventories/{env_name}/hosts.yml", "repository_id": repo_id, "ssh_key_id": key_id}
        if not inventory:
            inventory = api_call(f"/project/{project_id}/inventory", method="POST", data=inv_data, headers=headers)
        else:
            inv_data["id"] = inventory["id"]
            api_call(f"/project/{project_id}/inventory/{inventory['id']}", method="PUT", data=inv_data, headers=headers)
        inventory_id = inventory["id"]

        # Environment (Extra Vars)
        env_vars = next((e for e in env_vars_list if e["name"] == env_title), None)
        env_data = {"project_id": project_id, "name": env_title, "json": "{}", "env": "{}"}
        if not env_vars:
            env_vars = api_call(f"/project/{project_id}/environment", method="POST", data=env_data, headers=headers)
        else:
            env_data["id"] = env_vars["id"]
            api_call(f"/project/{project_id}/environment/{env_vars['id']}", method="PUT", data=env_data, headers=headers)
        env_id = env_vars["id"]

        print(f"Setting up Task Templates for {env_title}...")
        processed_template_ids = []
        
        for cat_dir, cat_label in categories.items():
            path = os.path.join(playbook_root, cat_dir)
            if not os.path.exists(path):
                # We still want to see the view even if no playbooks yet
                continue
            
            pb_files = glob.glob(os.path.join(path, "*.yml")) + glob.glob(os.path.join(path, "*.yaml"))
            print(f"  - Found {len(pb_files)} playbooks in {cat_dir}")
            
            for pb_file in pb_files:
                pb_name = os.path.basename(pb_file)
                print(f"    - Processing: {pb_name}")
                pb_title = os.path.splitext(pb_name)[0].replace("-", " ").replace("_", " ").title()
                if pb_title.lower().startswith(cat_label.lower()) or (cat_label == "Configure" and pb_title.lower().startswith("config")):
                    template_name = pb_title
                else:
                    template_name = f"{cat_label} {pb_title}"
                playbook_rel_path = f"playbooks/{cat_dir}/{pb_name}"
                
                view_id = view_map.get(cat_dir)
                
                template_id = create_task_template(
                    project_id=project_id,
                    name=template_name,
                    playbook=playbook_rel_path,
                    inventory_id=inventory_id,
                    repo_id=repo_id,
                    env_id=env_id,
                    key_id=key_id,
                    vault_key_id=vault_key_id,
                    view_id=view_id,
                    headers=headers,
                    category=cat_dir
                )
                
                if template_id:
                    processed_template_ids.append(template_id)

                # If category is other (Backup), create a schedule
                if cat_dir == "other" and template_id:
                    cron = DEFAULT_SCHEDULES.get(pb_name)
                    if cron:
                        create_schedule(
                            project_id=project_id,
                            template_id=template_id,
                            name=f"{template_name} (Cron)",
                            cron=cron,
                            repo_id=repo_id,
                            headers=headers
                        )

        # 7. Cleanup orphan templates
        print(f"Cleaning up orphan Task Templates for {project_name}...")
        all_templates = api_call(f"/project/{project_id}/templates", headers=headers) or []
        for template in all_templates:
            if template["id"] not in processed_template_ids:
                print(f"  - Deleting orphan template: {template['name']}")
                api_call(f"/project/{project_id}/templates/{template['id']}", method="DELETE", headers=headers)

    print("-" * 40)
    print("Provisioning complete for all environments!")
    print("-" * 40)
=======
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
            "Hasil di /opt/backups/mysql pada node target."
        ),
        "survey_vars": [],
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
            "json": "{}",
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

>>>>>>> 1714484 (Ansible + Teleport + Semaphore: backup/restore/verify MySQL)

if __name__ == "__main__":
    main()
