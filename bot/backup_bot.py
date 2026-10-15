#!/usr/bin/env python3
"""
Bot Telegram "human-in-the-loop" untuk backup MySQL
===================================================

Alur:
  1. Playbook Backup MySQL (Semaphore) mengirim laporan DB yang GAGAL ke
     POST http://backup-bot:8080/event  (hanya terjangkau di jaringan compose).
  2. Bot mengirim pesan Telegram per DB: penyebab gagal + tiga tombol
        🟢 Backup   -> jalankan template "Backup MySQL" di Semaphore untuk DB itu
        ⚪ Tunda    -> ingatkan lagi setelah BOT_SNOOZE_MINUTES
        🔴 Batal    -> tutup, tidak ada aksi
  3. Setelah run Semaphore selesai, bot melapor "sukses" atau "gagal lagi" + log.

Hanya stdlib Python (tanpa pip). Konfigurasi lewat env (.env.semaphore):
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, [TELEGRAM_ALLOWED_USER_IDS]
  SEMAPHORE_URL, SEMAPHORE_ADMIN, SEMAPHORE_ADMIN_PASSWORD, SEMAPHORE_PROJECT
"""

import calendar
import html
import json
import os
import re
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
ALLOWED = {
    x.strip()
    for x in (os.getenv("TELEGRAM_ALLOWED_USER_IDS") or CHAT_ID).split(",")
    if x.strip()
}
TG_BASE = os.getenv("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/")

SEM_URL = os.getenv("SEMAPHORE_URL", "http://semaphore:3000/api").rstrip("/")
SEM_USER = os.getenv("SEMAPHORE_ADMIN", "admin")
SEM_PASS = os.getenv("SEMAPHORE_ADMIN_PASSWORD", "")
SEM_PROJECT = os.getenv("SEMAPHORE_PROJECT", "backup")
SEM_TEMPLATE = os.getenv("BACKUP_TEMPLATE", "Backup MySQL")

SNOOZE_MIN = int(os.getenv("BOT_SNOOZE_MINUTES", "60"))
LISTEN_PORT = int(os.getenv("BOT_LISTEN_PORT", "8080"))
STATE_FILE = os.getenv("STATE_FILE", "/data/state.json")
POLL_SECONDS = int(os.getenv("BOT_TASK_POLL_SECONDS", "10"))
PORTAINER_URL = os.getenv("PORTAINER_URL", "").rstrip("/")
PORTAINER_USER = os.getenv("PORTAINER_USER", "")
PORTAINER_PASS = os.getenv("PORTAINER_PASSWORD", "")
PORTAINER_STACKS = [x.strip() for x in os.getenv("PORTAINER_STACKS", "").split(",") if x.strip()]
CHECK_TIME = os.getenv("BOT_CHECK_TIME", "08:00")  # jam lokal (lihat UTC_OFFSET)
CHECK_DAYS = os.getenv("BOT_CHECK_DAYS", "1-5")  # ISO: 1=Senin ... 7=Minggu
UTC_OFFSET = int(os.getenv("BOT_UTC_OFFSET_HOURS", "7"))  # WIB
STALE_HOURS = int(os.getenv("BOT_STALE_HOURS", "30"))  # tanpa run baru > ini = dianggap tidak jalan
IGNORE_DBS = {
    x.strip().lower()
    for x in os.getenv("BOT_IGNORE_DBS", "information_schema,performance_schema,sys").split(",")
    if x.strip()
}
INVENTORY_FILE = os.getenv("INVENTORY_FILE", "/inventory/hosts.yaml")
MAX_INDIVIDUAL = 15  # pesan per-DB; sisanya cukup lewat tombol "semua"

OPEN = ("pending", "snoozed")
lock = threading.RLock()
state = {"items": {}, "batches": {}, "offset": 0, "history": {}, "last_check": ""}


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# ----------------------------------------------------------------------------- state
def load_state():
    try:
        with open(STATE_FILE) as f:
            state.update(json.load(f))
    except (OSError, ValueError):
        pass


def save_state():
    with lock:
        try:
            os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(state, f)
            os.replace(tmp, STATE_FILE)
        except OSError as e:
            log("[!] gagal simpan state:", e)


# ----------------------------------------------------------------------------- telegram
def tg(method, payload):
    req = urllib.request.Request(
        f"{TG_BASE}/bot{TOKEN}/{method}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=70) as res:
            return json.loads(res.read().decode()).get("result")
    except urllib.error.HTTPError as e:
        log(f"[!] telegram {method} HTTP {e.code}: {e.read().decode()[:200]}")
    except Exception as e:  # noqa: BLE001
        log(f"[!] telegram {method}: {e}")
    return None


def say(text, keyboard=None):
    payload = {"chat_id": CHAT_ID, "text": text[:4000], "parse_mode": "HTML"}
    if keyboard is None:
        return tg("sendMessage", payload)
    # Telegram baru mendukung warna lewat "style"; klien/versi lama menolak
    # atau mengabaikannya -> ulangi tanpa style. Emoji tetap memberi petunjuk warna.
    msg = tg("sendMessage", dict(payload, reply_markup={"inline_keyboard": keyboard}))
    if msg is None:
        plain = [[{k: v for k, v in b.items() if k != "style"} for b in row] for row in keyboard]
        msg = tg("sendMessage", dict(payload, reply_markup={"inline_keyboard": plain}))
    return msg


def edit(item, status_line):
    """Ganti isi pesan item: teks dasar + baris status, tombol dihapus."""
    if not item.get("msg_id"):
        return
    tg(
        "editMessageText",
        {
            "chat_id": CHAT_ID,
            "message_id": item["msg_id"],
            "text": (item["text"] + "\n\n" + status_line)[:4000],
            "parse_mode": "HTML",
            "reply_markup": {"inline_keyboard": []},
        },
    )


def buttons(prefix, ident, label="", can_backup=True):
    row = []
    if can_backup:
        row.append({"text": f"🟢 Backup{label}", "callback_data": f"go{prefix}:{ident}", "style": "success"})
    row.append({"text": "⚪ Tunda", "callback_data": f"sn{prefix}:{ident}"})
    row.append({"text": "🔴 Batal", "callback_data": f"no{prefix}:{ident}", "style": "danger"})
    return [row]


def item_text(it, prefix=""):
    return (
        f"{prefix}⚠️ <b>Backup GAGAL</b>\n"
        f"Server : <code>{html.escape(it['host'])}</code>\n"
        f"DB     : <code>{html.escape(it['db'])}</code> (port {html.escape(str(it['port']))})\n"
        + (f"MySQL  : {html.escape(it['version'])}\n" if it.get("version") else "")
        + (f"Sumber : {html.escape(it['note'])}\n" if it.get("note") else "")
        + (f"🔁 Gagal {it['streak']} hari berturut-turut\n" if it.get("streak", 1) > 1 else "")
        + (
            ""
            if it.get("can_backup", True)
            else "ℹ️ Tombol Backup tidak tersedia: server ini belum ada di inventory Semaphore.\n"
        )
        + f"\n<b>Penyebab:</b>\n<pre>{html.escape(it['error'][:1800] or '(tidak ada pesan)')}</pre>"
    )


def post_item(it, prefix=""):
    it["text"] = item_text(it, prefix)
    msg = say(it["text"], buttons("", it["id"], can_backup=it.get("can_backup", True)))
    it["msg_id"] = (msg or {}).get("message_id")


# ----------------------------------------------------------------------------- semaphore
class Semaphore:
    def __init__(self):
        self.cookie = None
        self.ids = None

    def login(self):
        req = urllib.request.Request(
            f"{SEM_URL}/auth/login",
            data=json.dumps({"auth": SEM_USER, "password": SEM_PASS}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=20) as res:
            self.cookie = (res.info().get("Set-Cookie") or "").split(";")[0]
        if not self.cookie:
            raise RuntimeError("login Semaphore tidak mengembalikan cookie")

    def call(self, path, method="GET", data=None, retry=True):
        if not self.cookie:
            self.login()
        req = urllib.request.Request(
            f"{SEM_URL}{path}",
            data=json.dumps(data).encode() if data is not None else None,
            headers={"Content-Type": "application/json", "Cookie": self.cookie},
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as res:
                raw = res.read().decode()
                return json.loads(raw) if raw else True
        except urllib.error.HTTPError as e:
            if e.code == 401 and retry:
                self.cookie = None
                return self.call(path, method, data, retry=False)
            raise

    def resolve(self):
        if self.ids:
            return self.ids
        proj = next((p for p in self.call("/projects") if p["name"] == SEM_PROJECT), None)
        if not proj:
            raise RuntimeError(f"project Semaphore '{SEM_PROJECT}' tidak ditemukan")
        tpl = next(
            (t for t in self.call(f"/project/{proj['id']}/templates") if t["name"] == SEM_TEMPLATE),
            None,
        )
        if not tpl:
            raise RuntimeError(f"template '{SEM_TEMPLATE}' tidak ditemukan")
        self.ids = (proj["id"], tpl["id"])
        return self.ids

    def start(self, host, port, dbs):
        pid, tid = self.resolve()
        extra = {"backup_databases": ",".join(dbs), "backup_ports": str(port)}
        task = self.call(
            f"/project/{pid}/tasks",
            "POST",
            {"template_id": tid, "limit": host, "environment": json.dumps(extra)},
        )
        return pid, task["id"]

    def status(self, pid, task_id):
        return self.call(f"/project/{pid}/tasks/{task_id}")["status"]

    def log_tail(self, pid, task_id, lines=25):
        out = self.call(f"/project/{pid}/tasks/{task_id}/output")
        text = "\n".join(o.get("output", "") for o in out)
        text = re.sub(r"\x1b\[[0-9;]*m", "", text)
        return "\n".join([l for l in text.splitlines() if l.strip()][-lines:])


sem = Semaphore()


# ----------------------------------------------------------------------------- aksi
def key_of(it):
    return (it.get("sem_host") or it["host"], str(it["port"]), it["db"])


def ingest_event(ev):
    host = ev.get("host", "?")
    failed = ev.get("failed") or []
    new = []
    with lock:
        for f in failed:
            it = {
                "id": uuid.uuid4().hex[:10],
                "host": host,
                "db": str(f.get("db", "?")),
                "port": f.get("port", ""),
                "version": f.get("version", ""),
                "error": str(f.get("error", "")),
                "note": f.get("note", ""),
                "sem_host": f.get("sem_host", ""),
                "can_backup": f.get("can_backup", True),
                "streak": f.get("streak", 1),
                "status": "pending",
                "created": time.time(),
            }
            for old in state["items"].values():
                if key_of(old) != key_of(it):
                    continue
                if old["status"] in OPEN:  # laporan lama tergantikan
                    old["status"] = "superseded"
                    edit(old, "↪️ Digantikan laporan yang lebih baru.")
                elif old["status"] == "running":
                    old["superseded_by"] = it["id"]
            state["items"][it["id"]] = it
            new.append(it)
        for it in new[:MAX_INDIVIDUAL]:
            post_item(it)
        if len(new) > 1:
            bid = uuid.uuid4().hex[:10]
            state["batches"][bid] = [i["id"] for i in new]
            names = "\n".join(f"• {html.escape(i['db'])}" for i in new[:40])
            say(
                f"📋 <b>{len(new)} DB gagal</b> di <code>{html.escape(host)}</code>:\n{names}\n\n"
                "Aksi untuk semuanya:",
                buttons("a", bid, " semua", can_backup=any(i.get("can_backup", True) for i in new)),
            )
        save_state()
    log(f"event: {host} {len(new)} DB gagal")


def start_backup(item_ids, who):
    """Kelompokkan per (host, port) -> satu task Semaphore per kelompok."""
    groups = {}
    with lock:
        for iid in item_ids:
            it = state["items"].get(iid)
            if it and it["status"] in OPEN:
                it["status"] = "running"
                edit(it, f"⏳ Backup dijalankan oleh {html.escape(who)}…")
                groups.setdefault((it.get("sem_host") or it["host"], str(it["port"])), []).append(it)
        save_state()
    for (host, port), items in groups.items():
        threading.Thread(target=run_group, args=(host, port, items), daemon=True).start()


def run_group(host, port, items):
    dbs = [i["db"] for i in items]
    try:
        pid, task_id = sem.start(host, port, dbs)
    except Exception as e:  # noqa: BLE001
        say(f"❌ Gagal memulai backup di Semaphore: <code>{html.escape(str(e))}</code>")
        with lock:
            for it in items:
                it["status"] = "pending"
                it["error"] = f"Gagal memulai run Semaphore: {e}"
                post_item(it)
            save_state()
        return
    say(f"▶️ Backup dimulai: <code>{html.escape(', '.join(dbs))}</code> @ "
        f"<code>{html.escape(host)}</code> — run #{task_id}")
    status = "running"
    deadline = time.time() + 6 * 3600
    while time.time() < deadline:
        time.sleep(POLL_SECONDS)
        try:
            status = sem.status(pid, task_id)
        except Exception as e:  # noqa: BLE001
            log("[!] poll status:", e)
            continue
        if status in ("success", "error", "stopped"):
            break
    with lock:
        if status == "success":
            for it in items:
                it["status"] = "done"
                edit(it, "✅ Berhasil di-backup ulang.")
            names = "\n".join(f"• {html.escape(d)}" for d in dbs)
            say(f"✅ <b>Backup sukses</b> — <code>{html.escape(host)}</code> "
                f"(run #{task_id})\n{names}")
        else:
            try:
                tail = sem.log_tail(pid, task_id)
            except Exception as e:  # noqa: BLE001
                tail = f"(log tidak terbaca: {e})"
            for it in items:
                if it.get("superseded_by"):  # playbook sudah melapor ulang dgn tombol
                    it["status"] = "failed_again"
                    edit(it, f"❌ Gagal lagi (run #{task_id}) — lihat laporan baru.")
                else:  # gagal sebelum playbook sempat melapor (mis. host unreachable)
                    it["status"] = "pending"
                    it["error"] = f"Run #{task_id} berstatus {status}:\n{tail}"
                    post_item(it, "🔁 ")
        save_state()


def snooze(item_ids, who):
    due = time.time() + SNOOZE_MIN * 60
    with lock:
        for iid in item_ids:
            it = state["items"].get(iid)
            if it and it["status"] in OPEN:
                it["status"] = "snoozed"
                it["due"] = due
                edit(it, f"⏸ Ditunda {SNOOZE_MIN} menit oleh {html.escape(who)}.")
        save_state()


def cancel(item_ids, who):
    with lock:
        for iid in item_ids:
            it = state["items"].get(iid)
            if it and it["status"] in OPEN:
                it["status"] = "cancelled"
                edit(it, f"🔴 Dibatalkan oleh {html.escape(who)} — tidak ada backup ulang.")
        save_state()


def reminder_loop():
    while True:
        time.sleep(30)
        with lock:
            for it in state["items"].values():
                if it["status"] == "snoozed" and it.get("due", 0) <= time.time():
                    it["status"] = "pending"
                    post_item(it, "🔔 Pengingat\n")
                    save_state()


# ----------------------------------------------------------------------------- polling
def handle_callback(cb):
    uid = str(cb["from"]["id"])
    who = cb["from"].get("first_name") or uid
    if uid not in ALLOWED:
        tg("answerCallbackQuery", {"callback_query_id": cb["id"], "text": "Tidak diizinkan.", "show_alert": True})
        log(f"[!] callback ditolak dari user {uid}")
        return
    action, _, ident = (cb.get("data") or "").partition(":")
    with lock:
        if action.endswith("a") and len(action) == 3:  # goa / sna / noa -> batch
            ids = [i for i in state["batches"].get(ident, []) if state["items"].get(i, {}).get("status") in OPEN]
        else:
            it = state["items"].get(ident)
            ids = [ident] if it and it["status"] in OPEN else []
    if not ids:
        tg("answerCallbackQuery", {"callback_query_id": cb["id"], "text": "Sudah diproses sebelumnya."})
        return
    tg("answerCallbackQuery", {"callback_query_id": cb["id"], "text": "Diterima."})
    verb = action[:2]
    {"go": start_backup, "sn": snooze, "no": cancel}.get(verb, lambda *_: None)(ids, who)


def poll_loop():
    while True:
        updates = tg(
            "getUpdates",
            {"offset": state["offset"], "timeout": 30, "allowed_updates": ["callback_query", "message"]},
        )
        if updates is None:
            time.sleep(5)
            continue
        for u in updates:
            state["offset"] = u["update_id"] + 1
            msg = u.get("message") or {}
            if (msg.get("text") or "").startswith("/cek") and str(msg.get("from", {}).get("id")) in ALLOWED:
                threading.Thread(target=run_check, args=(True,), daemon=True).start()
            if "callback_query" in u:
                try:
                    handle_callback(u["callback_query"])
                except Exception as e:  # noqa: BLE001
                    log("[!] callback error:", repr(e))
        if updates:
            save_state()


# ----------------------------------------------------------------------------- portainer
class Portainer:
    """Klien read-only Portainer: daftar container stack backup + log-nya."""

    def __init__(self):
        self.jwt = None

    def login(self):
        req = urllib.request.Request(
            f"{PORTAINER_URL}/api/auth",
            data=json.dumps({"username": PORTAINER_USER, "password": PORTAINER_PASS}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=20) as res:
            self.jwt = json.loads(res.read().decode())["jwt"]

    def get(self, path, retry=True):
        if not self.jwt:
            self.login()
        req = urllib.request.Request(
            f"{PORTAINER_URL}{path}", headers={"Authorization": f"Bearer {self.jwt}"}
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as res:
                return res.read()
        except urllib.error.HTTPError as e:
            if e.code == 401 and retry:
                self.jwt = None
                return self.get(path, retry=False)
            raise

    def json(self, path):
        return json.loads(self.get(path).decode())

    def logs(self, endpoint, cid, since):
        raw = self.get(
            f"/api/endpoints/{endpoint}/docker/containers/{cid}/logs"
            f"?stdout=1&stderr=1&since={since}&timestamps=1"
        )
        out, i = [], 0  # stream Docker ber-header 8 byte bila container tanpa TTY
        while i + 8 <= len(raw) and raw[i] in (0, 1, 2) and raw[i + 1:i + 4] == b"\0\0\0":
            n = struct.unpack(">I", raw[i + 4:i + 8])[0]
            out.append(raw[i + 8:i + 8 + n].decode("utf8", "replace"))
            i += 8 + n
        return ("".join(out) if out else raw.decode("utf8", "replace")).splitlines()


ERR_RE = re.compile(
    r"(mysqldump|mysql): (?!\[Warning\])(.*(error|denied|lost connection|couldn't|can't connect|gone away|unknown))",
    re.I,
)
START_RE = re.compile(r"Backup Start Time")
END_RE = re.compile(r"Backup End")
DB_RE = re.compile(r"(?:Daily|Weekly|Monthly) Backup of Database \( (.+?) \)")


def ts_of(line):
    try:
        return calendar.timegm(time.strptime(line[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return None


def inventory_map():
    """(mysql_host, port) -> nama host inventory Ansible; parser longgar untuk hosts.yaml."""
    m, cur, ip = {}, None, None
    try:
        lines = open(INVENTORY_FILE).read().splitlines()
    except OSError:
        return m
    for line in lines:
        if line.strip().startswith("#"):
            continue
        h = re.match(r"^\s+([\w.-]+):\s*$", line)
        if h and h.group(1) not in ("vars", "hosts", "children", "mysql_instances"):
            cur, ip = h.group(1), None
        mh = re.match(r"^\s+mysql_host:\s*(\S+)", line)
        if mh and cur:
            ip = mh.group(1).strip("\"'")
        mp = re.match(r"^\s+-\s+port:\s*(\d+)", line)
        if mp and cur and ip:
            m[(ip, mp.group(1))] = cur
    return m


def parse_days(spec):
    days = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            lo, hi = part.split("-")
            days.update(range(int(lo), int(hi) + 1))
        elif part:
            days.add(int(part))
    return days


def analyse(lines, lookback_ts):
    """-> (last_start_ts, last_run_ended, [(db, error)])

    Error dikumpulkan dari SEMUA run sejak lookback_ts (bukan hanya run
    terakhir), supaya kegagalan di akhir pekan tetap terlaporkan hari kerja.
    """
    starts = [i for i, l in enumerate(lines) if START_RE.search(l)]
    if not starts:
        return None, False, []
    bounds = starts + [len(lines)]
    errs = {}
    last_start, last_ended = None, False
    for n, i in enumerate(starts):
        run = lines[i:bounds[n + 1]]
        start = ts_of(run[0])
        last_start, last_ended = start, any(END_RE.search(l) for l in run)
        if start is None or start < lookback_ts:
            continue
        stamp = time.strftime("%a %d %b", time.gmtime(start + UTC_OFFSET * 3600))
        db = None
        for l in run:
            d = DB_RE.search(l)
            if d:
                db = d.group(1)
            e = ERR_RE.search(l)
            if e and db and db.lower() not in IGNORE_DBS:
                msg = re.sub(r"^\S+\s+\S+\s+\S+\s+\d+:\s*", "", l)[:380]
                errs.setdefault(db, f"[{stamp}] {msg}")
    return last_start, last_ended, list(errs.items())


def check_portainer():
    """Periksa semua container stack backup -> dict container -> laporan masalah."""
    pt = Portainer()
    now = time.time()
    since = int(now) - 4 * 86400
    # cakupan error = sejak cek terakhir (min. 30 jam; Senin otomatis mencakup akhir pekan)
    last = state.get("last_check_ts")
    lookback = max(last - 2 * 3600, since) if last else now - STALE_HOURS * 3600
    stacks = {s["Name"]: s["EndpointId"] for s in pt.json("/api/stacks")}
    inv = inventory_map()
    problems, total = [], 0
    for name in PORTAINER_STACKS:
        ep = stacks.get(name)
        if ep is None:
            problems.append({"container": name, "issues": [("-", f"stack '{name}' tidak ditemukan di Portainer")]})
            continue
        flt = urllib.parse.quote(json.dumps({"label": [f"com.docker.compose.project={name}"]}))
        for c in sorted(pt.json(f"/api/endpoints/{ep}/docker/containers/json?all=1&filters={flt}"), key=lambda c: c["Names"][0]):
            total += 1
            cname = c["Names"][0].lstrip("/")
            issues = []
            if c["State"] != "running":
                issues.append(("(container)", f"Container berstatus '{c['State']}' ({c['Status']})"))
            try:
                start, ended, errs = analyse(pt.logs(ep, c["Id"], since), lookback)
            except Exception as e:  # noqa: BLE001
                issues.append(("(log)", f"Log tidak terbaca: {e}"))
                start, ended, errs = now, True, []
            if start is None or now - start > STALE_HOURS * 3600:
                last = time.strftime("%d %b %H:%M", time.gmtime(start + UTC_OFFSET * 3600)) if start else "tidak ada dalam 4 hari terakhir"
                issues.append(("(jadwal)", f"Backup TIDAK berjalan. Run terakhir: {last}"))
            elif not ended and now - start > 6 * 3600:
                issues.append(("(run)", "Run dimulai tapi tidak pernah selesai (kemungkinan terputus)"))
            issues += errs
            if issues:
                env = {}
                try:
                    for kv in pt.json(f"/api/endpoints/{ep}/docker/containers/{c['Id']}/json")["Config"]["Env"]:
                        k, _, v = kv.partition("=")
                        if k in ("DBHOST", "DBPORT"):
                            env[k] = v
                except Exception:  # noqa: BLE001
                    pass
                problems.append(
                    {
                        "container": cname,
                        "stack": name,
                        "dbhost": env.get("DBHOST", ""),
                        "port": env.get("DBPORT", "3306"),
                        "sem_host": inv.get((env.get("DBHOST", ""), env.get("DBPORT", "3306")), ""),
                        "issues": issues,
                    }
                )
    return total, problems


def run_check(manual=False):
    if not (PORTAINER_URL and PORTAINER_USER and PORTAINER_STACKS):
        say("⚠️ Pengecekan Portainer belum dikonfigurasi (PORTAINER_URL/USER/PASSWORD/STACKS).")
        return
    log("cek Portainer dimulai")
    try:
        total, problems = check_portainer()
    except Exception as e:  # noqa: BLE001
        say(f"🚫 <b>Cek harian GAGAL</b>: Portainer tidak bisa diakses.\n<pre>{html.escape(str(e)[:500])}</pre>")
        return
    today = time.strftime("%Y-%m-%d", time.gmtime(time.time() + UTC_OFFSET * 3600))
    with lock:
        state["last_check"] = today
        state["last_check_ts"] = time.time()
        hist = state.setdefault("history", {})
        seen = set()
        events = []
        for p in problems:
            failed = []
            for db, err in p["issues"]:
                key = f"{p['container']}|{db}"
                seen.add(key)
                prev = hist.get(key, {})
                streak = prev.get("streak", 0) + 1 if prev.get("last") != today else prev.get("streak", 1)
                hist[key] = {"last": today, "streak": streak}
                real_db = not db.startswith("(")
                failed.append(
                    {
                        "db": db,
                        "port": p.get("port", ""),
                        "error": err,
                        "streak": streak,
                        "note": f"Portainer › {p.get('stack', '')} › {p['container']}",
                        "sem_host": p.get("sem_host", ""),
                        "can_backup": bool(p.get("sem_host")) and real_db,
                    }
                )
            events.append({"host": p["container"], "failed": failed})
        for key in [k for k in hist if k not in seen]:
            del hist[key]  # masalah selesai -> streak reset
        save_state()
    bad = sum(len(e["failed"]) for e in events)
    if not events:
        say(f"✅ <b>Cek harian {today}</b>\n{total} container backup diperiksa — tidak ada masalah.")
        return
    say(f"📊 <b>Cek harian {today}</b>\n{total} container diperiksa — "
        f"<b>{len(events)} container bermasalah</b> ({bad} temuan).")
    for ev in events:
        ingest_event(ev)


def check_scheduler():
    hh, mm = (int(x) for x in CHECK_TIME.split(":"))
    days = parse_days(CHECK_DAYS)
    while True:
        time.sleep(30)
        local = time.gmtime(time.time() + UTC_OFFSET * 3600)
        today = time.strftime("%Y-%m-%d", local)
        if (
            local.tm_wday + 1 in days
            and state.get("last_check") != today
            and (local.tm_hour, local.tm_min) >= (hh, mm)
        ):
            try:
                run_check()
            except Exception as e:  # noqa: BLE001
                log("[!] run_check:", repr(e))
                state["last_check"] = today  # jangan mengulang tiap 30 detik


# ----------------------------------------------------------------------------- http
class Handler(BaseHTTPRequestHandler):
    def _reply(self, code, body=b"ok"):
        self.send_response(code)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        self._reply(200 if self.path == "/health" else 404)

    def do_POST(self):  # noqa: N802
        if self.path == "/check":  # picu cek Portainer sekarang (jaringan internal)
            self._reply(200)
            threading.Thread(target=run_check, args=(True,), daemon=True).start()
            return
        if self.path != "/event":
            return self._reply(404)
        try:
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            ev = json.loads(body.decode())
        except ValueError:
            return self._reply(400, b"bad json")
        self._reply(200)
        threading.Thread(target=ingest_event, args=(ev,), daemon=True).start()

    def log_message(self, *_):
        pass


def main():
    if not TOKEN or not CHAT_ID:
        raise SystemExit("TELEGRAM_BOT_TOKEN dan TELEGRAM_CHAT_ID wajib diisi (.env.semaphore).")
    load_state()
    threading.Thread(target=reminder_loop, daemon=True).start()
    if PORTAINER_URL:
        threading.Thread(target=check_scheduler, daemon=True).start()
    threading.Thread(target=lambda: ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Handler).serve_forever(), daemon=True).start()
    log(f"bot siap: event di :{LISTEN_PORT}, chat {CHAT_ID}, snooze {SNOOZE_MIN} menit, "
        f"cek Portainer {CHECK_TIME} hari {CHECK_DAYS} ({len(PORTAINER_STACKS)} stack)")
    poll_loop()


if __name__ == "__main__":
    main()
