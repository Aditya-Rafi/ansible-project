#!/usr/bin/env bash
#
# teleport-backup-ready.sh
# ------------------------------------------------------------------------------
# Otomatisasi token operasional Teleport untuk Semaphore/Ansible.
#
# Masalah yang diselesaikan:
#   Sertifikat SSH bot Teleport hanya berlaku ~1 jam dan diperbarui sidecar
#   `semaphore-tbot` tiap 20 menit. Bila stack mati lebih lama dari masa berlaku
#   identitas bot, bot "terkunci" (tidak bisa renew sendiri) dan backup gagal:
#       UNREACHABLE ... opening proxy stream ... tls: expired certificate
#
# Skrip ini IDEMPOTEN:
#   - Jika koneksi Teleport sudah sehat  -> tidak melakukan apa-apa (hemat token).
#   - Jika kedaluwarsa/terkunci          -> terbitkan token baru, perbarui config
#                                           sidecar, bersihkan identitas lama,
#                                           restart sidecar, tunggu cert segar.
#   - Lalu verifikasi koneksi (ssh + ansible ping).
#
# Pemakaian:
#   ./teleport-backup-ready.sh            # pastikan koneksi siap (refresh bila perlu)
#   ./teleport-backup-ready.sh --force    # paksa terbitkan token baru
#   ./teleport-backup-ready.sh backup     # siapkan koneksi + jalankan playbook backup
#   ./teleport-backup-ready.sh verify     # siapkan koneksi + jalankan playbook verify
#   ./teleport-backup-ready.sh restore    # siapkan koneksi + jalankan playbook restore
#   ./teleport-backup-ready.sh --help
#
# Catatan: backup/verify/restore tetap bisa dijalankan lewat dashboard Semaphore;
# cukup jalankan skrip ini tanpa argumen dulu agar koneksinya pasti sehat.
# ------------------------------------------------------------------------------
set -euo pipefail

# ----------------------------- Konfigurasi ------------------------------------
# Lokasi file (relatif terhadap lokasi skrip)
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Nilai spesifik lingkungan dibaca dari .env (gitignored; contoh di .env.example)
# agar nama cluster/node asli tidak ikut ter-commit ke repo publik.
# shellcheck disable=SC1091
[ -f "${REPO_ROOT}/.env" ] && . "${REPO_ROOT}/.env"

BOT_NAME="${TELEPORT_BOT_NAME:-semaphore-bot}"
TBOT_CONTAINER="semaphore-tbot"
SEMAPHORE_CONTAINER="semaphore"
TELEPORT_VOLUME=""                      # auto-deteksi bila kosong
CLUSTER="${TELEPORT_CLUSTER:?belum di-set — salin .env.example ke .env lalu isi}"
SSH_HOST="${TELEPORT_NODE:?belum di-set di .env}.${CLUSTER}"   # = ansible_host target
SSH_USER="${ANSIBLE_TARGET_USER:-root}"                        # = ansible_user
INVENTORY_TARGET="${ANSIBLE_TARGET_HOST:-dev-db}"              # nama host di inventory
WAIT_TIMEOUT=40                         # detik menunggu cert segar setelah restart
TBOT_CFG="${REPO_ROOT}/teleport/tbot-semaphore.yaml"
TBOT_CFG_EXAMPLE="${REPO_ROOT}/teleport/tbot-semaphore.yaml.example"
ENV_FILE="${REPO_ROOT}/.env.semaphore"

# Peta perintah -> playbook
PLAYBOOK_backup="playbook/backups/backup_mysql.yml"
PLAYBOOK_verify="playbook/backups/verify_backup.yml"
PLAYBOOK_restore="playbook/backups/restore_mysql.yml"

# ----------------------------- Util tampilan ----------------------------------
if [ -t 1 ]; then
  C_OK=$'\033[32m'; C_WARN=$'\033[33m'; C_ERR=$'\033[31m'; C_DIM=$'\033[2m'; C_RST=$'\033[0m'
else
  C_OK=""; C_WARN=""; C_ERR=""; C_DIM=""; C_RST=""
fi
log()  { printf '%s[*]%s %s\n' "$C_DIM" "$C_RST" "$*"; }
ok()   { printf '%s[OK]%s %s\n' "$C_OK" "$C_RST" "$*"; }
warn() { printf '%s[!]%s %s\n' "$C_WARN" "$C_RST" "$*"; }
die()  { printf '%s[X]%s %s\n' "$C_ERR" "$C_RST" "$*" >&2; exit 1; }

# sudo hanya bila bukan root
SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"

# ----------------------------- Prasyarat --------------------------------------
check_prereqs() {
  command -v docker >/dev/null 2>&1 || die "docker tidak ditemukan."
  command -v tctl   >/dev/null 2>&1 || die "tctl (Teleport) tidak ditemukan."
  docker inspect "$SEMAPHORE_CONTAINER" >/dev/null 2>&1 \
    || die "Container '$SEMAPHORE_CONTAINER' tidak ada. Jalankan: docker compose up -d"
  docker inspect "$TBOT_CONTAINER" >/dev/null 2>&1 \
    || die "Container '$TBOT_CONTAINER' tidak ada. Jalankan: docker compose up -d"
}

# Deteksi nama volume teleport_id bila belum di-set
resolve_volume() {
  [ -n "$TELEPORT_VOLUME" ] && return 0
  TELEPORT_VOLUME="$(docker inspect -f \
    '{{range .Mounts}}{{if eq .Destination "/teleport-id"}}{{.Name}}{{end}}{{end}}' \
    "$TBOT_CONTAINER" 2>/dev/null || true)"
  [ -z "$TELEPORT_VOLUME" ] && TELEPORT_VOLUME="$(docker volume ls --format '{{.Name}}' | grep -E 'teleport_id$' | head -1 || true)"
  if [ -z "$TELEPORT_VOLUME" ]; then die "Tidak bisa menentukan volume teleport_id."; fi
  return 0
}

# Resolusi biner ansible di dalam container (tahan terhadap perubahan versi)
ansible_dir() {
  docker exec "$SEMAPHORE_CONTAINER" sh -c \
    'ls -d /opt/semaphore/apps/ansible/*/venv/bin 2>/dev/null | head -1'
}

# ----------------------------- Cek koneksi ------------------------------------
# Tes persis jalur yang dipakai Ansible: SSH via ProxyCommand Teleport.
connectivity_ok() {
  docker exec -w /home/semaphore/ansible "$SEMAPHORE_CONTAINER" \
    ssh -F teleport-docker.cfg \
        -o BatchMode=yes \
        -o StrictHostKeyChecking=accept-new \
        -o ConnectTimeout=15 \
        "${SSH_USER}@${SSH_HOST}" true >/dev/null 2>&1
}

# Tampilkan masa berlaku cert (informasi saja; butuh ssh-keygen di host)
print_cert_validity() {
  command -v ssh-keygen >/dev/null 2>&1 || return 0
  local tmp; tmp="$(mktemp)"
  if docker exec "$TBOT_CONTAINER" sh -c 'cat /teleport-id/machine-id/key-cert.pub' >"$tmp" 2>/dev/null && [ -s "$tmp" ]; then
    local line; line="$(ssh-keygen -L -f "$tmp" 2>/dev/null | grep -i 'Valid:' | sed 's/^[[:space:]]*//')"
    [ -n "$line" ] && log "Sertifikat: $line"
  fi
  rm -f "$tmp"
}

# ----------------------------- Pemulihan token --------------------------------
refresh_token() {
  warn "Identitas bot kedaluwarsa/terkunci. Memulai pemulihan otomatis..."

  # 1) Pastikan file config sidecar ada (salin dari .example bila perlu)
  if [ ! -f "$TBOT_CFG" ]; then
    [ -f "$TBOT_CFG_EXAMPLE" ] || die "Config $TBOT_CFG dan contohnya tidak ada."
    cp "$TBOT_CFG_EXAMPLE" "$TBOT_CFG"
    warn "Membuat $TBOT_CFG dari contoh."
  fi

  # 2) Terbitkan token join baru untuk bot yang sudah ada
  log "Menerbitkan token join baru untuk bot '$BOT_NAME'..."
  local out token
  out="$($SUDO tctl bots instances add "$BOT_NAME" 2>&1)" \
    || die "Gagal menerbitkan token:\n$out"
  token="$(printf '%s\n' "$out" | awk -F': ' '/[Bb]ot token:/{print $2; exit}' | tr -d '[:space:]')"
  [ -z "$token" ] && token="$(printf '%s\n' "$out" | grep -oE '[a-f0-9]{32}' | head -1)"
  [ -z "$token" ] && die "Token tidak terbaca dari output tctl:\n$out"
  ok "Token baru diterbitkan."

  # 3) Tulis token ke config sidecar
  sed -i -E "s|^([[:space:]]*token:[[:space:]]*).*|\1\"${token}\"|" "$TBOT_CFG"
  log "Token diperbarui di $TBOT_CFG"

  # 4) Bersihkan identitas mati di volume bersama (paksa join bersih)
  resolve_volume
  log "Membersihkan identitas lama di volume '$TELEPORT_VOLUME'..."
  docker run --rm -v "${TELEPORT_VOLUME}:/t" alpine:3.21 \
    sh -c 'rm -rf /t/bot-storage/* /t/machine-id/* /t/machine-id/.write-test 2>/dev/null || true'

  # 5) Restart sidecar agar baca token baru & join ulang
  log "Restart sidecar '$TBOT_CONTAINER'..."
  docker restart "$TBOT_CONTAINER" >/dev/null

  # 6) Tunggu koneksi sehat
  log "Menunggu sertifikat segar (maks ${WAIT_TIMEOUT}s)..."
  local i
  for ((i=2; i<=WAIT_TIMEOUT; i+=2)); do
    sleep 2
    if connectivity_ok; then ok "Koneksi pulih setelah ~${i}s."; return 0; fi
    printf '%s.' "$C_DIM"; printf '%s' "$C_RST"
  done
  echo
  warn "Log sidecar terakhir:"
  docker logs "$TBOT_CONTAINER" --tail 8 2>&1 | grep -vE 'Stack Trace|github.com|runtime/|golang.org' || true
  die "Koneksi belum pulih setelah ${WAIT_TIMEOUT}s. Periksa server Teleport (systemctl status teleport)."
}

# ----------------------------- Verifikasi -------------------------------------
# Password vault (opsional): hanya dibutuhkan bila group_vars/all/vault.yml
# benar-benar dienkripsi ansible-vault. Bila vault.yml plaintext, biarkan kosong.
vault_password() {
  [ -f "$ENV_FILE" ] || return 0
  grep -E '^ANSIBLE_VAULT_PASSWORD=' "$ENV_FILE" | head -1 | cut -d= -f2- \
    | sed -e 's/^["'\'']//' -e 's/["'\'']$//'
}

# ansible ping (verifikasi lapisan Ansible)
ansible_ping() {
  local vp adir rc=0
  vp="$(vault_password)"
  adir="$(ansible_dir)"; [ -z "$adir" ] && { warn "Biner ansible tak ditemukan di container; lewati ping."; return 0; }

  if [ -n "$vp" ]; then
    docker exec "$SEMAPHORE_CONTAINER" sh -c "umask 077; printf '%s' '$vp' > /tmp/.vp"
    docker exec -w /home/semaphore/ansible \
      -e "ANSIBLE_SSH_ARGS=-F /home/semaphore/ansible/teleport-docker.cfg" \
      "$SEMAPHORE_CONTAINER" \
      "${adir}/ansible" "$INVENTORY_TARGET" -m ping --vault-password-file /tmp/.vp 2>&1 | tail -8 || rc=$?
    docker exec "$SEMAPHORE_CONTAINER" rm -f /tmp/.vp || true
  else
    docker exec -w /home/semaphore/ansible \
      -e "ANSIBLE_SSH_ARGS=-F /home/semaphore/ansible/teleport-docker.cfg" \
      "$SEMAPHORE_CONTAINER" \
      "${adir}/ansible" "$INVENTORY_TARGET" -m ping 2>&1 | tail -8 || rc=$?
  fi
  return $rc
}

# ----------------------------- Jalankan playbook ------------------------------
run_playbook() {
  local action="$1" pb_var="PLAYBOOK_$1" pb adir vp
  pb="${!pb_var:-}"
  [ -z "$pb" ] && die "Aksi tidak dikenal: $action"

  # Konfirmasi untuk restore (berpotensi menimpa data)
  if [ "$action" = "restore" ] && [ -t 0 ]; then
    read -r -p "RESTORE dapat menimpa database. Lanjutkan? ketik 'yes': " ans
    [ "$ans" = "yes" ] || die "Dibatalkan."
  fi

  vp="$(vault_password)"
  adir="$(ansible_dir)"; [ -z "$adir" ] && die "Biner ansible-playbook tak ditemukan di container."

  log "Menjalankan playbook: $pb"
  local rc=0
  if [ -n "$vp" ]; then
    docker exec "$SEMAPHORE_CONTAINER" sh -c "umask 077; printf '%s' '$vp' > /tmp/.vp"
    docker exec -w /home/semaphore/ansible \
      -e "ANSIBLE_SSH_ARGS=-F /home/semaphore/ansible/teleport-docker.cfg" \
      "$SEMAPHORE_CONTAINER" \
      "${adir}/ansible-playbook" "$pb" --vault-password-file /tmp/.vp || rc=$?
    docker exec "$SEMAPHORE_CONTAINER" rm -f /tmp/.vp || true
  else
    docker exec -w /home/semaphore/ansible \
      -e "ANSIBLE_SSH_ARGS=-F /home/semaphore/ansible/teleport-docker.cfg" \
      "$SEMAPHORE_CONTAINER" \
      "${adir}/ansible-playbook" "$pb" || rc=$?
  fi
  [ "$rc" -eq 0 ] && ok "Playbook '$action' selesai." || die "Playbook '$action' gagal (exit $rc)."
}

# ----------------------------- Main -------------------------------------------
usage() { sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'; }

main() {
  local force=0 action=""
  for arg in "$@"; do
    case "$arg" in
      --force) force=1 ;;
      -h|--help) usage; exit 0 ;;
      backup|verify|restore) action="$arg" ;;
      *) die "Argumen tidak dikenal: $arg (pakai --help)";;
    esac
  done

  check_prereqs
  log "Memeriksa koneksi Teleport ke ${SSH_HOST}..."

  if [ "$force" -eq 1 ]; then
    warn "--force: memaksa penerbitan token baru."
    refresh_token
  elif connectivity_ok; then
    ok "Koneksi Teleport sudah sehat. Tidak perlu token baru."
    print_cert_validity
  else
    refresh_token
  fi

  # Verifikasi akhir lapisan Ansible
  log "Verifikasi ansible ping ($INVENTORY_TARGET)..."
  if ansible_ping | grep -q 'pong'; then
    ok "ansible ping -> pong. Koneksi SIAP untuk backup."
  else
    warn "ansible ping tidak mengembalikan pong (cek vault/inventory). Jalur SSH Teleport sendiri sudah OK."
  fi

  if [ -n "$action" ]; then
    run_playbook "$action"
  else
    echo
    ok "SIAP. Silakan jalankan backup/verify/restore di dashboard Semaphore,"
    log "atau jalankan langsung: ./teleport-backup-ready.sh backup"
  fi
}

main "$@"
