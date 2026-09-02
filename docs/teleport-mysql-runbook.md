# Runbook: Akses MySQL via Teleport (Semaphore → Teleport → MySQL)

Dokumen ini menjelaskan cara mengarahkan backup/verify/restore Ansible agar
koneksi ke MySQL melewati Teleport (audit log, RBAC, auth sertifikat pendek,
tanpa password statis), **tanpa merusak MySQL produksi**.

```
mysqldump ─► tbot database-tunnel (127.0.0.1:1330x) ─► Teleport proxy/auth
          ─► teleport db_service ─► MySQL (auth via cert, REQUIRE SUBJECT)
```

Skema ini sudah **dibuktikan end-to-end** memakai MySQL throwaway lokal
(lihat bagian "Status pembuktian" di bawah). Yang tersisa untuk produksi
adalah konfigurasi TLS di sisi server MySQL .192.

---

## ⚠️ Prasyarat & batasan

| Hal | Keterangan |
|-----|------------|
| TLS di MySQL | **Wajib.** Teleport hanya bisa proxy database yang mendukung TLS. |
| MySQL 5.1 (port 3307) | **Tidak didukung** — `have_ssl=DISABLED`. Tetap koneksi langsung. |
| MySQL 5.7 (3308), 8.x (3309) | `have_ssl=YES` → bisa via Teleport. |
| Akses OS ke .192 | **Diperlukan** untuk Langkah A (taruh file CA + edit my.cnf + reload TLS). |
| Versi tbot | ≥ 18.6.4 untuk `database-tunnel` (host ini sudah v18.8.2). |

---

## Langkah A — Sisi MySQL produksi (.192) — NON-BREAKING

> Dilakukan oleh admin yang punya akses OS ke 192.0.2.30.
> Bersifat **aditif**: TIDAK memakai `require_secure_transport=ON`, jadi semua
> koneksi aplikasi/password yang ada tetap berjalan normal. Hanya menambah
> kemampuan TLS + satu user khusus Teleport.

### A.1 Export CA Teleport (jalankan di host auth Teleport)

```bash
sudo tctl auth sign --format=db \
  --host=192.0.2.30 \
  --out=mysql-teleport --ttl=2190h
# menghasilkan: mysql-teleport.cas (CA), mysql-teleport.crt, mysql-teleport.key
```

Salin ketiga file ke .192, mis. ke `/etc/mysql/teleport/` (chown mysql:mysql,
chmod 600 untuk `.key`).

### A.2 Aktifkan TLS di my.cnf (per instance 3308 & 3309) — ADITIF

```ini
[mysqld]
ssl-ca=/etc/mysql/teleport/mysql-teleport.cas
ssl-cert=/etc/mysql/teleport/mysql-teleport.crt
ssl-key=/etc/mysql/teleport/mysql-teleport.key
# JANGAN set require_secure_transport=ON — biarkan koneksi lama tetap jalan
```

Muat ulang TLS **tanpa restart** (MySQL 8.0):
```sql
ALTER INSTANCE RELOAD TLS;
```
MySQL 5.7 tidak punya RELOAD TLS → perlu restart terjadwal di maintenance window.

### A.3 Buat user khusus Teleport (auth via sertifikat, tanpa password)

```sql
-- CN harus sama dengan teleport_db_user di inventory Ansible
CREATE USER 'teleport-backup'@'%' REQUIRE SUBJECT '/CN=teleport-backup';
-- hak akses minimum untuk backup (read-only + lock + show view)
GRANT SELECT, LOCK TABLES, SHOW VIEW, EVENT, TRIGGER ON *.* TO 'teleport-backup'@'%';
FLUSH PRIVILEGES;
```

> Catatan: subject persisnya bisa diverifikasi dengan menghubungkan sekali dan
> cek error log; jika `/CN=...` saja menolak, gunakan `REQUIRE X509` (menerima
> sertifikat valid apa pun dari CA Teleport — sedikit lebih longgar).

---

## Langkah B — Sisi Teleport (host ini / agen mana pun yg bisa menjangkau .192)

### B.1 Role

`teleport/role-db-backup.yaml` lalu `tctl create -f`. Lihat file template di repo.

### B.2 Daftarkan database (agen terpisah — TIDAK mengubah /etc/teleport.yaml)

Pakai `teleport/db-agent.yaml.example`, isi token dari:
```bash
sudo tctl tokens add --type=db --ttl=1h
```
Jalankan via systemd unit `teleport/systemd/teleport-db-agent.service`.

### B.3 Bot (Machine ID) untuk Semaphore + tunnel

```bash
sudo tctl bots add semaphore-backup --roles=db-backup
```
Isi token bot ke `teleport/tbot.yaml.example`, jalankan via systemd unit
`teleport/systemd/tbot.service`. tbot membuka tunnel lokal 13308/13309.

---

## Langkah C — Aktifkan di Ansible

Edit `inventory/hosts.yaml`, buka komentar pada instance yang sudah siap:

```yaml
mysql_instances:
  - port: 3307
    databases: "demo_legacydb"      # 5.1 → tetap langsung
  - port: 3308
    databases: "demo_erpdb"
    teleport_local_port: 13308         # ← aktifkan
    teleport_db_user: "teleport-backup"
  - port: 3309
    databases: "demo_accountingdb"
    teleport_local_port: 13309         # ← aktifkan
    teleport_db_user: "teleport-backup"
```

Tanpa field `teleport_local_port`, instance memakai koneksi langsung seperti
sebelumnya — jadi 3307 tidak berubah dan rollback cukup meng-comment lagi.

Jalankan seperti biasa: `ansible-playbook playbook/backups/backup_mysql.yml`.
Logika version-aware client (mysql:5.7/8.0) tetap bekerja melalui tunnel.

---

## Verifikasi & rollback

- Cek tunnel hidup: `ss -ltn | grep 1330`
- Cek audit: event `db.session.start` muncul di Teleport (Web UI → Audit Log).
- Rollback Ansible: comment kembali `teleport_local_port` (langsung instan).
- Rollback MySQL: hapus user `teleport-backup` + baris ssl-* di my.cnf (opsional;
  membiarkannya tidak merusak apa pun karena aditif).

---

## Status pembuktian (dilakukan pada sesi setup)

Rantai penuh sudah diuji dengan MySQL 8.0 throwaway lokal yang trust CA Teleport:
- `mysqldump` via tunnel Teleport (tanpa password) → dump valid + footer lengkap.
- Koneksi langsung sebagai user X509 tanpa cert → `ERROR 1045 Access denied`
  (membuktikan satu-satunya jalan masuk adalah lewat Teleport).
- Audit log mencatat `db.session.start`/`end` dgn identitas `bot-semaphore-backup`.
- Playbook `backup_mysql` dengan `teleport_local_port` → backup sukses, versi
  terdeteksi via tunnel, client mysql:8.0 dipilih otomatis.
