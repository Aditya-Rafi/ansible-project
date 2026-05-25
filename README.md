# Ansible ADIT

Ansible project untuk provisioning dua node:

- `dev-apps`: aplikasi berjalan langsung di host (non-Docker) dengan Nginx dan Keepalived.
- `dev-db`: MySQL berjalan di Docker container.

> Catatan keamanan: IP dan kredensial asli tidak disimpan di repository. Inventory memakai alamat dokumentasi `192.0.2.0/24` sebagai placeholder. Override nilai asli lewat environment variable, extra vars, atau file inventory private yang tidak di-commit.

## Struktur

```text
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
```

## Keamanan

- Jangan commit password SSH, token, private key, atau IP internal produksi.
- Pakai SSH key atau Ansible Vault untuk secret.
- File private seperti `.env`, `inventory/local.yaml`, dan file vault sudah diabaikan oleh `.gitignore`.
