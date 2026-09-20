#!/usr/bin/env python3
"""
OLM Telegram Bot + Web Admin
- Toàn bộ cấu hình lưu trong config.json (nhập qua Web Admin)
- KHÔNG cần Environment Variables
- Anti-sleep: tự ping server mỗi 3 phút
- Bot loop: kiểm tra bài tập mỗi 5 phút
"""

import requests
import json
import time
import os
import hashlib
import threading
import logging
from datetime import datetime
from functools import wraps

from flask import Flask, render_template_string, request, redirect, url_for, session, jsonify
from bs4 import BeautifulSoup

# ══════════════════════════════════════════════════════
#  ⚙️  HẰNG SỐ CỐ ĐỊNH (không cần chỉnh)
# ══════════════════════════════════════════════════════

ADMIN_USERNAME = "Adminn"
ADMIN_PASSWORD = "120510@"
SECRET_KEY     = "olm_secret_2026_!@#$%"

CHECK_INTERVAL = 300   # 5 phút
PING_INTERVAL  = 180   # 3 phút
DATA_FILE      = "seen_assignments.json"
CONFIG_FILE    = "config.json"

# ══════════════════════════════════════════════════════
#  📁 Config (tất cả lưu trong file JSON)
# ══════════════════════════════════════════════════════

DEFAULT_CONFIG = {
    "telegram_token"  : "8721220429:AAHzvTU0sDyrt4NYosSih352ZQFFoSfrvz0",
    "telegram_chat_id": "",
    "render_url"      : "",
    "access_token"    : "f450e6097358120c6467a6faf4e81bcc55ef0493",
    "xsrf_token"      : "rWotkC2pVyvHO65cYvUxyeoBMyU9iuT9dK3siCiv",
    "bot_enabled"     : True,
    "updated_at"      : "",
    "cookie_updated_at": "",
}

def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                cfg.update(json.load(f))
        except Exception:
            pass
    return cfg

def save_config(cfg: dict):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

def get_olm_cookies() -> dict:
    cfg = load_config()
    return {
        "accessToken" : cfg["access_token"],
        "userToken"   : cfg["access_token"],
        "token"       : cfg["access_token"],
        "auth_token"  : cfg["access_token"],
        "XSRF-TOKEN"  : cfg["xsrf_token"],
        "third_party" : "enabled",
    }

# ══════════════════════════════════════════════════════
#  💾 Seen assignments
# ══════════════════════════════════════════════════════

def load_seen() -> set:
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE) as f:
                return set(json.load(f))
        except Exception:
            pass
    return set()

def save_seen(seen: set):
    with open(DATA_FILE, "w") as f:
        json.dump(list(seen), f)

# ══════════════════════════════════════════════════════
#  📡 Telegram
# ══════════════════════════════════════════════════════

def send_telegram(message: str) -> bool:
    cfg   = load_config()
    token = cfg.get("telegram_token", "")
    chat  = cfg.get("telegram_chat_id", "")
    if not token or not chat:
        _log("[Telegram] Chưa cấu hình token hoặc chat_id")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id"   : chat,
            "text"      : message,
            "parse_mode": "HTML",
        }, timeout=10)
        return r.status_code == 200
    except Exception as e:
        logging.error(f"[Telegram] {e}")
        return False

# ══════════════════════════════════════════════════════
#  🌐 Scrape OLM
# ══════════════════════════════════════════════════════

HEADERS = {
    "User-Agent"     : "Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36 Chrome/120 Safari/537.36",
    "Accept"         : "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "vi-VN,vi;q=0.9",
    "Referer"        : "https://olm.vn/",
}

def fetch_assignments() -> list:
    cookies = get_olm_cookies()
    try:
        r = requests.get(
            "https://olm.vn/lop-hoc-cua-toi",
            cookies=cookies, headers=HEADERS, timeout=15,
        )
        r.raise_for_status()
    except Exception as e:
        logging.error(f"[Fetch] {e}")
        return []

    soup  = BeautifulSoup(r.text, "html.parser")
    cards = (
        soup.select("div.assignment-item") or
        soup.select("div.homework-card")   or
        soup.select("div.card-assignment") or
        soup.select("[class*='assignment']") or
        soup.select("[class*='bai-tap']")
    )
    if not cards:
        cards = [
            p.find_parent("div")
            for p in soup.find_all(string=lambda t: t and "Ngày giao" in t)
            if p.find_parent("div")
        ]

    result = []
    for card in cards:
        text     = card.get_text(separator="\n", strip=True)
        title_el = card.find(["h3", "h4", "a", "strong"])
        title    = title_el.get_text(strip=True) if title_el else "Không rõ tên"
        link_el  = card.find("a", href=True)
        link     = "https://olm.vn" + link_el["href"] if link_el else "https://olm.vn/lop-hoc-cua-toi"
        subject  = ""
        for tag in card.find_all(["span", "div"]):
            cls = " ".join(tag.get("class", []))
            if any(k in cls for k in ["subject", "mon", "tag", "badge"]):
                subject = tag.get_text(strip=True); break
        deadline = next((l.strip() for l in text.split("\n") if "hạn" in l.lower()), "")
        status   = next((l.strip() for l in text.split("\n") if "hoàn thành" in l.lower() or "điểm" in l.lower()), "")
        uid      = hashlib.md5(f"{title}{link}".encode()).hexdigest()[:12]
        result.append({"id": uid, "title": title, "subject": subject,
                        "deadline": deadline, "status": status, "link": link})

    if not result:
        result = fetch_via_api(cookies)
    return result


def fetch_via_api(cookies: dict) -> list:
    for url in [
        "https://olm.vn/api/assignments",
        "https://olm.vn/api/homework",
        "https://olm.vn/api/student/assignments",
    ]:
        try:
            r = requests.get(url, cookies=cookies, headers=HEADERS, timeout=10)
            if r.status_code == 200 and "json" in r.headers.get("Content-Type", ""):
                data  = r.json()
                items = data if isinstance(data, list) else data.get("data", data.get("assignments", []))
                out   = []
                for item in items:
                    uid = hashlib.md5(str(item).encode()).hexdigest()[:12]
                    out.append({
                        "id"      : uid,
                        "title"   : item.get("title", item.get("name", "Bài tập mới")),
                        "subject" : item.get("subject", ""),
                        "deadline": item.get("deadline", item.get("due_date", "")),
                        "status"  : item.get("status", ""),
                        "link"    : "https://olm.vn/lop-hoc-cua-toi",
                    })
                return out
        except Exception:
            continue
    return []

# ══════════════════════════════════════════════════════
#  📢 Format tin nhắn
# ══════════════════════════════════════════════════════

def format_message(a: dict) -> str:
    now = datetime.now().strftime("%H:%M %d/%m/%Y")
    lines = ["📚 <b>BÀI TẬP MỚI TRÊN OLM!</b>", ""]
    if a["subject"]:  lines.append(f"📌 Môn: <b>{a['subject']}</b>")
    lines.append(f"📝 Tên: <b>{a['title']}</b>")
    if a["deadline"]: lines.append(f"⏰ Hạn: {a['deadline']}")
    if a["status"]:   lines.append(f"✅ {a['status']}")
    lines += [f"🔗 <a href='{a['link']}'>Xem bài tập</a>", "", f"<i>Phát hiện lúc {now}</i>"]
    return "\n".join(lines)

# ══════════════════════════════════════════════════════
#  🔄 Bot loop (chạy thread riêng)
# ══════════════════════════════════════════════════════

bot_log   = []
first_run = True

def bot_loop():
    global first_run
    seen = load_seen()
    first_run = len(seen) == 0
    time.sleep(5)  # chờ Flask khởi động
    send_telegram("🤖 <b>OLM Bot đã khởi động!</b>\nSẽ thông báo khi có bài tập mới.")

    while True:
        cfg = load_config()
        if not cfg.get("bot_enabled", True):
            _log("⏸️  Bot đang tắt (disabled từ admin)")
            time.sleep(60)
            continue

        if not cfg.get("telegram_chat_id"):
            _log("⚠️  Chưa cấu hình Chat ID — vào Admin để nhập")
            time.sleep(60)
            continue

        now_str = datetime.now().strftime("%H:%M:%S")
        _log(f"[{now_str}] Đang kiểm tra bài tập...")

        try:
            assignments = fetch_assignments()
            _log(f"  → Tìm thấy {len(assignments)} bài tập")

            if first_run and assignments:
                for a in assignments:
                    seen.add(a["id"])
                save_seen(seen)
                send_telegram(f"📋 <b>Đồng bộ {len(assignments)} bài hiện có.</b>\nSẽ báo khi có bài <b>mới</b>.")
                first_run = False
                _log(f"  → Lần đầu: lưu {len(assignments)} bài")
            else:
                new_count = 0
                for a in assignments:
                    if a["id"] not in seen:
                        _log(f"  🆕 Bài mới: {a['title']}")
                        if send_telegram(format_message(a)):
                            seen.add(a["id"])
                            save_seen(seen)
                            new_count += 1
                            time.sleep(1)
                _log(f"  → {new_count} bài mới" if new_count else "  → Không có bài mới")
        except Exception as e:
            _log(f"  ❌ Lỗi: {e}")

        time.sleep(CHECK_INTERVAL)


def _log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    entry = f"[{ts}] {msg}"
    bot_log.append(entry)
    if len(bot_log) > 100:
        bot_log.pop(0)
    print(entry)

# ══════════════════════════════════════════════════════
#  😴 Anti-sleep ping
# ══════════════════════════════════════════════════════

def ping_loop():
    time.sleep(30)
    while True:
        cfg = load_config()
        url = cfg.get("render_url", "").strip()
        if url:
            try:
                r = requests.get(url.rstrip("/") + "/ping", timeout=10)
                _log(f"[Ping] {r.status_code} ✅ Server còn sống")
            except Exception as e:
                _log(f"[Ping] ❌ Lỗi: {e}")
        else:
            _log("[Ping] Chưa cấu hình Render URL — bỏ qua")
        time.sleep(PING_INTERVAL)

# ══════════════════════════════════════════════════════
#  🌐 Flask App
# ══════════════════════════════════════════════════════

app = Flask(__name__)
app.secret_key = SECRET_KEY

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

# ══════════════════════════════════════════════════════
#  🎨 HTML Templates
# ══════════════════════════════════════════════════════

LOGIN_HTML = """
<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>OLM Bot - Đăng nhập</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    min-height: 100vh;
    display: flex; align-items: center; justify-content: center;
    background: linear-gradient(135deg, #1e1e2e 0%, #313244 100%);
    font-family: 'Segoe UI', sans-serif;
  }
  .card {
    background: #1e1e2e; border: 1px solid #45475a;
    border-radius: 16px; padding: 40px 36px; width: 360px;
    box-shadow: 0 20px 60px rgba(0,0,0,0.5);
  }
  .logo { text-align: center; font-size: 48px; margin-bottom: 8px; }
  h1 { text-align: center; color: #cba6f7; font-size: 22px; margin-bottom: 4px; }
  .sub { text-align: center; color: #6c7086; font-size: 13px; margin-bottom: 28px; }
  label { display: block; color: #a6adc8; font-size: 13px; margin-bottom: 6px; }
  input {
    width: 100%; padding: 11px 14px;
    background: #313244; border: 1px solid #45475a;
    border-radius: 8px; color: #cdd6f4; font-size: 14px;
    margin-bottom: 18px; outline: none; transition: border-color .2s;
  }
  input:focus { border-color: #cba6f7; }
  button {
    width: 100%; padding: 12px; background: #cba6f7; color: #1e1e2e;
    border: none; border-radius: 8px; font-size: 15px; font-weight: bold;
    cursor: pointer; transition: background .2s;
  }
  button:hover { background: #b4befe; }
  .error {
    background: #f38ba820; border: 1px solid #f38ba8;
    color: #f38ba8; border-radius: 8px;
    padding: 10px 14px; margin-bottom: 16px; font-size: 13px;
  }
</style>
</head>
<body>
<div class="card">
  <div class="logo">🤖</div>
  <h1>OLM Bot Admin</h1>
  <p class="sub">Đăng nhập để quản trị</p>
  {% if error %}<div class="error">❌ {{ error }}</div>{% endif %}
  <form method="POST">
    <label>Tài khoản</label>
    <input type="text" name="username" placeholder="Username" autocomplete="off">
    <label>Mật khẩu</label>
    <input type="password" name="password" placeholder="Password">
    <button type="submit">Đăng nhập →</button>
  </form>
</div>
</body>
</html>
"""

ADMIN_HTML = """
<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>OLM Bot - Admin</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #1e1e2e; color: #cdd6f4; font-family: 'Segoe UI', sans-serif; min-height: 100vh; }
  nav {
    background: #181825; border-bottom: 1px solid #45475a;
    padding: 14px 24px; display: flex; align-items: center; justify-content: space-between;
  }
  .nav-title { color: #cba6f7; font-size: 18px; font-weight: bold; }
  .nav-right { display: flex; gap: 12px; align-items: center; }
  .badge { padding: 4px 10px; border-radius: 20px; font-size: 12px; font-weight: bold; }
  .badge-on  { background: #a6e3a120; color: #a6e3a1; border: 1px solid #a6e3a1; }
  .badge-off { background: #f38ba820; color: #f38ba8; border: 1px solid #f38ba8; }
  a.logout {
    color: #f38ba8; text-decoration: none; font-size: 13px;
    padding: 6px 14px; border: 1px solid #f38ba8; border-radius: 6px;
  }
  .container { max-width: 920px; margin: 0 auto; padding: 24px 16px; }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 16px; }
  @media(max-width:640px){ .grid { grid-template-columns: 1fr; } }
  .card { background: #313244; border: 1px solid #45475a; border-radius: 12px; padding: 20px; }
  .card-title {
    font-size: 13px; text-transform: uppercase; letter-spacing: 1px;
    color: #a6adc8; margin-bottom: 14px; display: flex; align-items: center; gap: 8px;
  }
  .stat-val { font-size: 32px; font-weight: bold; color: #cba6f7; }
  .stat-sub { font-size: 12px; color: #6c7086; margin-top: 4px; }
  label { display: block; color: #a6adc8; font-size: 13px; margin-bottom: 6px; }
  input[type=text], input[type=password] {
    width: 100%; padding: 10px 13px;
    background: #1e1e2e; border: 1px solid #45475a;
    border-radius: 8px; color: #cdd6f4; font-size: 13px;
    margin-bottom: 14px; outline: none; font-family: monospace;
    transition: border-color .2s;
  }
  input:focus { border-color: #cba6f7; }
  .btn {
    padding: 10px 20px; border: none; border-radius: 8px;
    font-size: 13px; font-weight: bold; cursor: pointer; transition: .2s;
  }
  .btn-primary { background: #cba6f7; color: #1e1e2e; }
  .btn-primary:hover { background: #b4befe; }
  .btn-danger  { background: #f38ba820; color: #f38ba8; border: 1px solid #f38ba8; }
  .btn-danger:hover { background: #f38ba840; }
  .btn-success { background: #a6e3a120; color: #a6e3a1; border: 1px solid #a6e3a1; }
  .btn-success:hover { background: #a6e3a140; }
  .btn-sm { padding: 6px 14px; font-size: 12px; }
  .log-box {
    background: #1e1e2e; border: 1px solid #45475a; border-radius: 8px;
    padding: 12px; font-family: monospace; font-size: 12px;
    color: #a6e3a1; max-height: 280px; overflow-y: auto;
    white-space: pre-wrap; word-break: break-all;
  }
  .alert { padding: 10px 14px; border-radius: 8px; margin-bottom: 16px; font-size: 13px; }
  .alert-ok  { background: #a6e3a120; border: 1px solid #a6e3a1; color: #a6e3a1; }
  .alert-err { background: #f38ba820; border: 1px solid #f38ba8; color: #f38ba8; }
  .token-display {
    background: #1e1e2e; border: 1px solid #45475a; border-radius: 6px;
    padding: 8px 12px; font-family: monospace; font-size: 11px;
    color: #f9e2af; word-break: break-all; margin-bottom: 10px;
  }
  .row-btn { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 4px; }
  .full { grid-column: 1 / -1; }
  .hint { font-size: 11px; color: #6c7086; margin-bottom: 10px; line-height: 1.5; }
  .tag-warn { color: #fab387; font-size: 12px; margin-top: -10px; margin-bottom: 12px; }
</style>
</head>
<body>
<nav>
  <span class="nav-title">🤖 OLM Bot Admin</span>
  <div class="nav-right">
    <span class="badge {{ 'badge-on' if cfg.bot_enabled else 'badge-off' }}">
      {{ '● ĐANG CHẠY' if cfg.bot_enabled else '● ĐÃ TẮT' }}
    </span>
    <a class="logout" href="/logout">Đăng xuất</a>
  </div>
</nav>

<div class="container">
  {% if msg %}<div class="alert alert-ok">✅ {{ msg }}</div>{% endif %}
  {% if err %}<div class="alert alert-err">❌ {{ err }}</div>{% endif %}

  <div class="grid">

    <!-- Thống kê -->
    <div class="card">
      <div class="card-title">📊 Bài tập đã theo dõi</div>
      <div class="stat-val">{{ seen_count }}</div>
      <div class="stat-sub">Tổng số bài đã lưu lịch sử</div>
    </div>

    <div class="card">
      <div class="card-title">🕐 Cookie cập nhật lúc</div>
      <div class="stat-val" style="font-size:16px;margin-top:8px">{{ cfg.cookie_updated_at or 'Chưa cập nhật' }}</div>
      <div class="stat-sub">Cookie OLM hết hạn ~7 ngày</div>
    </div>

    <!-- === TELEGRAM === -->
    <div class="card full">
      <div class="card-title">📡 Cài đặt Telegram</div>
      <p class="hint">
        Lấy <b>Bot Token</b> từ @BotFather &nbsp;|&nbsp;
        Lấy <b>Chat ID</b> bằng cách nhắn /start cho @userinfobot
      </p>
      <form method="POST" action="/update-telegram">
        <label>Bot Token</label>
        <div class="token-display">{{ cfg.telegram_token[:15] }}...{{ cfg.telegram_token[-8:] if cfg.telegram_token else '' }}</div>
        <input type="text" name="bot_token" placeholder="Dán Bot Token mới (bỏ qua nếu không đổi)" />

        <label>Chat ID (Telegram của bạn)</label>
        <div class="token-display">{{ cfg.telegram_chat_id or '⚠️ Chưa cấu hình!' }}</div>
        <input type="text" name="chat_id" placeholder="Dán Chat ID vào đây..." />
        {% if not cfg.telegram_chat_id %}
        <div class="tag-warn">⚠️ Bot sẽ không gửi tin nếu chưa có Chat ID!</div>
        {% endif %}

        <div class="row-btn">
          <button type="submit" class="btn btn-primary">💾 Lưu Telegram</button>
          <button type="button" class="btn btn-success btn-sm" onclick="testTelegram()">🔔 Test gửi tin</button>
        </div>
      </form>
    </div>

    <!-- === COOKIE OLM === -->
    <div class="card full">
      <div class="card-title">🍪 Cập nhật Cookie OLM</div>
      <p class="hint">
        Dùng script <b>Violentmonkey</b> trên OLM → nhấn "🍪 Lấy Cookie" → copy <b>accessToken</b> dán vào đây.<br>
        Cookie hết hạn khoảng <b>7 ngày</b>, cần cập nhật lại khi bot báo lỗi xác thực.
      </p>
      <form method="POST" action="/update-cookie">
        <label>Access Token (accessToken)</label>
        <div class="token-display">{{ cfg.access_token[:20] }}...{{ cfg.access_token[-10:] }}</div>
        <input type="text" name="access_token" placeholder="Dán accessToken mới vào đây..." />

        <label>XSRF Token <span style="color:#6c7086">(không bắt buộc)</span></label>
        <div class="token-display">{{ cfg.xsrf_token[:20] }}...{{ cfg.xsrf_token[-10:] }}</div>
        <input type="text" name="xsrf_token" placeholder="Dán XSRF-TOKEN mới (để trống nếu không đổi)" />

        <div class="row-btn">
          <button type="submit" class="btn btn-primary">💾 Lưu Cookie</button>
        </div>
      </form>
    </div>

    <!-- === RENDER URL === -->
    <div class="card full">
      <div class="card-title">🏓 Cấu hình Anti-Sleep</div>
      <p class="hint">
        Nhập URL Render của bạn để bot tự ping mỗi 3 phút, tránh server ngủ đông.<br>
        Ví dụ: <code style="color:#f9e2af">https://olm-bot.onrender.com</code>
      </p>
      <form method="POST" action="/update-render-url">
        <label>Render URL</label>
        <div class="token-display">{{ cfg.render_url or '⚠️ Chưa cấu hình — server có thể ngủ sau 15 phút' }}</div>
        <input type="text" name="render_url" placeholder="https://tên-app-của-bạn.onrender.com" />
        <div class="row-btn">
          <button type="submit" class="btn btn-primary">💾 Lưu URL</button>
        </div>
      </form>
    </div>

    <!-- === ĐIỀU KHIỂN BOT === -->
    <div class="card">
      <div class="card-title">⚙️ Điều khiển Bot</div>
      <div class="row-btn">
        <form method="POST" action="/toggle-bot">
          <button type="submit" class="btn {{ 'btn-danger' if cfg.bot_enabled else 'btn-success' }}">
            {{ '⏸️ Tắt Bot' if cfg.bot_enabled else '▶️ Bật Bot' }}
          </button>
        </form>
        <form method="POST" action="/reset-seen">
          <button type="submit" class="btn btn-danger btn-sm"
            onclick="return confirm('Reset sẽ xoá lịch sử. Bot sẽ không báo lại bài cũ. Tiếp tục?')">
            🗑️ Reset lịch sử
          </button>
        </form>
      </div>
    </div>

    <!-- === PING STATUS === -->
    <div class="card">
      <div class="card-title">🏓 Anti-Sleep Status</div>
      {% if cfg.render_url %}
      <div style="font-size:13px;color:#a6e3a1">● Ping mỗi 3 phút</div>
      <div class="stat-sub" style="margin-top:8px;word-break:break-all">{{ cfg.render_url }}</div>
      {% else %}
      <div style="font-size:13px;color:#f38ba8">⚠️ Chưa cấu hình Render URL</div>
      <div class="stat-sub" style="margin-top:8px">Server có thể ngủ sau 15 phút không hoạt động</div>
      {% endif %}
    </div>

    <!-- === LOG === -->
    <div class="card full">
      <div class="card-title" style="justify-content:space-between">
        <span>📋 Log hoạt động</span>
        <button class="btn btn-sm" style="background:#45475a;color:#cdd6f4" onclick="location.reload()">🔄 Refresh</button>
      </div>
      <div class="log-box" id="logbox">{{ logs }}</div>
    </div>

  </div>
</div>

<script>
  const lb = document.getElementById('logbox');
  lb.scrollTop = lb.scrollHeight;

  async function testTelegram() {
    const r = await fetch('/test-telegram', {method:'POST'});
    const d = await r.json();
    alert(d.ok ? '✅ Gửi thành công! Kiểm tra Telegram của bạn.' : '❌ Gửi thất bại: ' + d.error);
  }

  setInterval(() => location.reload(), 30000);
</script>
</body>
</html>
"""

# ══════════════════════════════════════════════════════
#  🔌 Routes
# ══════════════════════════════════════════════════════

@app.route("/")
def index():
    return redirect(url_for("admin"))

@app.route("/ping")
def ping():
    return "OK", 200

@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        u = request.form.get("username", "")
        p = request.form.get("password", "")
        if u == ADMIN_USERNAME and p == ADMIN_PASSWORD:
            session["logged_in"] = True
            return redirect(url_for("admin"))
        error = "Sai tài khoản hoặc mật khẩu!"
    return render_template_string(LOGIN_HTML, error=error)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/admin")
@login_required
def admin():
    cfg  = load_config()
    seen = load_seen()
    logs = "\n".join(bot_log[-60:]) or "Chưa có log..."
    return render_template_string(
        ADMIN_HTML,
        cfg=cfg,
        seen_count=len(seen),
        logs=logs,
        msg=request.args.get("msg"),
        err=request.args.get("err"),
    )

@app.route("/update-telegram", methods=["POST"])
@login_required
def update_telegram():
    token   = request.form.get("bot_token", "").strip()
    chat_id = request.form.get("chat_id",   "").strip()
    if not chat_id:
        return redirect(url_for("admin") + "?err=Chat+ID+không+được+để+trống!")
    cfg = load_config()
    if token:   cfg["telegram_token"]   = token
    if chat_id: cfg["telegram_chat_id"] = chat_id
    cfg["updated_at"] = datetime.now().strftime("%H:%M %d/%m/%Y")
    save_config(cfg)
    _log(f"[Admin] Telegram cập nhật — Chat ID: {chat_id}")
    return redirect(url_for("admin") + "?msg=Cài+đặt+Telegram+đã+lưu!")

@app.route("/update-cookie", methods=["POST"])
@login_required
def update_cookie():
    access = request.form.get("access_token", "").strip()
    xsrf   = request.form.get("xsrf_token",   "").strip()
    if not access:
        return redirect(url_for("admin") + "?err=Access+Token+không+được+để+trống!")
    cfg = load_config()
    cfg["access_token"] = access
    if xsrf: cfg["xsrf_token"] = xsrf
    cfg["cookie_updated_at"] = datetime.now().strftime("%H:%M %d/%m/%Y")
    save_config(cfg)
    _log(f"[Admin] Cookie OLM đã cập nhật: {access[:16]}...")
    return redirect(url_for("admin") + "?msg=Cookie+OLM+đã+lưu+thành+công!")

@app.route("/update-render-url", methods=["POST"])
@login_required
def update_render_url():
    url = request.form.get("render_url", "").strip().rstrip("/")
    if not url:
        return redirect(url_for("admin") + "?err=URL+không+được+để+trống!")
    cfg = load_config()
    cfg["render_url"] = url
    save_config(cfg)
    _log(f"[Admin] Render URL đã cập nhật: {url}")
    return redirect(url_for("admin") + "?msg=Render+URL+đã+lưu!+Anti-sleep+đã+bật.")

@app.route("/toggle-bot", methods=["POST"])
@login_required
def toggle_bot():
    cfg = load_config()
    cfg["bot_enabled"] = not cfg.get("bot_enabled", True)
    save_config(cfg)
    state = "bật" if cfg["bot_enabled"] else "tắt"
    _log(f"[Admin] Bot đã {state}")
    return redirect(url_for("admin") + f"?msg=Bot+đã+{state}!")

@app.route("/reset-seen", methods=["POST"])
@login_required
def reset_seen_route():
    global first_run
    save_seen(set())
    first_run = True
    _log("[Admin] Đã reset lịch sử bài tập")
    return redirect(url_for("admin") + "?msg=Đã+reset+lịch+sử!")

@app.route("/test-telegram", methods=["POST"])
@login_required
def test_telegram():
    ok = send_telegram("🔔 <b>Test từ OLM Bot Admin!</b>\nBot đang hoạt động bình thường ✅")
    return jsonify({"ok": ok, "error": "" if ok else "Kiểm tra Bot Token và Chat ID trong Admin"})

# ══════════════════════════════════════════════════════
#  🚀 Khởi động
# ══════════════════════════════════════════════════════

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    threading.Thread(target=bot_loop, daemon=True).start()
    threading.Thread(target=ping_loop, daemon=True).start()

    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
