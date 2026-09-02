# Teleport untuk Semaphore/Ansible

Dua skema independen:

1. **SSH Ansible via Teleport** (AKTIF) — koneksi Ansible ke host target lewat
   Teleport, bukan SSH:22. Auth sertifikat, tanpa password. Tidak menyentuh MySQL.
   Panduan: [`../docs/teleport-ssh-ansible.md`](../docs/teleport-ssh-ansible.md)

2. **MySQL via Teleport** (disiapkan, menunggu setup .192) — koneksi MySQL backup
   lewat Teleport database-tunnel.
   Panduan: [`../docs/teleport-mysql-runbook.md`](../docs/teleport-mysql-runbook.md)

## Isi direktori

| File | Skema | Fungsi |
|------|-------|--------|
| `role-ansible-ssh.yaml` | SSH | Role login SSH untuk bot Ansible |
| `tbot-ssh.yaml.example` | SSH | Machine ID → ssh_config untuk Ansible |
| `systemd/tbot-ssh.service` | SSH | Unit systemd tbot SSH (terpasang & aktif) |
| `role-db-backup.yaml` | DB | Role Teleport untuk bot backup DB |
| `db-agent.yaml.example` | DB | Agen Database Service (3308/3309) |
| `tbot.yaml.example` | DB | Machine ID + database-tunnel |
| `systemd/teleport-db-agent.service` | DB | Unit systemd agen database |
| `systemd/tbot.service` | DB | Unit systemd tbot database |

## Alur singkat

1. **MySQL .192**: trust CA Teleport + user `REQUIRE SUBJECT` (aditif, non-breaking) — Runbook Langkah A.
2. **Teleport**: role + agen database + bot — Langkah B.
3. **Ansible**: buka komentar `teleport_local_port` di `inventory/hosts.yaml` — Langkah C.

> Port 3307 (MySQL 5.1) tidak mendukung TLS → selalu koneksi langsung.
> File `*.example` berisi placeholder; isi token/CA pin saat deploy, jangan commit nilai asli.
