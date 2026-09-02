# Ansible SSH via Teleport (untuk Semaphore background)

Mengarahkan koneksi **SSH Ansible → host target** melalui Teleport (bukan plain
SSH port 22). Auth memakai sertifikat pendek dari **Machine ID (tbot)** — tanpa
password, cocok untuk Semaphore yang jalan non-interaktif. **Tidak menyentuh
konfigurasi MySQL** sama sekali.

```
Semaphore → Ansible → ssh -F ssh_config → ProxyCommand (tbot) → Teleport proxy
         → node SSH 'developer' (cluster example-cluster) → login OS 'developer' → sudo
```

## Komponen (sudah aktif di host ini)

| Komponen | Detail |
|----------|--------|
| Role | `ansible-ssh` (logins: `developer`, akses semua node) |
| Bot | `ansible-bot` |
| tbot (Machine ID) | systemd `tbot-ssh.service`, config `~/teleport-ssh/tbot-ssh.yaml` |
| Output identity | `~/teleport-ssh/machine-id/` → `ssh_config`, `known_hosts`, `key`, `key-cert.pub` |

tbot memperbarui sertifikat otomatis (renew tiap ~20 menit). systemd memastikan
hidup setelah reboot.

## Perubahan di inventory (hanya blok koneksi)

```yaml
ansible_host: developer.example-cluster          # <node>.<cluster> Teleport
ansible_user: "developer"                    # login OS via Teleport
ansible_connection: ssh
ansible_ssh_common_args: "-F /home/developer/teleport-ssh/machine-id/ssh_config -o StrictHostKeyChecking=accept-new"
ansible_become: true
ansible_become_method: sudo
ansible_become_user: root
ansible_become_password: "{{ vault_ansible_password }}"
```

Dihapus: `ansible_port: 22`, `ansible_password` (auth kini via sertifikat).
Semua variabel MySQL (`mysql_host`, `mysql_username`, `mysql_password`,
`mysql_instances`) **tidak diubah**.

## Menjalankan di Semaphore (background)

> **Penting:** tbot harus berjalan di tempat Semaphore mengeksekusi Ansible,
> dan path `ssh_config` harus terjangkau oleh proses Ansible tersebut.

- **Semaphore di host ini (192.0.2.20):** sudah siap — systemd `tbot-ssh`
  aktif dan path `/home/developer/teleport-ssh/machine-id/ssh_config` tersedia.
- **Semaphore dalam container:** mount direktori `machine-id/` ke container, atau
  jalankan tbot sebagai sidecar; sesuaikan path di `ansible_ssh_common_args`.
- **Semaphore di host lain:** jalankan tbot di host itu (pakai bot/role yang
  sama), arahkan `ansible_ssh_common_args` ke ssh_config lokalnya.

`binary` yang dibutuhkan di runtime: `tbot` (untuk ProxyCommand) + `ssh`.

## Uji cepat

```bash
ansible dev-db -m ping                      # → pong (via Teleport, tanpa password)
ansible dev-db -m shell -a whoami --become  # → root
```

Audit: setiap sesi SSH muncul di Teleport (Web UI → Audit Log / Session Recordings).

## Operasional

```bash
systemctl status tbot-ssh         # status Machine ID
journalctl -u tbot-ssh -f         # log tbot
sudo systemctl restart tbot-ssh   # restart bila perlu
```

Rollback ke SSH:22 langsung: kembalikan blok koneksi inventory ke `ansible_host:
192.0.2.20`, `ansible_port: 22`, `ansible_password: "{{ vault_ansible_password }}"`,
hapus `ansible_ssh_common_args` & `ansible_connection`.

## Catatan keamanan

- `--insecure` pada tbot dipakai karena proxy Teleport memakai cert self-signed.
  Untuk produksi penuh, pasang cert valid di proxy lalu hapus `--insecure`.
- Role `ansible-ssh` memberi akses login `developer` ke semua node; persempit
  `node_labels` bila perlu least-privilege.
