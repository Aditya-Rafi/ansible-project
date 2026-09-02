# Panduan Setup Lengkap — Teleport + Ansible + Semaphore + Backup MySQL Multi-Versi

> **Dokumen base-development.** Panduan ini menjelaskan skema dari **nol sampai
> selesai**: membangun cluster Teleport mandiri, Machine ID (tbot) untuk SSH
> tanpa password, menjalankan Ansible lewat Teleport, mengintegrasikan Semaphore
> (di dalam Docker), dan menjalankan backup/restore/verify MySQL untuk server
> multi-versi (5.1 / 5.7 / 8.3). Semua nilai memakai contoh deployment nyata di
> host `192.0.2.10` (hostname `master`, Ubuntu 22.04). Ganti IP/nama sesuai
> lingkungan Anda.

---

## Daftar Isi

1. [Konsep & Arsitektur](#1-konsep--arsitektur)
2. [Prasyarat](#2-prasyarat)
3. [Bagian 0 — Fondasi dari Nol: Docker, Ansible & Project](#3-bagian-0--fondasi-dari-nol-docker-ansible--project)
4. [Bagian A — Cluster Teleport](#4-bagian-a--cluster-teleport)
5. [Bagian B — Machine ID (tbot) untuk SSH](#5-bagian-b--machine-id-tbot-untuk-ssh)
6. [Bagian C — Ansible via Teleport (host)](#6-bagian-c--ansible-via-teleport-host)
7. [Bagian D — Semaphore (Docker) + Sidecar tbot](#7-bagian-d--semaphore-docker--sidecar-tbot)
8. [Bagian E — Backup/Restore/Verify MySQL Multi-Versi](#8-bagian-e--backuprestoreverify-mysql-multi-versi)
9. [Bagian F — Operasional & Troubleshooting](#9-bagian-f--operasional--troubleshooting)
10. [Lampiran — Layout File & Cheat-Sheet](#10-lampiran--layout-file--cheat-sheet)

---

## 1. Konsep & Arsitektur

### Apa yang dibangun

Ansible biasanya SSH ke target lewat port 22 dengan password/kunci statis. Skema
ini menggantinya: **Ansible terhubung ke host target melalui Teleport** memakai
**sertifikat berumur pendek** yang diperbarui otomatis (Machine ID). Tidak ada
password SSH yang disimpan, setiap sesi tercatat di audit log Teleport, dan
Semaphore (UI penjadwal Ansible) bisa berjalan non-interaktif memakai skema yang
sama.

```
                 ┌───────────────────────────────────────────────────────┐
                 │  HOST 192.0.2.10 (hostname: master, Ubuntu 22.04)   │
                 │                                                         │
  Operator ──┐   │  ┌─────────────┐   ┌──────────────────────────────┐    │
  (tsh/web)  └──►│  │  Teleport   │   │  tbot-ssh.service (systemd)  │    │
                 │  │  auth+proxy │◄──┤  bot: ansible-bot            │    │
                 │  │  +ssh       │   │  → /opt/teleport/.../machine-id   │
                 │  │ master-     │   └──────────────────────────────┘    │
                 │  │ cluster     │            ▲ sertifikat (renew 20m)    │
                 │  │ :3080 :3025 │            │                          │
                 │  └──────▲──────┘   ┌────────┴─────────┐                │
                 │         │          │ Ansible (host)   │  ansible -F    │
                 │         │          │ /root/opt/ansible│  teleport-host │
                 │         │          └──────────────────┘                │
                 │         │                                              │
                 │  ┌──────┴───────────────── Docker ───────────────────┐│
                 │  │ semaphore-tbot (sidecar, uid 1001)                 ││
                 │  │   → volume teleport_id:/teleport-id/machine-id     ││
                 │  │ semaphore (UI+runner) ── ansible -F teleport.cfg ──┼┼──┐
                 │  │ semaphore-db (postgres)                            ││  │
                 │  └────────────────────────────────────────────────────┘│  │
                 └─────────────────────────────────────────────────────────┘  │
                                                                               │ via Teleport SSH (root)
                                                                               ▼
                                                              host target = master (127.0.0.1)
                                                                               │
                                          docker run mysql:5.7/8.0 mysqldump   │ TCP :3307/3308/3309
                                                                               ▼
                                                              MySQL produksi 192.0.2.30
                                                              (5.1 / 5.7 / 8.3, multi-instance)
```

### Komponen kunci

| Komponen | Peran |
|----------|-------|
| **Teleport** (auth+proxy+ssh) | Cluster `master-cluster`, satu host, identitas & audit |
| **tbot (Machine ID)** | Bot `ansible-bot` menghasilkan sertifikat SSH pendek, auto-renew |
| **Ansible** | Konek ke node via `ProxyCommand tbot` (cert auth, tanpa password) |
| **Semaphore** | UI penjadwal Ansible di Docker; pakai **sidecar tbot** untuk cert |
| **Role MySQL** | Backup/restore/verify **version-aware** (client mysqldump cocok versi server) |

### Dua "alur" Ansible (penting)

Konfigurasi SSH (`ssh_config`/`teleport.cfg`) **berbeda path** tergantung di mana
Ansible dijalankan, karena path sertifikat berbeda:

| Konteks | File config | Path sertifikat |
|---------|-------------|-----------------|
| Ansible **di host** (CLI) — default `ansible.cfg` | `teleport.cfg` | `/opt/teleport/teleport-ssh/machine-id/` |
| Ansible **di container Semaphore** | `teleport-docker.cfg` (disuntik via Environment Semaphore `ANSIBLE_SSH_ARGS`) | `/teleport-id/machine-id/` (volume) |

> **Penamaan ini dipakai konsisten di seluruh dokumen** (sesuai deployment nyata
> `.128`): `teleport.cfg` = host (default `ansible.cfg`), `teleport-docker.cfg` =
> container (disuntik via Environment Semaphore).
>
> Inilah sumber error paling umum: memakai config host saat jalan di container
> (atau sebaliknya) → `failed to parse identity file`. Lihat [Bagian F](#9-bagian-f--operasional--troubleshooting).

---

## 2. Prasyarat

- Ubuntu 22.04 (root access; di contoh ini `root` / password server).
- Paket: `curl`, `docker` + `docker compose` v2, akses internet untuk pull image
  `mysql:5.7` & `mysql:8.0`.
- Jaringan: host target & MySQL `.192` saling terjangkau; port Teleport `3080`
  (web/proxy) dan `3025` (auth) terbuka di host cluster.
- NTP **aktif & sinkron** — sertifikat Teleport sangat sensitif terhadap waktu
  (lihat [drift clock](#drift-clock--sertifikat-expired)).

```bash
timedatectl set-ntp true
timedatectl   # pastikan "System clock synchronized: yes"
```

---

## 3. Bagian 0 — Fondasi dari Nol: Docker, Ansible & Project

> Bagian A–F mengasumsikan host sudah punya **Docker**, **Ansible**, dan **file
> project** lengkap. Bagian 0 ini membangun semua fondasi itu dari OS Ubuntu 22.04
> yang masih bersih, sampai siap masuk ke Bagian A. Kerjakan **berurutan**:
> `0.1 → 0.2 → 0.3 → 0.4 → 0.5`, lalu lanjut ke Bagian A.

### 0.1 Update sistem & paket dasar

```bash
# login sebagai root (atau pakai sudo)
apt-get update && apt-get -y upgrade

# tools dasar yang dipakai langkah-langkah berikutnya
apt-get install -y curl ca-certificates gnupg git rsync jq python2 python3-pip

# NTP wajib aktif (sertifikat Teleport sangat sensitif waktu — lihat Prasyarat)
timedatectl set-ntp true
timedatectl     # pastikan "System clock synchronized: yes"
```

### 0.2 Install Docker Engine + Compose v2

Pakai repo resmi Docker (bukan paket `docker.io` Ubuntu yang sering lawas dan
**tanpa** `docker compose` v2):

```bash
# 1) kunci GPG resmi Docker
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
chmod a+r /etc/apt/keyrings/docker.gpg

# 2) tambahkan repo
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
  > /etc/apt/sources.list.d/docker.list

# 3) install engine + plugin compose v2 + buildx
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# 4) aktifkan & aktif-saat-boot
systemctl enable --now docker
```

Verifikasi (semua harus jalan tanpa error):

```bash
docker version                 # Client + Server muncul
docker compose version         # Docker Compose version v2.x  (perhatikan: "compose", bukan "docker-compose")
docker run --rm hello-world    # → "Hello from Docker!"
```

> **Catatan user non-root:** bila ingin menjalankan `docker` tanpa `sudo` sebagai
> user biasa: `usermod -aG docker <user>` lalu **logout/login**. Di demo `.128`
> semua dijalankan sebagai `root`, jadi langkah ini opsional.

> **Image yang akan dipakai** (di-pull otomatis nanti, pastikan akses internet):
> `semaphoreui/semaphore:latest`, `postgres:14`, `alpine:3.21`, `python:3-alpine`
> (Semaphore stack) serta `mysql:5.7` & `mysql:8.0` (client backup version-aware,
> lihat [Bagian E](#8-bagian-e--backuprestoreverify-mysql-multi-versi)).

### 0.3 Install Ansible + dependensi

Pakai paket apt Ubuntu 22.04 agar **versi cocok dengan dokumen ini** (ansible-core
**2.10.8** di host — penting untuk isu `split` di [Bagian E.3](#8-bagian-e--backuprestoreverify-mysql-multi-versi)):

```bash
apt-get install -y ansible
ansible --version        # → ansible 2.10.8 ... python 3.10

# collection yang dipakai role (community.docker untuk MySQL-in-Docker)
ansible-galaxy collection install community.docker
ansible-galaxy collection list community.docker   # konfirmasi terpasang
```

Client MySQL di **host yang menjalankan dump** (host target) dibutuhkan role untuk
discovery & deteksi versi (`SELECT VERSION()`):

```bash
apt-get install -y mysql-client-core-8.0
```

> Dump sebenarnya tetap memakai **image Docker** sesuai versi server (lihat
> Bagian E), jadi host target juga wajib punya Docker aktif (sudah dari langkah 0.2).

### 0.4 Siapkan file project

Letakkan project di `/root/opt/ansible` (lokasi yang dipakai dokumen ini):

```bash
mkdir -p /root/opt
cd /root/opt
git clone <URL-REPO-ANDA> ansible      # atau salin/rsync folder project ke sini
cd /root/opt/ansible
```

Buat file rahasia & config lokal dari template `.example` (file aslinya
**di-`.gitignore`**, jadi tidak ikut clone — wajib dibuat manual):

```bash
# 1) Kredensial Semaphore + Postgres
cp .env.semaphore.example .env.semaphore
#   edit: SEMAPHORE_ADMIN_PASSWORD, SEMAPHORE_DB_PASS, POSTGRES_PASSWORD,
#         dan tambahkan ANSIBLE_VAULT_PASSWORD=<password-vault-anda>

# 2) Secret MySQL untuk inventory (vault)
cp inventory/group_vars/all/vault.yml.example inventory/group_vars/all/vault.yml
#   edit: vault_mysql_password: "<password-root-MySQL-target>"
#   (produksi: ansible-vault encrypt inventory/group_vars/all/vault.yml)

# 3) Config sidecar tbot Semaphore (token diisi nanti di Bagian D.4)
cp teleport/tbot-semaphore.yaml.example teleport/tbot-semaphore.yaml
```

Struktur minimum yang harus ada setelah langkah ini:

```
/root/opt/ansible/
  ansible.cfg                 # default -F ./teleport.cfg (HOST) + vault_password_file; container override via Environment Semaphore
  docker-compose.yml          # stack Semaphore + sidecar tbot + postgres + provisioner
  .env.semaphore              # ← dibuat dari .example (gitignored)
  inventory/hosts.yaml
  inventory/group_vars/all/vault.yml   # ← dibuat dari .example (gitignored)
  teleport/tbot-semaphore.yaml         # ← dibuat dari .example (gitignored)
  roles/  playbook/  provision/
```

> File `teleport.cfg` (path host) & `teleport-docker.cfg` (path container) baru
> final-benar setelah tbot menghasilkan sertifikat (Bagian B). Isinya disalin dari
> output `machine-id/ssh_config` — lihat [Bagian C.1](#6-bagian-c--ansible-via-teleport-host).

### 0.5 Peta urutan eksekusi (roadmap)

Setelah fondasi siap, kerjakan sisa dokumen **berurutan**:

| Langkah | Bagian | Hasil |
|---------|--------|-------|
| 1 | [A — Cluster Teleport](#4-bagian-a--cluster-teleport) | Teleport auth+proxy+ssh hidup, role `ansible_ssh` ada |
| 2 | [B — Machine ID (tbot)](#5-bagian-b--machine-id-tbot-untuk-ssh) | Sertifikat SSH auto-renew di `machine-id/` |
| 3 | [C — Ansible via Teleport](#6-bagian-c--ansible-via-teleport-host) | `ansible all -m ping` sukses dari host |
| 4 | [D — Semaphore (Docker)](#7-bagian-d--semaphore-docker--sidecar-tbot) | UI Semaphore + sidecar tbot jalan, ping sukses dari container |
| 5 | [E — Backup MySQL](#8-bagian-e--backuprestoreverify-mysql-multi-versi) | Backup/restore/verify multi-versi `failed=0` |

Bila ada error di tahap mana pun, langsung ke
[Bagian F — Troubleshooting](#9-bagian-f--operasional--troubleshooting) yang
mengkatalogkan error nyata beserta solusinya.

---

## 4. Bagian A — Cluster Teleport

### A.1 Instalasi binary

Teleport v18 memakai layout *managed-update* (`/opt/teleport/system/bin/`) dengan
symlink ke `/usr/local/bin/`:

```bash
curl -fsSL https://goteleport.com/static/install.sh | bash -s 18.8.3
teleport version    # Teleport v18.8.3
```

### A.2 Konfigurasi cluster (`/etc/teleport.yaml`)

Satu host menjalankan **auth + proxy + ssh** sekaligus:

```yaml
version: v3
teleport:
  nodename: master
  data_dir: /var/lib/teleport
  log:
    output: stderr
    severity: INFO
auth_service:
  enabled: "yes"
  proxy_listener_mode: multiplex      # semua lewat satu port (TLS routing)
  cluster_name: master-cluster
  listen_addr: 0.0.0.0:3025
proxy_service:
  enabled: "yes"
  web_listen_addr: 0.0.0.0:3080
  public_addr: 192.0.2.10:3080     # alamat yang diiklankan ke client
ssh_service:
  enabled: "yes"
```

> **Catatan TLS:** proxy memakai sertifikat **self-signed**, jadi semua `tbot`
> dijalankan dengan flag `--insecure`. Untuk produksi penuh, pasang cert valid di
> proxy lalu hapus `--insecure`.

### A.3 Service systemd (`/etc/systemd/system/teleport.service`)

```ini
[Unit]
Description=Teleport Service
After=network.target

[Service]
Type=simple
Restart=always
RestartSec=5
EnvironmentFile=-/etc/default/teleport
ExecStart=/usr/local/bin/teleport start --config /etc/teleport.yaml --pid-file=/run/teleport.pid
ExecReload=/bin/sh -c "exec pkill -HUP -L -F /run/teleport.pid"
PIDFile=/run/teleport.pid
LimitNOFILE=524288

[Install]
WantedBy=multi-user.target
```

```bash
systemctl enable --now teleport
tctl status                 # cek cluster sehat (butuh sudo; baca /etc/teleport.yaml)
```

### A.4 Buat role akses (`ansible_ssh`)

Role ini mengizinkan login OS `root` & `developer` ke semua node:

```yaml
# role-ansible-ssh.yaml
kind: role
version: v7
metadata:
  name: ansible_ssh
  labels:
    '*': '*'
spec:
  allow:
    logins: [root, developer]
    node_labels:
      '*': '*'
  options:
    max_session_ttl: 12h0m0s
    enhanced_recording: [command, network]
    ssh_file_copy: true
```

```bash
tctl create -f role-ansible-ssh.yaml
```

> **Gotcha:** login yang dipakai Ansible **harus** ada di daftar `logins`. Jika
> nanti menambah login (mis. `root`), bot perlu di-restart agar principal baru
> muncul di sertifikat.

---

## 5. Bagian B — Machine ID (tbot) untuk SSH

`tbot` adalah agen yang join ke cluster sebagai **bot**, lalu terus-menerus
menulis sertifikat SSH pendek ke direktori output. Ansible memakai sertifikat itu.

### B.1 Buat bot

```bash
tctl bots add ansible-bot --roles ansible_ssh
# Output berisi TOKEN join sekali-pakai (berlaku ~60 menit). Salin tokennya.
```

### B.2 Konfigurasi tbot host (`/opt/teleport/teleport-ssh/tbot-ssh.yml`)

```yaml
version: v2
proxy_server: 192.0.2.10:3080
onboarding:
  join_method: token
  token: "<TOKEN_DARI_LANGKAH_B1>"
storage:
  type: directory
  path: /opt/teleport/teleport-ssh/bot-storage
services:
  - type: identity
    destination:
      type: directory
      path: /opt/teleport/teleport-ssh/machine-id   # ← output sertifikat
```

### B.3 Service systemd (`/etc/systemd/system/tbot-ssh.service`)

```ini
[Unit]
Description=Teleport Machine ID (tbot) - SSH identity for Ansible
After=network.target teleport.service

[Service]
Type=simple
ExecStart=/usr/local/bin/tbot start --config /opt/teleport/teleport-ssh/tbot-ssh.yml --insecure
Restart=on-failure
RestartSec=5
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
```

```bash
systemctl enable --now tbot-ssh
journalctl -u tbot-ssh -f      # tunggu "Identity initialized successfully"
ls /opt/teleport/teleport-ssh/machine-id/   # → key, key-cert.pub, known_hosts, ssh_config, ...
```

Output penting di `machine-id/`:

| File | Fungsi |
|------|--------|
| `key`, `key-cert.pub` | kunci privat + sertifikat SSH (renew tiap ~20m) |
| `known_hosts` | host CA untuk verifikasi node |
| `ssh_config` | config OpenSSH siap-pakai (template `teleport.cfg`) |

> **Identitas bot TTL 1 jam, renew tiap 20 menit.** systemd menjaga tbot hidup
> setelah reboot.

---

## 6. Bagian C — Ansible via Teleport (host)

### C.1 `teleport.cfg` (ssh_config untuk Ansible di host)

Salin dari output tbot (`machine-id/ssh_config`). **Penting: `ProxyCommand` harus
satu baris penuh** — OpenSSH tidak mengenal `\` sebagai penyambung baris di
`ssh_config`, dan baris lanjutan dibaca sebagai opsi terpisah
(`Bad configuration option: --destination-dir`). Isinya:

```sshconfig
# Common flags untuk semua host master-cluster
Host *.master-cluster 192.0.2.10
    UserKnownHostsFile "/opt/teleport/teleport-ssh/machine-id/known_hosts"
    IdentityFile "/opt/teleport/teleport-ssh/machine-id/key"
    CertificateFile "/opt/teleport/teleport-ssh/machine-id/key-cert.pub"

# Semua host master-cluster kecuali proxy → lewat ProxyCommand tbot (SATU BARIS)
Host *.master-cluster !192.0.2.10
    Port 3022
    ProxyCommand /usr/local/bin/tbot ssh-proxy-command --destination-dir=/opt/teleport/teleport-ssh/machine-id --proxy-server=192.0.2.10:3080 --cluster=master-cluster --tls-routing --no-connection-upgrade --resume --insecure --user=%r --host=%h --port=%p
```

> `--insecure` dipakai karena proxy memakai cert self-signed (lihat A.2). Bisa
> dihapus bila proxy sudah memakai cert valid.

### C.2 `ansible.cfg`

```ini
[defaults]
roles_path = ./roles
inventory = ./inventory/hosts.yaml
deprecation_warnings = False
interpreter_python = /usr/bin/python3
vault_password_file = ./.vault_pass_file   # path relatif — valid di host & container

[ssh_connection]
ssh_args = -F ./teleport.cfg               # default = config HOST; container override via ANSIBLE_SSH_ARGS
transfer_method = piped                    # WAJIB untuk Teleport — JANGAN scp_if_ssh
pipelining = true                          # kurangi jumlah operasi SSH per task
```

> **Jangan pakai `scp_if_ssh = True`.** `scp` modern (OpenSSH 8.9 Ubuntu 22.04)
> salah mem-parse host saat lewat ProxyCommand Teleport → error
> `hostname contains invalid characters`. `transfer_method = piped` mengalirkan
> modul lewat stdin SSH (tanpa scp/sftp terpisah) dan paling andal untuk Teleport.

### C.3 Inventory (`inventory/hosts.yaml`)

```yaml
all:
  vars:
    ansible_user: root
  children:
    db_server:
      hosts:
        dev_db:
          ansible_host: master.master-cluster   # <node>.<cluster> Teleport
          ansible_connection: ssh
          database_server: mysql
          enable_backup: true
          mysql_host: 192.0.2.30
          mysql_username: "root"
          mysql_password: "{{ vault_mysql_password }}"
          mysql_instances:
            - port: 3308
              databases: "demo_erpaccdb"   # atau "all" untuk auto-discovery semua DB non-system
```

> **Jangan** menaruh `ansible_ssh_common_args` atau `ansible_scp_if_ssh` di
> inventory (atau di `-e`). SSH args sudah di-set di `ansible.cfg` (host) /
> Environment Semaphore (container); menumpuknya menimpa config dan memicu jalur
> kode SSH lama yang bermasalah (`hostname contains invalid characters`).

> **Quoting Jinja2:** value yang diawali `{{` **wajib** dikutip
> (`mysql_password: "{{ vault_mysql_password }}"`), kalau tidak YAML membacanya
> sebagai flow-mapping → `This may be an issue with missing quotes around a
> template block`. Pastikan nama variabel sama persis dengan di `vault.yml`
> (waspadai typo `passpword`/`password`).

**Aturan penamaan:** `ansible_host` = `<nama-node>.<nama-cluster>`. Nama node =
`nodename` di `/etc/teleport.yaml` (`master`), bukan nama login. Cek dengan
`tctl nodes ls`.

### C.4 Secret (`inventory/group_vars/all/vault.yml`) + vault password file

Isi vault (di-enkripsi `ansible-vault` di produksi):
```yaml
vault_mysql_password: "<password-root-MySQL-.192>"
```

Vault password disimpan di file yang dirujuk `ansible.cfg`
(`vault_password_file = ./.vault_pass_file`):
```bash
printf 'PASSWORD_VAULT' > .vault_pass_file   # printf, BUKAN echo (tanpa newline)
chmod 644 .vault_pass_file                   # readable container (lihat Bagian D)
echo '.vault_pass_file' >> .gitignore
```

> **Gotcha vault password file (terbukti memakan waktu):**
> - Tulis dengan `printf` (tanpa `\n`), **bukan** `echo` — trailing newline bisa
>   bikin password tidak cocok. Verifikasi byte: `cat -A .vault_pass_file` atau
>   `xxd .vault_pass_file` (cari newline/spasi/BOM tersembunyi).
> - Pakai **path relatif** `./.vault_pass_file`, bukan `~` — ekspansi tilde tidak
>   selalu jalan dari `ansible.cfg`. Path relatif valid di host **dan** container.
> - Beda error: *"Attempting to decrypt but no vault secrets found"* = password
>   belum disediakan; *"Decryption failed"* = password ada tapi **salah**.
>   Sinkronkan dengan `ansible-vault rekey vault.yml --new-vault-password-file
>   .vault_pass_file`.
> - **Permission**: container Semaphore harus bisa membaca `vault.yml` &
>   `.vault_pass_file`. Jika `Permission denied`: jalankan container sebagai root
>   (D.1) atau `chmod -R a+rX /root/opt/ansible`.

### C.5 Uji koneksi (host)

```bash
# 1. Pastikan folder induk /opt/teleport/ ada (jika belum)
sudo mkdir -p /opt/teleport

# 2. Buat jalan pintas (symlink) dari lokasi asli ke jalur yang dicari Ansible
sudo ln -s /home/developer/teleport-ssh /opt/teleport/teleport-ssh

# 3. Verifikasi apakah jalur tersebut sudah terhubung ke berkas sertifikat
ls -l /opt/teleport/teleport-ssh/machine-id/

cd /root/opt/ansible
ansible all -m ping
# → dev_db | SUCCESS => {"ping": "pong"}
```

> Cukup `ansible all -m ping` — `teleport.cfg` & vault sudah di-set di `ansible.cfg`.
> **Jangan** tambahkan `-e "ansible_ssh_common_args=-F ..."`; itu menimpa config dan
> memicu jalur SSH lama (`hostname contains invalid characters`).
>
> Isolasi/debug SSH manual: `ssh -F teleport.cfg -v root@master.master-cluster`
> → baris sukses: `Authenticated to master.master-cluster (via proxy) using "publickey"`.
> Jika muncul `target host ... is offline or does not exist`, node belum terdaftar
> di cluster — cek `tctl nodes ls` (kolom Host = prefix `ansible_host`).

Setiap sesi muncul di Teleport **Audit Log / Session Recordings**.

---

## 7. Bagian D — Semaphore (Docker) + Sidecar tbot

Semaphore menjalankan Ansible **di dalam container** (`semaphoreui/semaphore`,
Alpine). Container itu tidak punya akses ke `machine-id/` host, jadi kita jalankan
**sidecar tbot** yang menulis sertifikat ke **named volume bersama**
(`teleport_id`) yang dibaca container Semaphore.

> **Kunci yang menyelesaikan semua masalah permission: jalankan KEDUA container
> (semaphore + tbot) sebagai root (`user: "0:0"`).** Root selalu ada di
> `/etc/passwd` Alpine (tidak ada `unknown userid 1001`), dan root-menulis /
> root-membaca menghilangkan seluruh konflik UID antar container yang berbagi
> volume `teleport_id`. `user: "1001:0"` justru gagal karena UID 1001 tidak
> terdaftar di passwd Alpine saat tbot menulis output identity.

### D.1 `docker-compose.yml`

```yaml
services:
  semaphore:
    image: semaphoreui/semaphore:latest
    container_name: semaphore
    user: "0:0"                                                # root → bisa baca cert tbot (root)
    ports: ["3001:3000"]
    env_file: [.env]
    extra_hosts:
      - "host.docker.internal:host-gateway"
    depends_on: [db]
    restart: unless-stopped
    volumes:
      - semaphore_data:/var/lib/semaphore
      - .:/home/semaphore/ansible                              # project (incl. teleport-docker.cfg, .vault_pass_file)
      - /opt/teleport/system/bin/tbot:/usr/local/bin/tbot:ro   # binary tbot (static, jalan di Alpine)
      - teleport_id:/teleport-id:ro                            # sertifikat dari sidecar (read-only)

  tbot:                                                         # SIDECAR Machine ID
    image: alpine:3.21
    container_name: semaphore-tbot
    user: "0:0"                                                # root → hindari "unknown userid 1001"
    restart: unless-stopped
    volumes:
      - /opt/teleport/system/bin/tbot:/usr/local/bin/tbot:ro
      - teleport_id:/teleport-id                               # rw — tbot menulis ke sini
      - ./teleport/tbot-semaphore.yaml:/etc/tbot.yaml:ro
    command: ["/usr/local/bin/tbot", "start", "-c", "/etc/tbot.yaml", "--insecure"]

  db:
    image: postgres:14
    container_name: semaphore-db
    env_file: [.env]
    restart: unless-stopped
    volumes: [semaphore_db:/var/lib/postgresql/data]

volumes:
  semaphore_data:
  semaphore_db:
  teleport_id:
```

### D.2 Config sidecar (`teleport/tbot-semaphore.yaml`) — **path container**

```yaml
version: v2
proxy_server: 192.0.2.10:3080
onboarding:
  join_method: token
  token: "<TOKEN_INSTANCE_BARU>"        # lihat D.4
storage:
  type: directory
  path: /teleport-id/bot-storage        # ← di dalam volume
services:
  - type: identity
    destination:
      type: directory
      path: /teleport-id/machine-id     # ← dibaca container Semaphore
```

### D.3 `teleport-docker.cfg` — **path container** (`/teleport-id/...`)

`ProxyCommand` **satu baris** (sama seperti C.1):

```sshconfig
Host *.master-cluster 192.0.2.10
    UserKnownHostsFile "/teleport-id/machine-id/known_hosts"
    IdentityFile "/teleport-id/machine-id/key"
    CertificateFile "/teleport-id/machine-id/key-cert.pub"

Host *.master-cluster !192.0.2.10
    Port 3022
    ProxyCommand /usr/local/bin/tbot ssh-proxy-command --destination-dir=/teleport-id/machine-id --proxy-server=192.0.2.10:3080 --cluster=master-cluster --tls-routing --no-connection-upgrade --resume --insecure --user=%r --host=%h --port=%p
```

### D.4 Tiga hal yang WAJIB benar saat menjalankan sidecar

Karena kedua container jalan sebagai **root** (D.1), gotcha permission uid-1001
tidak berlaku. Yang tersisa adalah tiga hal berikut:

```bash
cd /root/opt/ansible

# 1) proxy_server di config WAJIB IP proxy yang benar (192.0.2.10) — BUKAN .168/.192
#    salah IP → join gagal dgn pesan menyesatkan "token expired or not found"
grep proxy_server teleport/tbot-semaphore.yaml   # harus 192.0.2.10:3080

# 2) Token instance baru (sekali-pakai, ~60 menit) — pakai bot yang sudah ada
tctl bots instances add ansible-bot              # → salin "The bot token: <TOKEN>"
sed -i 's/token: ".*"/token: "<TOKEN>"/' teleport/tbot-semaphore.yaml

# 3) Volume bersih milik root sebelum start (terutama bila sebelumnya pernah uid lain)
docker run --rm -v ansible_teleport_id:/d alpine \
  sh -c "chown -R 0:0 /d && rm -rf /d/bot-storage/* /d/machine-id/* /d/.write-test"
```

> **Gotcha penting yang terbukti memakan waktu:**
> - **`docker compose restart` TIDAK menerapkan perubahan `user:` / `volumes:`.**
>   Harus `docker compose up -d --force-recreate <svc>` (atau `stop` + `rm -f` + `up`).
> - **Hapus `bot-storage` = wajib token fresh.** Setiap `bot-storage` dikosongkan,
>   tbot harus join ulang dari token. Setelah sukses join sekali, identity tersimpan.
> - **Lihat log TERBARU saja** saat debug: `docker logs --since 1m -f semaphore-tbot`
>   (`docker logs` polos menampilkan riwayat lama yang menyesatkan).
> - Nama volume = `<nama-folder-project>_teleport_id`. Folder `/root/opt/ansible`
>   → volume **`ansible_teleport_id`**. Cek:
>   `docker inspect -f '{{range .Mounts}}{{.Name}} {{end}}' semaphore`.

### D.5 Jalankan & uji

```bash
docker compose up -d --force-recreate tbot semaphore   # PROPER recreate (bukan restart)

sleep 8
docker logs --tail 12 semaphore-tbot                   # → "Identity initialized successfully" + heartbeat
docker exec semaphore id                               # uid=0(root)
docker exec semaphore ls -la /teleport-id/machine-id/  # cert terbaca (key, key-cert.pub, ssh_config, ...)

# uji persis seperti task Semaphore (pakai config CONTAINER):
docker exec semaphore sh -c "cd /home/semaphore/ansible && ANSIBLE_SSH_ARGS='-F ./teleport-docker.cfg' ansible all -m ping"
# → dev_db | SUCCESS => {"ping": "pong"}
```

> Objek Semaphore (Repository/Inventory/Environment/Template) dibuat **manual** di
> UI — provisioner otomatis tidak terpakai (lihat [Bagian F](#9-bagian-f--operasional--troubleshooting)).
> Yang **wajib**: Environment `teleport-ssh` berisi
> `{ "ANSIBLE_SSH_ARGS": "-F /home/semaphore/ansible/teleport-docker.cfg" }`
> dan di-attach ke template — tanpa ini, container memakai config host (`teleport.cfg`)
> yang path-nya tidak ada di container → `failed to parse identity file`.

### D.6 Konfigurasi objek di dashboard Semaphore

> **Pembaruan (2026-09-02):** provisioner `provision/provision-semaphore.py` sudah
> ditulis ulang mengikuti layout repo ini (`playbook/…` + `inventory/hosts.yaml`)
> dan **idempoten** — sekali jalan membuat Repository `local`, Inventory
> `cek-backup`, Environment `teleport-ssh`, tiga Task Template
> (Backup/Verify/Restore), plus schedule harian (dibuat nonaktif):
>
> ```bash
> SEMAPHORE_URL=http://localhost:3001/api \
> SEMAPHORE_ADMIN=admin SEMAPHORE_ADMIN_PASSWORD=<password> \
>   python3 provision/provision-semaphore.py
> ```
>
> Langkah manual di bawah tetap valid sebagai referensi isi tiap field (dan untuk
> memahami apa yang dibuat provisioner).

Buka `http://192.0.2.10:3001`, login
dengan `SEMAPHORE_ADMIN` / `SEMAPHORE_ADMIN_PASSWORD` dari `.env.semaphore`.

Urutan pembuatan objek: **Project → Repository → Inventory → Environment → Key Store
(opsional) → Task Template**. Karena Ansible jalan di bind-mount working tree, semua
path **relatif terhadap root repository di dalam container** (`/home/semaphore/ansible`).

**1) Project.** Sidebar → **New Project** (mis. `backups`). Project mengelompokkan
semua objek di bawah ini.

**2) Repository** — tipe **`local`** (kode sudah ada via bind-mount, bukan di-clone):

| Field | Isi |
|-------|-----|
| Name | `local` |
| Type | **Local** |
| Path | `/home/semaphore/ansible` (**path DI DALAM container**, bukan `/root/opt/ansible`) |
| Branch | **kosongkan** (working-tree bind-mount — lihat catatan as-built Bagian F) |
| Access Key | **None** |

> Jangan isi `git_branch` ke `main` sebelum `teleport-docker.cfg` + `ansible.cfg`
> di-commit, kalau tidak clone bersih akan kehilangan file itu (lihat Bagian F).

**3) Inventory** — tipe **File** (menunjuk `inventory/hosts.yaml`):

| Field | Isi |
|-------|-----|
| Name | `cek-backup` |
| Type | **File** |
| Path to Inventory file | `inventory/hosts.yaml` (relatif ke repo) |
| Repository | pilih `local` di atas |
| User Credentials | **None** (auth lewat cert Teleport, bukan credential Semaphore) |
| Sudo Credentials | **None** |

**4) Environment `teleport-ssh`** — **WAJIB**, inilah yang menyuntikkan config SSH
container ke Ansible:

- **Project → Environment → New Environment**, nama `teleport-ssh`.
- Di kolom **Environment Variables** (JSON), isi:
  ```json
  { "ANSIBLE_SSH_ARGS": "-F /home/semaphore/ansible/teleport-docker.cfg" }
  ```
- Kolom **Extra Variables** biarkan `{}`.

> **Jangan andalkan `.env`/`ANSIBLE_SSH_ARGS` di OS container.** Semaphore **tidak**
> mewariskan environment OS ke proses ansible; ia hanya menyuntik variabel dari objek
> **Environment** miliknya. Tanpa Environment ini, ansible memakai `teleport.cfg`
> (path host `/opt/teleport/...`) yang tidak ada di container → `failed to parse
> identity file`.

**5) Key Store / Vault password (pilih salah satu):**

- **Cara file (paling sederhana, sesuai dokumen ini):** `.vault_pass_file` sudah ter-mount
  di repo dan `ansible.cfg` merujuknya (`vault_password_file = ./.vault_pass_file`),
  jadi **tidak perlu** objek apa pun di Semaphore — vault otomatis ke-decrypt.
- **Cara Key Store (lebih aman, opsional):** simpan password vault di **Key Store**
  (tipe *Login with password* / *None* berisi teks), lalu rujuk lewat
  `--vault-id`/credential template. Hilangkan `vault_password_file` dari `ansible.cfg`
  bila memakai cara ini agar tidak bentrok.

**6) Task Templates** — satu per playbook:

| Field | `backups` | `verify` | `restore` |
|-------|-----------|----------|-----------|
| Playbook | `playbook/backups/backup_mysql.yml` | `playbook/backups/verify_backup.yml` | `playbook/backups/restore_mysql.yml` |
| Inventory | `cek-backup` | `cek-backup` | `cek-backup` |
| Repository | `local` | `local` | `local` |
| Environment | **`teleport-ssh`** | **`teleport-ssh`** | **`teleport-ssh`** |

> **Path playbook relatif & tanpa spasi.** Mengisi absolut
> (`/home/semaphore/ansible/...`) menghasilkan path ganda
> `/home/semaphore/ansible/home/semaphore/ansible/...` → *playbook not found*.
> Spasi di akhir nama (`...backup_mysql.yml `) juga → *could not be found*.

**7) Menjalankan & membaca hasil.** Buka template → **Run**. Output task tampil
realtime. Sukses = `failed=0` dan recap berisi path file `.sql`, ukuran (MB), jumlah
tabel, status LENGKAP, lokasi `.sha256`. File ada di `/opt/backups/mysql/` pada host
target. Jadwalkan otomatis lewat tab **Schedule** (cron) bila perlu.

> **Via API (opsional):** saat `PUT` template, set **kedua** field `environment_id`
> **dan** `environment_ids:[id]`. Bila hanya `environment_id`, Semaphore mereset-nya
> ke `0` dan Environment tidak ter-attach.

> **Pastikan koneksi Teleport sehat sebelum Run.** Bila stack pernah mati >1 jam,
> identity bot kedaluwarsa → backup `UNREACHABLE ... tls: expired certificate`.
> Jalankan dulu `./teleport-backup-ready.sh` (lihat [Bagian F](#9-bagian-f--operasional--troubleshooting))
> untuk memulihkan token & cert sebelum menjalankan task dashboard.

---

## 8. Bagian E — Backup/Restore/Verify MySQL Multi-Versi

### E.1 Masalah inti: client mysqldump harus cocok versi server

Server target berbeda versi per port (contoh `.192`):

| Port | Versi server | Client mysqldump dipakai |
|------|--------------|--------------------------|
| 3307 | **5.1** (lawas) | *(tak didukung client modern — lihat catatan)* |
| 3308 | **5.7** | image `mysql:5.7` |
| 3309 | **8.3** | image `mysql:8.0` |

`mysqldump` 8.x terhadap server **5.1** bisa **gagal diam-diam** (hanya dump
trigger, tabel terlewat tanpa error). Karena itu role memakai **image Docker
sesuai versi** untuk menjalankan `mysqldump`, dipilih otomatis dari `SELECT
VERSION()`.

> **Catatan 5.1:** client `mysql`/`mysqldump` 8.0 menolak auth ke server 5.1
> (`Access denied ... using password: NO`, plugin password lama). Untuk
> mem-backup 5.1 butuh client 5.1/MariaDB lama (mis. image `vsamov/mysql-5.1`).

### E.2 Alur role `backup_mysql`

```
main.yml
  ├─ buat /opt/backups/mysql
  ├─ docker pull mysql:5.7, mysql:8.0          (client version-aware)
  └─ loop mysql_instances → backup_instance.yml
        ├─ discover DB (jika databases: "all") / ambil dari inventory
        ├─ SELECT VERSION()  → pilih _client_image (5.x→5.7, 8.x→8.0)
        └─ loop DB → backup_db.yml
              ├─ docker run --network host <image> mysqldump > file.sql
              ├─ assert footer "Dump completed" ada      (deteksi dump terputus)
              ├─ assert jumlah "CREATE TABLE" > 0         (deteksi backup kosong)
              └─ buat .sha256
```

Inti perintah dump (`backup_db.yml`):

```bash
docker run --rm --network host -e "MYSQL_PWD=$MYSQL_PWD" \
  {{ _client_image }} mysqldump {{ _db }} \
    --single-transaction --skip-lock-tables --quick --skip-triggers \
    --host=... --port=... --user=... > backup-<db>-<port>-<timestamp>.sql
```

Validasi integritas (kenapa penting): mysqldump bisa **exit 0 tapi file korup**
saat versi tak cocok. Maka role menegaskan footer `-- Dump completed` ada **dan**
jumlah `CREATE TABLE > 0`, lalu menyimpan SHA256.

### E.3 Kompatibilitas versi Ansible — `split`

Role memakai `(_var | trim).split('.')` (**method** Python), **bukan** filter
`| split('.')`. Filter `split` hanya ada di ansible-core baru; host `.128` pakai
**Ansible 2.10.8** → filter itu error `No filter named 'split'`. Method `.split()`
jalan di **kedua** versi (2.10 host & 2.20 container Semaphore).

### E.4 Prasyarat di host target

```bash
# role discovery/version-detect pakai client `mysql` langsung di host target:
apt-get install -y mysql-client-core-8.0
# dump-nya pakai image docker → host target butuh docker aktif:
systemctl enable --now docker
```

### E.5 Menjalankan

```bash
# host (default ansible.cfg sudah -F ./teleport.cfg = path host):
ansible-playbook playbook/backups/backup_mysql.yml
# Semaphore: jalankan template 'backups' di UI (Environment 'teleport-ssh' wajib ter-attach)
docker exec semaphore sh -c "cd /home/semaphore/ansible && ANSIBLE_SSH_ARGS='-F ./teleport-docker.cfg' ansible-playbook playbook/backups/backup_mysql.yml"
```

Hasil sukses (`failed=0`) menampilkan ringkasan: path file, ukuran (MB), jumlah
tabel, status LENGKAP, lokasi `.sha256`. File ada di `/opt/backups/mysql/` pada
host target.

### E.6 Restore & Verify (singkat)

- **`restore_mysql`** (`playbook/backups/restore_mysql.yml`): pasang client MySQL,
  pilih image restore version-aware (`.split('.')` method, sama seperti backup),
  restore tiap instance.
- **`restore_verify_mysql`** (`playbook/backups/verify_backup.yml`): salin
  `verify_mysql_integrity.py` ke target, bandingkan jumlah tabel/baris backup vs
  server (integrity check 1:1).

---

## 9. Bagian F — Operasional & Troubleshooting

Katalog error nyata yang ditemui saat membangun skema ini, lengkap penyebab & solusi.

### `failed to parse identity file ... no such file or directory`
Ansible memakai file config dengan path yang **tidak ada di konteksnya**.
- Jalan di **host** tapi pakai path container `/teleport-id/...` → pakai
  `teleport.cfg` (path host `/opt/teleport/...`).
- Jalan di **container** tapi pakai path host `/opt/teleport/...` → pakai
  `teleport-docker.cfg` (path `/teleport-id/...`) **dan** pastikan sidecar mengisi
  volume. Di Semaphore, sematkan Environment `teleport-ssh` (lihat D.6).

### `Bad configuration option: --destination-dir` (atau opsi lain)
`ProxyCommand` di `teleport.cfg` / `teleport-docker.cfg` ditulis multi-baris dengan
`\`. OpenSSH tidak mengenal line-continuation di `ssh_config` → baris lanjutan dibaca
sebagai opsi terpisah. **Tulis `ProxyCommand` dalam SATU baris penuh.**

### `hostname contains invalid characters` (transfer file Ansible)
Disebabkan `scp_if_ssh = True` → `scp` modern salah parse host lewat ProxyCommand
Teleport. **Solusi: hapus `scp_if_ssh`, pakai `transfer_method = piped` +
`pipelining = true`** di `ansible.cfg` (lihat C.2). Jangan pula taruh
`ansible_scp_if_ssh`/`ansible_ssh_common_args` di inventory.

### Sidecar `unknown userid 1001` saat menulis output identity
Terjadi bila sidecar dijalankan `user: "1001:0"` — UID 1001 tidak terdaftar di
`/etc/passwd` Alpine, sehingga tbot gagal resolve user untuk set ownership output.
**Solusi: jalankan KEDUA container sebagai `user: "0:0"` (root)** (lihat D.1).

### Sidecar `mkdir /teleport-id/bot-storage: permission denied`
Volume `teleport_id` tidak bisa ditulis user sidecar. Dengan kedua container root
(D.1): pastikan volume milik root →
`docker run --rm -v ansible_teleport_id:/d alpine chown -R 0:0 /d`.

### `Permission denied` baca `vault.yml` / `.vault_pass_file` dari container
File project dibuat root host dengan mode ketat. Solusi: jalankan container sebagai
**root** (`user: "0:0"`, sudah di D.1) **atau** `chmod -R a+rX /root/opt/ansible`.
Taruh `.vault_pass_file` di dalam folder project (ter-mount), rujuk relatif
`vault_password_file = ./.vault_pass_file`.

### `token expired or not found` padahal token fresh
Sering bukan soal expired, tapi **`proxy_server` salah IP** (mis. `.168`/`.192`
alih-alih `.128`) → join tidak pernah sampai ke proxy yang benar. Verifikasi
`grep proxy_server teleport/tbot-semaphore.yaml`. Cek alasan asli di audit auth:
`journalctl -u teleport --since "3 minutes ago" | grep -iE "token|join|bot"`.
Isolasi dari host:
`tbot start --destination-dir=/tmp/t --token=<T> --proxy-server=192.0.2.10:3080 --join-method=token --oneshot`.

### `No filter named 'split' found` (Ansible 2.10)
Role memakai filter `| split('.')` yang baru ada di ansible-core ≥2.11 (jalan di
container 2.20, gagal di host 2.10.8). Ganti ke method `(_var | trim).split('.')`
(kompatibel kedua versi). Sudah dipatch di `roles/backup_mysql/tasks/backup_instance.yml`
& `roles/restore_mysql/tasks/restore_instance.yml`.

### Error YAML inventory / playbook (saat menyusun file)
- `Colons in unquoted values must be followed by ...` → baris `all` ditulis tanpa
  `:`. Pakai `all:` dan indentasi **spasi** (bukan tab).
- `missing quotes around a template block` → value diawali `{{` tak dikutip. Tulis
  `mysql_password: "{{ vault_mysql_password }}"`.
- `Skipping unexpected key (host) ... only "vars","children","hosts"` → tulis
  `hosts:` (jamak), bukan `host:`.
- `the role 'backup_mysql' was not found` → role tidak ada di `roles_path`. Pastikan
  `roles/<nama>/tasks/main.yml` ada (lihat `roles_path` di `ansible.cfg`).

### `target host ... is offline or does not exist`
Node belum terdaftar di cluster. `ansible_host` harus `<nodename>.<cluster>` dan
node benar-benar muncul di `tctl nodes ls`. Tes manual:
`ssh -F teleport.cfg -v root@master.master-cluster`.

### Drift clock → sertifikat expired
Gejala: `ssh: cert has expired` (tbot), `tls: expired certificate` (proxy).
Penyebab: jam host melompat/mundur, atau host mati melewati TTL (1 jam).
**Recovery (host & sidecar sama polanya):**
```bash
# 1) token instance baru
tctl bots instances add ansible-bot       # untuk host tbot-ssh
# (sidecar: token baru lagi, masukkan ke tbot-semaphore.yaml)
# 2) bersihkan bot-storage lama (kalau tidak, tbot terus pakai identity kadaluarsa)
rm -f /opt/teleport/teleport-ssh/bot-storage/*                      # host
docker run --rm -v ansible_teleport_id:/d alpine sh -c 'rm -f /d/bot-storage/*'   # sidecar
# 3) restart
systemctl restart tbot-ssh
docker restart semaphore-tbot
```
Pencegahan: `timedatectl set-ntp true`. **Pantau clock `.128`.**

### Pemulihan otomatis: `teleport-backup-ready.sh`
Recovery di atas (token baru → bersihkan `bot-storage` → restart sidecar → tunggu
cert segar) sudah dibungkus skrip **idempoten** `teleport-backup-ready.sh` di root
project. Jalankan **sebelum** backup dari dashboard agar koneksi pasti sehat:

```bash
./teleport-backup-ready.sh            # cek koneksi; refresh token HANYA bila perlu (hemat token)
./teleport-backup-ready.sh --force    # paksa terbitkan token baru
./teleport-backup-ready.sh backup     # siapkan koneksi + jalankan playbook backup
./teleport-backup-ready.sh verify     # + jalankan verify   (restore = + jalankan restore)
```

Skrip mengetes jalur SSH persis seperti Ansible (`ssh -F teleport-docker.cfg` di
dalam container) lalu `ansible -m ping`. Sesuaikan variabel di kepala skrip dengan
lingkungan Anda: `BOT_NAME`, `CLUSTER`, `SSH_HOST` (`<node>.<cluster>`), `SSH_USER`,
`INVENTORY_TARGET`. Password vault dibaca dari `ANSIBLE_VAULT_PASSWORD` di
`.env.semaphore`.

### `Access denied ... using password: NO` ke MySQL 5.1
Client 8.0 tak kompatibel dengan plugin password server 5.1. Bukan salah
password. Gunakan client 5.1/MariaDB lama untuk port itu.

### `NO_MYSQLDUMP` / `mysql: command not found` / discovery gagal di target
Discovery & deteksi versi (`SELECT VERSION()`) jalan di **node target**, bukan di
container. Node belum punya client mysql → `apt-get install -y mysql-client-core-8.0`
(atau `mariadb-client`). Dump tetap memakai image docker sesuai versi server.

### Vault: `no vault secrets found` vs `Decryption failed`
- *"Attempting to decrypt but no vault secrets found"* → password belum disediakan
  (cek `vault_password_file` di `ansible.cfg` / `.vault_pass_file` ada).
- *"Decryption failed (no vault secrets were found that could decrypt)"* → password
  ada tapi **salah**. Verifikasi: `ansible-vault view vault.yml --ask-vault-pass`,
  lalu sinkronkan: `ansible-vault rekey vault.yml --new-vault-password-file .vault_pass_file`.

### Cek cepat kesehatan
```bash
systemctl status teleport tbot-ssh           # service host
docker ps                                    # semaphore, semaphore-db, semaphore-tbot UP
journalctl -u tbot-ssh -n 20                 # renew identity tiap 20m
docker logs --tail 20 semaphore-tbot
tctl bots instances list ansible-bot         # instance Healthy
# MySQL .192 terjangkau dari node target (lewat container):
docker exec semaphore sh -c "nc -zv 192.0.2.30 3308"
```

---

### Studi kasus terverifikasi: "Semaphore belum bisa backup" (.128, 2026-06-15)

Rangkaian error nyata saat backup dijalankan dari **dashboard Semaphore**, urut sesuai
munculnya. Semua sudah diperbaiki & diverifikasi (`task success`, `failed=0`, file
`/opt/backups/mysql/backup-demo_erpdb-3308-*.sql` + `.sha256`).

**1. `[ERROR] the playbook: playbook/backups/backup_mysql.yml  could not be found`**
Ada **spasi di akhir** field *Playbook* pada template. Ansible mencari nama file
persis termasuk spasi. → Edit template → hapus spasi di belakang `.yml`.

**2. `UNREACHABLE ... failed to parse identity file: /opt/teleport/teleport-ssh/machine-id/identity: no such file`**
`ansible.cfg` default memakai `-F ./teleport.cfg` yang berisi **path host**
(`/opt/teleport/...`) — path itu **tidak ada di dalam container**. Di container,
sertifikat ada di `/teleport-id/machine-id/` (volume `teleport_id`) dan dipakai oleh
**`teleport-docker.cfg`**.

> **Penting — jangan pakai `.env`:** menaruh `ANSIBLE_SSH_ARGS` di `.env` Docker
> **tidak berpengaruh**. Semaphore **tidak mewariskan environment OS container** ke
> proses ansible; ia hanya menyuntik variabel dari objek **Environment** miliknya.

Perbaikan (cara dashboard):
- **Project → Environment → New**: nama `teleport-ssh`, isi **Environment Variables**:
  ```json
  { "ANSIBLE_SSH_ARGS": "-F /home/semaphore/ansible/teleport-docker.cfg" }
  ```
- **Task Templates → backups → Edit → Environment → pilih `teleport-ssh` → Save.**
- (Via API: pada PUT template **wajib** set **kedua** field `environment_id` **dan**
  `environment_ids:[id]`; bila hanya `environment_id`, Semaphore mengembalikannya ke 0.)

**3. `No filter named 'split'` (saat dijalankan dari host, Ansible 2.10.8)**
Role memakai **filter** `| split('.')` di `roles/backup_mysql/tasks/backup_instance.yml`
dan `roles/restore_mysql/tasks/restore_instance.yml`. Filter `split` baru ada di
ansible-core ≥2.11, jadi jalan di container (2.20.6) tapi **gagal di host (2.10.8)**.
→ Ganti ke **method** `(_var | trim).split('.')` (kompatibel kedua versi). **Sudah
dipatch di repo** — kedua file kini memakai `((_var | trim).split('.') | first | int)`.

**4. Deteksi versi MySQL gagal: `mysql: command not found` di node target**
Task `SELECT VERSION()` berjalan **di node `master` (.128)**, bukan di container.
Node belum punya client MySQL. → `apt-get install -y mariadb-client` di `.128`.
(Dump tetap memakai image docker `mysql:5.7`/`8.0` sesuai versi server.)

### Catatan as-built — nilai final terverifikasi

Seluruh body dokumen (Bagian C/D) **sudah disinkronkan** dengan kondisi nyata yang
terverifikasi berjalan. Ringkasan keputusan kunci (sumber kebenaran tunggal):

| Hal | Nilai final (terverifikasi) |
|-----|------------------------------|
| Config SSH **host** | `teleport.cfg` (default di `ansible.cfg`, path `/opt/teleport/...`) |
| Config SSH **container** | `teleport-docker.cfg` (via Environment Semaphore `ANSIBLE_SSH_ARGS`, path `/teleport-id/...`) |
| User container (semaphore & tbot) | **`user: "0:0"` (root)** — gotcha uid-1001 tidak berlaku |
| Transfer ansible | **`transfer_method = piped` + `pipelining = true`** (BUKAN `scp_if_ssh`) |
| ProxyCommand | **satu baris** (line-continuation `\` tidak didukung ssh_config) |
| Vault | **`vault_password_file = ./.vault_pass_file`** (relatif), `vault.yml` ter-enkripsi |
| Recreate container | **`up -d --force-recreate`** (bukan `restart`) untuk perubahan `user:`/`volumes:` |
| Role `split` | **method** `(_var | trim).split('.')` (jalan di Ansible 2.10 & 2.20) |

**Objek Semaphore as-built:** project `backups` → template `backups`
(`playbook/backups/backup_mysql.yml`) → inventory `cek-backup`
(`/home/semaphore/ansible/inventory`, tipe file/direktori → auto-load `group_vars`) →
environment `teleport-ssh`.

**Provisioner tidak terpakai (known limitation).** `provision/provision-semaphore.py`
dibuat untuk layout lama (`playbooks/`, `inventories/<env>/hosts.yml`, branch `master`),
sedangkan repo ini memakai `playbook/`, `inventory/hosts.yaml`, branch `main`. Log:
`CRITICAL: Could not find playbooks directory`. Akibatnya provisioner **tidak membuat
apa pun** dan Semaphore dikonfigurasi **manual**. Jika ingin reprovision otomatis,
provisioner harus ditulis ulang menyesuaikan layout repo.

**Ketergantungan working-tree.** Repository Semaphore "backups" memakai **`git_branch`
kosong**, sehingga ansible berjalan langsung di bind-mount `/home/semaphore/ansible`
(working tree) — bukan clone bersih.

> ⚠️ Bila beralih ke `git_branch: main` (clone bersih), pastikan dulu file ini
> **ter-commit** di branch tersebut: `ansible.cfg`, `teleport.cfg`, `teleport-docker.cfg`,
> dan patch `.split` di roles. Sejak revisi ini ketiganya sudah tracked di repo
> (hanya `.env.semaphore`, `vault.yml`, `.vault_pass_file`, dan `teleport/tbot-semaphore.yaml`
> yang sengaja `.gitignore` karena berisi rahasia/token).

### Teleport — buat / reset user admin Web UI

Cluster `.128` hanya punya bot `ansible-bot` (tak ada user manusia). Web UI di
`https://192.0.2.10:3080`, auth `local` dengan `second_factor: otp` (wajib aplikasi
authenticator).

```bash
# Buat user admin (editor=admin, access=akses node):
tctl users add adit --roles=editor,access --logins=root --ttl=24h
#   → cetak URL undangan https://192.0.2.10:3080/web/invite/<token>
#   buka di browser → set password → scan QR TOTP → login.

# Reset password+MFA user yang sudah ada:
tctl users reset <username>

# Hapus user:
tctl users rm <username>
```

> Operasi user **tidak menyentuh** `ansible-bot` maupun proses backup.

---

## 10. Lampiran — Layout File & Cheat-Sheet

### Layout direktori (host `.128`)

```
/etc/teleport.yaml                      # config cluster
/etc/systemd/system/teleport.service
/etc/systemd/system/tbot-ssh.service
/opt/teleport/
  system/bin/{teleport,tctl,tbot,tsh}   # binary (symlink ke /usr/local/bin)
  teleport-ssh/
    tbot-ssh.yml                         # config tbot host
    bot-storage/                         # state bot (renewable)
    machine-id/                          # OUTPUT sertifikat (key, cert, ssh_config, ...)
/root/opt/ansible/                       # project Ansible + Semaphore
  ansible.cfg                            # default -F ./teleport.cfg (path HOST)
  teleport.cfg                           # ssh_config HOST (/opt/teleport/...)  ← default
  teleport-docker.cfg                    # ssh_config CONTAINER (/teleport-id/...) ← dipakai Semaphore
  .vault_pass_file                       # password Ansible Vault (vault_password_file)
  docker-compose.yml
  .env                                   # kredensial Semaphore/Postgres + ANSIBLE_VAULT_PASSWORD
  inventory/hosts.yaml
  inventory/group_vars/all/vault.yml     # vault_mysql_password (ter-enkripsi ansible-vault)
  teleport/tbot-semaphore.yaml           # config sidecar (container jalan sbg root 0:0)
  provision/provision-semaphore.py       # ⚠ tidak cocok layout repo ini (lihat Bagian F)
  roles/{backup_mysql,restore_mysql,restore_verify_mysql,...}
  playbook/backups/{backup_mysql,restore_mysql,verify_backup}.yml
/opt/backups/mysql/                      # OUTPUT backup .sql + .sha256
```

### Cheat-sheet perintah

```bash
# --- Teleport / bot ---
tctl status                              # status cluster
tctl nodes ls                            # daftar node (kolom Host = ansible_host prefix)
tctl bots ls                             # daftar bot
tctl bots instances list ansible-bot     # instance + kesehatan
tctl bots instances add ansible-bot      # token instance baru (sidecar / re-join)

# --- tbot host ---
systemctl restart tbot-ssh
journalctl -u tbot-ssh -f
ssh -F /opt/teleport/teleport-ssh/machine-id/ssh_config root@master.master-cluster whoami

# --- Semaphore / sidecar ---
docker compose up -d --force-recreate tbot semaphore   # PROPER recreate (perubahan user:/volumes:)
docker logs --since 1m -f semaphore-tbot                # log terbaru saja
docker exec semaphore sh -c "cd /home/semaphore/ansible && ANSIBLE_SSH_ARGS='-F ./teleport-docker.cfg' ansible all -m ping"
docker exec semaphore sh -c "nc -zv 192.0.2.30 3308" # MySQL .192 reachable dari node

# --- Pemulihan koneksi (token/cert expired) ---
./teleport-backup-ready.sh            # cek + refresh bila perlu
./teleport-backup-ready.sh backup     # siapkan koneksi + jalankan backup

# --- Ansible (host; default ansible.cfg sudah pakai teleport.cfg path host) ---
ansible all -m ping
ansible-playbook playbook/backups/backup_mysql.yml

# --- Ansible (paksa config container, mis. saat uji di dalam container) ---
ANSIBLE_SSH_ARGS='-F ./teleport-docker.cfg' ansible all -m ping
```

### Catatan IP (referensi)

| IP / Port | Peran |
|-----------|-------|
| `192.0.2.10` | master — auth + proxy + ssh Teleport, host Docker/Semaphore |
| `192.0.2.30` | MySQL server (backup target, via TCP, tidak di-join Teleport) |
| `3080` | proxy Teleport (multiplex / TLS routing) |
| `3025` | auth Teleport |
| `3022` | port SSH node Teleport |
| `3001` | UI Semaphore (`http://<host>:3001`) |
| `3307/3308/3309` | instance MySQL 5.1 / 5.7 / 8.3 yang di-backup |

### Ringkasan keamanan

- `--insecure` pada semua `tbot` karena proxy self-signed. Untuk produksi pasang
  cert valid lalu hapus flag ini.
- Role `ansible_ssh` memberi akses `*` ke semua node — persempit `node_labels`
  untuk least-privilege.
- Enkripsi `vault.yml` dengan `ansible-vault` di produksi.
- Token join bersifat sekali-pakai & kadaluarsa ~60 menit; simpan hanya di file
  yang di-`.gitignore` (`tbot-ssh.yml`, `tbot-semaphore.yaml`).
- Sertifikat berumur pendek (TTL 1 jam, renew 20 menit) — tidak ada kredensial
  statis yang bocor bila file output dicuri (kadaluarsa cepat).

**Roadmap pengerasan (bukan blocker fungsional):**
1. Ganti `--insecure` dengan cert proxy valid / CA pin yang benar.
2. Pertimbangkan user non-root (UID terdaftar) untuk container produksi.
3. Pindahkan vault password ke **Semaphore Key Store** (bukan file mode 644) — lihat D.6.
4. Audit ulang dampak `chmod -R a+rX` pada project — jangan ekspos file sensitif.
5. Pertimbangkan join method tbot yang lebih persisten daripada token sekali-pakai.

---

*Dokumen ini merefleksikan deployment nyata per 2026-06-15 dan disinkronkan ulang
(Convention Y, satu konvensi tunggal) per 2026-06-17 — Teleport v18.8.3, Ansible
2.10.8 host / 2.20.6 container, Semaphore di Docker. Sesuaikan IP, nama cluster/node,
dan versi sesuai lingkungan target Anda.*