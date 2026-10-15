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

## Mengatur backup (cepat)

| Mau | Cara |
|---|---|
| Backup **semua** DB | inventory: `databases: all` — atau sekali jalan `-e backup_databases=all` |
| Backup **satu/beberapa** DB | inventory: `databases: "db1,db2"` — atau `-e backup_databases=db1` |
| Semua DB **kecuali** beberapa | `databases: all` + `exclude_databases: "db_besar"` (atau `-e backup_exclude_databases=db_besar`) |
| Hanya satu server | `--limit nama-host` (di Semaphore: kolom *Limit*) |
| Ubah retensi | `mysql_backup_retention_days: 5` (inventory) atau `-e backup_retention_days=3`; `0` = nonaktif |

Di dashboard, template **Backup MySQL** punya form isian *Database* dan *Retensi*
(kosongkan = ikut inventory).

Perilaku run:

- **Retensi:** setiap run menghapus semua `backup-*` (`.sql`, `.sha256`, `.err`) yang
  lebih tua dari 5 hari di folder host, apa pun isinya dan walau tidak ada backup
  penggantinya. Pastikan salinan penting disimpan di tempat lain.
- **Tahan gagal:** satu DB gagal tidak menghentikan DB lain. Dump yang terputus
  (`2013 Lost connection`) diulang otomatis 2×. Di akhir ada ringkasan ✔/✘ per DB
  dan run ditandai gagal bila ada yang ✘, jadi yang perlu dibackup manual langsung
  terlihat.
- Opsi lanjutan (`defaults/main.yml`): `mysql_backup_retries`, `mysql_dump_extra_args`.

## Notifikasi Telegram + backup ulang dengan persetujuan manusia

Bila ada DB gagal di-backup, bot mengirim pesan Telegram berisi server, DB, versi
MySQL, dan **log penyebab**, dengan tiga tombol:

| Tombol | Aksi |
|---|---|
| 🟢 Backup | menjalankan template *Backup MySQL* di Semaphore hanya untuk DB itu (`backup_databases`, `backup_ports`, `--limit` host). Setelah selesai bot melapor **sukses** atau **gagal lagi** + log |
| ⚪ Tunda | pesan muncul lagi setelah `BOT_SNOOZE_MINUTES` (default 60) |
| 🔴 Batal | ditutup, tidak ada aksi |

Bila lebih dari satu DB gagal, ada pesan tambahan dengan tombol yang sama untuk
semuanya. Hanya `TELEGRAM_ALLOWED_USER_IDS` (default: `TELEGRAM_CHAT_ID`) yang
boleh menekan tombol.

Setup:

1. Buat bot lewat @BotFather → salin token. Kirim satu pesan ke bot, lalu ambil chat id Anda
   (mis. @userinfobot).
2. Isi `.env.semaphore`: `TELEGRAM_BOT_TOKEN=...` dan `TELEGRAM_CHAT_ID=...`
3. `docker compose up -d backup-bot` lalu jalankan ulang provisioner
   (`docker compose run --rm provisioner`) agar Environment Semaphore mendapat
   `backup_bot_url`. Tanpa token, notifikasi nonaktif dan backup berjalan seperti biasa.

Catatan: bot memakai *long-polling* (tanpa port publik) sehingga tombol hanya bekerja
saat PC ini menyala.

### Cek stack automysqlbackup di Portainer (Senin–Jumat 08:00 WIB)

Setiap hari kerja (`BOT_CHECK_DAYS`, default Senin–Jumat) pukul `BOT_CHECK_TIME` (default
08:00 WIB; bila PC baru menyala setelahnya, dijalankan saat itu juga) bot login ke Portainer (read-only) dan memeriksa semua
container di `PORTAINER_STACKS`. Kirim `/cek` ke bot untuk menjalankannya sekarang.
Yang dilaporkan:

- **Error mysqldump** pada run terakhir, per database (`Lost connection`, `command denied`, dll.).
  `information_schema`/`performance_schema`/`sys` diabaikan (`BOT_IGNORE_DBS`) karena
  selalu error dan bukan data nyata.
- **Backup tidak berjalan:** tidak ada run baru dalam `BOT_STALE_HOURS` (30 jam) — kegagalan
  senyap yang tidak terlihat sebagai error di log.
- **Run tidak selesai** (dimulai tapi tidak ada `Backup End`) dan **container tidak running**.
- Error dicari di **semua run sejak cek terakhir**, jadi kegagalan Sabtu/Minggu ikut
  terlaporkan pada hari Senin (diberi tanggal run-nya).
- Masalah yang sama berhari-hari diberi penanda "gagal N hari berturut-turut".
- Bila semua sehat tetap dikirim ringkasan "✅ tidak ada masalah", jadi diam = bot mati.

Tombol 🟢 Backup hanya muncul bila server MySQL itu (`DBHOST:DBPORT`) terdaftar di
`inventory/hosts.yaml` (aktif, tidak dikomentari); selain itu hanya ⚪ Tunda / 🔴 Batal.
Untuk mengaktifkannya pada server lain, tambahkan server itu ke inventory.

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

## Struktur

```text
.gitignore                     # semua file bernilai asli ditahan di sini
.env.example                   # → .env  (cluster/node/IP; dipakai compose & skrip)
ansible.cfg                    # default: koneksi via ./teleport.cfg
docker-compose.yml             # semaphore + postgres + sidecar tbot + provisioner
teleport-backup-ready.sh       # pemulihan token/cert Teleport (idempoten)
teleport.cfg / teleport-docker.cfg
provision/provision-semaphore.py   # bikin objek dashboard (idempoten)
inventory/hosts.yaml           # target + daftar database
bot/                           # bot Telegram (backup_bot.py, stdlib saja)
playbook/backups/              # backup_mysql.yml, restore_mysql.yml, verify_backup.yml
playbook/dashboard-ui/         # setup/service Semaphore (opsional, control_nodes)
roles/                         # backup_mysql, restore_mysql, restore_verify_mysql, dll.
teleport/                      # config bot + unit systemd + role Teleport
docs/                          # panduan lengkap & runbook
```

## Keamanan

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
