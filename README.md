<<<<<<< HEAD
# Ansible ADIT

Ansible project untuk provisioning dua node:

- `dev-apps`: aplikasi berjalan langsung di host (non-Docker) dengan Nginx dan Keepalived.
- `dev-db`: MySQL berjalan di Docker container.

> Catatan keamanan: IP dan kredensial asli tidak disimpan di repository. Inventory memakai alamat dokumentasi `192.0.2.0/24` sebagai placeholder. Override nilai asli lewat environment variable, extra vars, atau file inventory private yang tidak di-commit.
=======
# Ansible ADIT — Backup MySQL via Teleport + Dashboard Semaphore

Stack operasional untuk **backup / restore / verify** database MySQL produksi,
dijalankan dari dashboard **Semaphore**, dengan koneksi SSH lewat **Teleport**
(sertifikat pendek, tanpa password SSH).

```
Semaphore (Docker :3001) ── ansible ─┐
                                     │ ssh -F teleport-docker.cfg
                                     ▼
                        Teleport proxy (example-cluster, :3080)
                                     │
                                     ▼
                    node 'developer' (host ini, login root)
                                     │ docker run mysql:5.7|8.0 mysqldump
                                     ▼
                    MySQL produksi 192.0.2.30 :3307/:3308/:3309
                                     │
                                     ▼
                          /opt/backups/mysql/*.sql (+ .sha256)
```

Panduan lengkap dari nol: [docs/panduan-setup-lengkap.md](docs/panduan-setup-lengkap.md).

## Komponen aktif

| Komponen | Detail |
|---|---|
| Teleport (auth+proxy+ssh) | `systemd teleport.service`, cluster `example-cluster`, node `developer` |
| Machine ID host | `systemd tbot-ssh.service`, bot `ansible-bot` → `/home/developer/teleport-ssh/machine-id/` |
| Machine ID container | sidecar `semaphore-tbot`, bot `semaphore-bot` → volume `teleport_id` → `/teleport-id/machine-id/` |
| Semaphore | `docker compose`, UI di `http://localhost:3001` (kredensial di `.env.semaphore`) |
| Target backup | `dev-db` = `developer.example-cluster` (host ini), dump ke `/opt/backups/mysql` |

## Menjalankan

Repo ini **tidak menyimpan nilai asli**. Setelah clone, buat file lokal Anda dari
templatnya (semuanya sudah masuk `.gitignore`):

```bash
cp .env.example                              .env                  # cluster/node/IP proxy Teleport
cp .env.semaphore.example                    .env.semaphore        # kredensial dashboard + postgres
cp inventory/hosts.yaml.example              inventory/hosts.yaml  # target + daftar database
cp inventory/group_vars/all/vault.yml.example inventory/group_vars/all/vault.yml   # password MySQL
cp teleport.cfg.example                      teleport.cfg          # ssh_config jalur host
cp teleport-docker.cfg.example               teleport-docker.cfg   # ssh_config jalur container
cp teleport/tbot-semaphore.yaml.example      teleport/tbot-semaphore.yaml
# lalu ganti semua placeholder (CLUSTER, PROXY_HOST, PROXY_ALIAS, USER, CHANGE_ME_*)
```

```bash
# 1. Stack dashboard
docker compose up -d

# 2. Pastikan sertifikat Teleport segar (idempoten; hanya bertindak bila perlu)
./teleport-backup-ready.sh

# 3. Buat/selaraskan objek dashboard (Repository, Inventory, Environment, Template)
SEMAPHORE_URL=http://localhost:3001/api \
SEMAPHORE_ADMIN=admin SEMAPHORE_ADMIN_PASSWORD="$SEMAPHORE_ADMIN_PASSWORD" \
  python3 provision/provision-semaphore.py
```

Lalu buka `http://localhost:3001` → project **backup** → **Task Templates**:

| Template | Playbook | Fungsi |
|---|---|---|
| **Backup MySQL** | `playbook/backups/backup_mysql.yml` | mysqldump (client versi-aware) + assert footer & jumlah tabel + `.sha256` |
| **Verify Backup** | `playbook/backups/verify_backup.yml` | restore backup terbaru ke container sementara, bandingkan 1:1 dengan produksi |
| **Restore MySQL** | `playbook/backups/restore_mysql.yml` | restore ke container MySQL terpisah (**tidak** menimpa produksi), auto-hapus 3 hari |

Schedule harian `0 2 * * *` sudah terpasang untuk *Backup MySQL* dalam keadaan
**nonaktif** — aktifkan di tab **Schedule** bila ingin jalan otomatis.

CLI (tanpa dashboard):

```bash
ansible dev-db -m ping                                  # uji jalur Teleport
ansible-playbook playbook/backups/backup_mysql.yml
./teleport-backup-ready.sh backup                       # refresh cert + backup sekaligus
```

## Konfigurasi yang perlu diketahui

- **`inventory/hosts.yaml`** — daftar database yang di-backup ada di
  `mysql_instances`. Isi `databases:` dengan daftar dipisah koma, atau `"all"`
  untuk semua DB non-system (port 3308 punya 143 DB — hati-hati soal ruang disk).
  Port 3307 (MySQL 5.1) tidak bisa dipakai client `mysql` 8.x; port 3309 (8.3)
  tersedia sebagai blok komentar.
- **`inventory/group_vars/all/vault.yml`** — password MySQL (`vault_mysql_password`).
  Tidak di-commit; contoh ada di `vault.yml.example`.
- **`ansible.cfg`** — `ssh_args = -F ./teleport.cfg` (jalur host) dan
  `transfer_method = piped` (**wajib** untuk Teleport; `scp` salah mem-parse host).
- **`teleport.cfg`** vs **`teleport-docker.cfg`** — beda path sertifikat: host
  (`/home/developer/teleport-ssh/...`) vs container (`/teleport-id/...`).
  Versi container disuntik lewat Environment Semaphore `teleport-ssh`
  (`ANSIBLE_SSH_ARGS`); tanpa itu → `failed to parse identity file`.

## Bila backup gagal `tls: expired certificate`

Sertifikat bot hanya berlaku ~1 jam. Jika stack mati lebih lama dari itu, bot
tidak bisa memperbarui diri sendiri:

```bash
./teleport-backup-ready.sh          # terbitkan token baru, bersihkan identity, restart sidecar
```

Untuk bot **host** (`tbot-ssh.service`) — bila `ansible dev-db -m ping` gagal
sementara jalur container sehat:

```bash
sudo tctl bots instances add ansible-bot            # salin token
sudo sed -i -E 's|^(\s*token:\s*).*|\1"<TOKEN>"|' /home/developer/teleport-ssh/tbot-ssh.yaml
sudo systemctl stop tbot-ssh
sudo rm -rf /home/developer/teleport-ssh/{bot-storage,machine-id}/*
sudo systemctl start tbot-ssh
```
>>>>>>> 1714484 (Ansible + Teleport + Semaphore: backup/restore/verify MySQL)

## Struktur

```text
<<<<<<< HEAD
ansible.cfg
inventory/
  hosts.yaml
playbook/
  apps/main.yml
  database/main.yml
roles/
  install_nginx/
  config_nginx/
  service_nginx/
  keepalived/
  install_mysql/
  config_mysql/
  service_mysql/
```

## Arsitektur

| Node | Role | Mode |
| --- | --- | --- |
| `dev-apps` | Nginx + Keepalived | Native host / non-Docker |
| `dev-db` | MySQL | Docker container |

## Prasyarat

- Ansible core terpasang di control node.
- SSH dari control node ke target host.
- Target berbasis Debian/Ubuntu untuk role `apt`.
- Docker sudah terpasang di node DB, atau minimal service Docker tersedia.
- Collection `community.docker` tersedia.

Cek collection:

```bash
ansible-galaxy collection list community.docker
```

Install bila belum ada:

```bash
ansible-galaxy collection install community.docker
```

## Konfigurasi Host

File [inventory/hosts.yaml](inventory/hosts.yaml) memakai placeholder:

```yaml
dev-apps:
  ansible_host: 192.0.2.10

dev-db:
  ansible_host: 192.0.2.20
```

Ganti dengan IP asli sebelum deploy, atau buat file inventory private seperti `inventory/local.yaml` (sudah masuk `.gitignore`) lalu jalankan:

```bash
ansible-playbook -i inventory/local.yaml playbook/apps/main.yml
ansible-playbook -i inventory/local.yaml playbook/database/main.yml
```

## Kredensial

Kredensial tidak disimpan di repository. Gunakan environment variable:

```bash
export ANSIBLE_USER=root
export MYSQL_ROOT_PASSWORD='replace-with-strong-password'
export KEEPALIVED_AUTH_PASS='replace-with-vrrp-password'
```

Untuk password SSH, disarankan pakai SSH key. Jika tetap harus password, pakai Ansible Vault atau inventory private yang tidak di-commit.

## Variabel Penting

MySQL Docker:

```yaml
mysql_container_name: mysql
mysql_image: mysql:8.0
mysql_host_port: 3306
mysql_container_port: 3306
mysql_database: app_db
```

Keepalived:

```yaml
keepalived_interface: enp2s0
keepalived_virtual_ip: 192.0.2.100
keepalived_priority: 100
```

Sesuaikan `keepalived_interface` dengan interface target, contoh `ip addr` di node.

## Validasi

Syntax check:

```bash
ansible-playbook playbook/apps/main.yml --syntax-check
ansible-playbook playbook/database/main.yml --syntax-check
```

List task:

```bash
ansible-playbook playbook/apps/main.yml --list-tasks
ansible-playbook playbook/database/main.yml --list-tasks
```

Dry run:

```bash
ansible-playbook playbook/apps/main.yml --check --diff -v
ansible-playbook playbook/database/main.yml --check --diff -v
```

## Deploy

Deploy app node:

```bash
ansible-playbook playbook/apps/main.yml -v
```

Deploy DB node:

```bash
ansible-playbook playbook/database/main.yml -v
```

## Cek Hasil

Cek Nginx:

```bash
ansible dev-apps -m shell -a 'nginx -t && systemctl status nginx --no-pager' -b
```

Cek Keepalived:

```bash
ansible dev-apps -m shell -a 'systemctl status keepalived --no-pager' -b
```

Cek MySQL container:

```bash
ansible dev-db -m shell -a 'docker ps --filter name=mysql' -b
```

Cek koneksi MySQL:

```bash
ansible dev-db -m shell -a 'docker exec mysql mysql -uroot -p"$MYSQL_ROOT_PASSWORD" -e "SELECT VERSION();"' -b
```

## Upload ke GitHub

Inisialisasi repo:

```bash
git init
git add .
git commit -m "Initial Ansible project cleanup"
```

Buat repository kosong di GitHub, lalu hubungkan remote:

```bash
git remote add origin https://github.com/<username>/<repo-name>.git
git branch -M main
git push -u origin main
```

Jika memakai SSH:

```bash
git remote add origin git@github.com:<username>/<repo-name>.git
git branch -M main
git push -u origin main
=======
.gitignore                     # semua file bernilai asli ditahan di sini
.env.example                   # → .env  (cluster/node/IP; dipakai compose & skrip)
ansible.cfg                    # default: koneksi via ./teleport.cfg
docker-compose.yml             # semaphore + postgres + sidecar tbot + provisioner
teleport-backup-ready.sh       # pemulihan token/cert Teleport (idempoten)
teleport.cfg / teleport-docker.cfg
provision/provision-semaphore.py   # bikin objek dashboard (idempoten)
inventory/hosts.yaml           # target + daftar database
playbook/backups/              # backup_mysql.yml, restore_mysql.yml, verify_backup.yml
playbook/dashboard-ui/         # setup/service Semaphore (opsional, control_nodes)
roles/                         # backup_mysql, restore_mysql, restore_verify_mysql, dll.
teleport/                      # config bot + unit systemd + role Teleport
docs/                          # panduan lengkap & runbook
>>>>>>> 1714484 (Ansible + Teleport + Semaphore: backup/restore/verify MySQL)
```

## Keamanan

<<<<<<< HEAD
- Jangan commit password SSH, token, private key, atau IP internal produksi.
- Pakai SSH key atau Ansible Vault untuk secret.
- File private seperti `.env`, `inventory/local.yaml`, dan file vault sudah diabaikan oleh `.gitignore`.
=======
Repo ini publik, jadi **tidak ada satu pun nilai asli** yang di-commit. Yang
ditahan `.gitignore`:

| Kategori | File |
|---|---|
| Kredensial | `.env`, `.env.semaphore`, `*vault*.yml`, `.vault_pass_file` |
| Identitas Teleport | `teleport/tbot-*.yaml` (join token), `teleport.cfg`, `teleport-docker.cfg`, `*.key`, `*-cert.pub` |
| Topologi & nama klien | `inventory/hosts.yaml` |
| Data produksi | `*.sql`, `*.sql.gz`, `*.zip`, `proaccounting/`, `testing/` |
| Catatan privat | `private/` |

Semua IP di dokumentasi memakai rentang dokumentasi RFC 5737 (`192.0.2.0/24`),
bukan alamat sungguhan.

Sebelum `git push`, biasakan memeriksa:

```bash
git status --short                     # pastikan tidak ada file rahasia ter-stage
git ls-files | xargs grep -nIE 'password[[:space:]]*[:=][[:space:]]*["'"'"']?[^"'"'"'[:space:]{<]{3,}' | grep -v CHANGE_ME
git ls-files | xargs grep -nIE '\b[a-f0-9]{32}\b|192\.168\.'   # token & IP internal
```

Catatan lain:

- Token bot Teleport bersifat **sekali-pakai** dan berumur ~1 jam, jadi kalaupun
  bocor nilainya cepat mati — tapi tetap jangan di-commit.
- Setiap sesi SSH tercatat di **Audit Log / Session Recordings** Teleport.
>>>>>>> 1714484 (Ansible + Teleport + Semaphore: backup/restore/verify MySQL)
