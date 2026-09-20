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
    # OLM dùng onlinemath_session (lấy từ Cookie Grabber) + XSRF-TOKEN
    cookies = {
        "onlinemath_session": cfg.get("access_token", ""),
        "XSRF-TOKEN"        : cfg.get("xsrf_token", ""),
        "accessToken"       : cfg.get("access_token", ""),
        "token"             : cfg.get("access_token", ""),
    }
    return {k: v for k, v in cookies.items() if v}

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

def _resolve_chat_id(chat: str):
    """Chuẩn hoá chat_id: số → int, username → @username"""
    if chat.lstrip("-").isdigit():
        return int(chat)
    return chat if chat.startswith("@") else f"@{chat}"

def send_telegram(message: str, reply_markup: dict = None) -> bool:
    cfg   = load_config()
    token = cfg.get("telegram_token", "").strip()
    chat  = cfg.get("telegram_chat_id", "").strip()
    if not token or not chat:
        _log("[Telegram] Chưa cấu hình token hoặc chat_id")
        return False

    chat_id = _resolve_chat_id(chat)
    url     = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id"                 : chat_id,
        "text"                    : message,
        "parse_mode"              : "HTML",
        "disable_web_page_preview": False,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup

    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code == 200:
            return True
        try:
            err_detail = r.json().get("description", r.text[:200])
        except Exception:
            err_detail = r.text[:200]
        _log(f"[Telegram] ❌ HTTP {r.status_code}: {err_detail}")
        logging.error(f"[Telegram] {r.status_code} {err_detail}")
        return False
    except Exception as e:
        _log(f"[Telegram] ❌ Exception: {e}")
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
    """
    Scrape trang bài tập OLM theo đúng cấu trúc HTML thật:
    - Thẻ môn: span badge (Lịch sử, Ngữ văn, Toán...)
    - Tên bài: thẻ <a> tiêu đề chính
    - Trạng thái: span "Hoàn thành: X%, Điểm: Y"
    - Lớp, Giáo viên, Ngày giao, Ngày đến hạn: text trong card
    """
    cookies = get_olm_cookies()
    headers = dict(HEADERS)
    headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())

    try:
        r = requests.get(
            "https://olm.vn/bai-tap-duoc-giao",
            cookies=cookies, headers=headers, timeout=20,
        )
        # Nếu redirect về login → cookie hết hạn
        if "đăng nhập" in r.text.lower() or "login" in r.url:
            _log("⚠️ [Fetch] Cookie hết hạn — cần cập nhật lại!")
            return []
        r.raise_for_status()
        html = r.text
    except Exception as e:
        logging.error(f"[Fetch] {e}")
        # Thử URL dự phòng
        try:
            r2 = requests.get(
                "https://olm.vn/lop-hoc-cua-toi",
                cookies=cookies, headers=headers, timeout=20,
            )
            html = r2.text
        except Exception as e2:
            logging.error(f"[Fetch fallback] {e2}")
            return []

    return _parse_olm_html(html)


def _parse_olm_html(html: str) -> list:
    """Parse HTML trang OLM, trả về list bài tập với đầy đủ thông tin."""
    import re
    soup = BeautifulSoup(html, "html.parser")
    result = []

    # ── Chiến lược 1: card bài tập chuẩn OLM ──────────────────────
    # Mỗi bài tập là 1 div/article chứa thẻ môn + tên bài + thông tin
    cards = []

    # Tìm theo cấu trúc thực tế: div chứa "Ngày giao" hoặc "Ngày đến hạn"
    for node in soup.find_all(string=re.compile(r"Ngày giao|Ngày đến hạn", re.I)):
        # Leo lên để tìm card container
        parent = node.find_parent(["div", "article", "li"])
        for _ in range(4):   # leo tối đa 4 cấp
            if parent is None:
                break
            txt = parent.get_text()
            if ("Ngày giao" in txt and ("Lớp:" in txt or "Giáo viên:" in txt)):
                if parent not in cards:
                    cards.append(parent)
                break
            parent = parent.find_parent(["div", "article", "li"])

    # ── Chiến lược 2: tìm theo class ───────────────────────────────
    if not cards:
        for sel in [
            "div.homework-item", "div.assignment-card", "div.bai-tap-item",
            "[class*='homework']", "[class*='assignment']", "[class*='bai-tap']",
            "div.card-body", "div.list-item",
        ]:
            found = soup.select(sel)
            if found:
                cards = [c for c in found if "Ngày" in c.get_text()]
                if cards:
                    break

    _log(f"  → Parser tìm được {len(cards)} card HTML")

    for card in cards:
        text_lines = [l.strip() for l in card.get_text(separator="\n").split("\n") if l.strip()]
        full_text  = "\n".join(text_lines)

        # ── Môn học (badge/tag đầu tiên) ──
        subject = ""
        for tag in card.find_all(["span", "div", "label"]):
            cls = " ".join(tag.get("class", []))
            txt = tag.get_text(strip=True)
            if (any(k in cls.lower() for k in ["badge", "tag", "label", "mon", "subject"])
                    and txt and len(txt) < 30 and not txt.startswith("Hoàn")):
                subject = txt; break
        # fallback: dòng đầu ngắn (tên môn thường < 20 ký tự)
        if not subject and text_lines:
            first = text_lines[0]
            if len(first) < 25 and not any(c.isdigit() for c in first[:5]):
                subject = first

        # ── Tên bài (thẻ <a> hoặc <h3/h4>) ──
        title = ""
        for tag in card.find_all(["a", "h3", "h4", "h5", "strong"]):
            t = tag.get_text(strip=True)
            if t and len(t) > 3 and t not in (subject,):
                title = t; break
        if not title:
            # Lấy dòng dài nhất không phải nhãn
            for line in text_lines:
                if (len(line) > 8
                        and not re.match(r"^(Lớp|Giáo viên|Ngày|Loại|Hoàn thành|Điểm):", line)
                        and line != subject):
                    title = line; break

        # ── Link ──
        link = "https://olm.vn/bai-tap-duoc-giao"
        link_el = card.find("a", href=True)
        if link_el:
            href = link_el["href"]
            link = href if href.startswith("http") else "https://olm.vn" + href

        # ── Trạng thái (Hoàn thành / Điểm) ──
        status = ""
        for line in text_lines:
            if re.search(r"hoàn thành|điểm|%", line, re.I):
                status = line; break
        # Thử lấy từ span badge màu
        for tag in card.find_all(["span", "div"]):
            t = tag.get_text(strip=True)
            if re.search(r"hoàn thành.*%|điểm.*\d", t, re.I) and len(t) < 50:
                status = t; break

        # ── Lớp ──
        classroom = ""
        for line in text_lines:
            if line.startswith("Lớp:") or re.match(r"^Lớp\s*:", line, re.I):
                classroom = line.replace("Lớp:", "").replace("Lớp :", "").strip(); break

        # ── Giáo viên ──
        teacher = ""
        for line in text_lines:
            if re.match(r"^Giáo viên\s*:", line, re.I):
                teacher = re.sub(r"^Giáo viên\s*:\s*", "", line, flags=re.I).strip(); break

        # ── Ngày giao ──
        assigned = ""
        for line in text_lines:
            if re.match(r"^Ngày giao\s*:", line, re.I):
                assigned = re.sub(r"^Ngày giao\s*:\s*", "", line, flags=re.I).strip(); break

        # ── Ngày đến hạn (deadline) ──
        deadline = ""
        for line in text_lines:
            if re.match(r"^Ngày đến hạn\s*:", line, re.I):
                deadline = re.sub(r"^Ngày đến hạn\s*:\s*", "", line, flags=re.I).strip(); break
        if not deadline:
            for line in text_lines:
                if re.search(r"hạn|due", line, re.I):
                    deadline = line; break

        # ── Loại học liệu ──
        loai = ""
        for line in text_lines:
            if re.match(r"^Loại học liệu\s*:", line, re.I):
                loai = re.sub(r"^Loại học liệu\s*:\s*", "", line, flags=re.I).strip(); break

        if not title:
            continue  # bỏ qua card rỗng

        uid = hashlib.md5(f"{title}{link}{assigned}".encode()).hexdigest()[:12]
        result.append({
            "id"       : uid,
            "title"    : title,
            "subject"  : subject,
            "classroom": classroom,
            "teacher"  : teacher,
            "loai"     : loai,
            "assigned" : assigned,
            "deadline" : deadline,
            "status"   : status,
            "link"     : link,
        })

    # ── Chiến lược 3: API JSON ──────────────────────────────────────
    if not result:
        result = _fetch_via_api(get_olm_cookies())

    return result


def _fetch_via_api(cookies: dict) -> list:
    """Thử các endpoint API JSON của OLM."""
    for url in [
        "https://olm.vn/api/assignments",
        "https://olm.vn/api/homework",
        "https://olm.vn/api/student/assignments",
        "https://olm.vn/api/bai-tap",
    ]:
        try:
            r = requests.get(url, cookies=cookies, headers=HEADERS, timeout=10)
            if r.status_code == 200 and "json" in r.headers.get("Content-Type", ""):
                data  = r.json()
                items = data if isinstance(data, list) else data.get("data", data.get("assignments", data.get("bai_tap", [])))
                if not isinstance(items, list):
                    continue
                out = []
                for item in items:
                    uid = hashlib.md5(str(item).encode()).hexdigest()[:12]
                    out.append({
                        "id"       : uid,
                        "title"    : item.get("title", item.get("name", item.get("ten", "Bài tập mới"))),
                        "subject"  : item.get("subject", item.get("mon", "")),
                        "classroom": item.get("class", item.get("lop", "")),
                        "teacher"  : item.get("teacher", item.get("giao_vien", "")),
                        "loai"     : item.get("type", ""),
                        "assigned" : item.get("assigned_at", item.get("ngay_giao", "")),
                        "deadline" : item.get("deadline", item.get("due_date", item.get("den_han", ""))),
                        "status"   : item.get("status", item.get("trang_thai", "")),
                        "link"     : item.get("url", "https://olm.vn/bai-tap-duoc-giao"),
                    })
                if out:
                    _log(f"  → API {url}: {len(out)} bài")
                    return out
        except Exception:
            continue
    return []

# ══════════════════════════════════════════════════════
#  📢 Format tin nhắn
# ══════════════════════════════════════════════════════

def _deadline_info(deadline_str: str):
    """
    Phân tích chuỗi deadline, trả về (emoji_urgency, deadline_display, days_left).
    Hỗ trợ định dạng: dd/mm/yyyy, dd-mm-yyyy, yyyy-mm-dd, hoặc chuỗi thô.
    """
    if not deadline_str:
        return "📅", deadline_str, None
    import re
    now = datetime.now()
    dt  = None
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y %H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(deadline_str.strip(), fmt); break
        except Exception:
            pass
    if dt is None:
        # thử trích ngày từ chuỗi tự do
        m = re.search(r"(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})", deadline_str)
        if m:
            try:
                dt = datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)))
            except Exception:
                pass

    if dt is None:
        return "📅", deadline_str, None

    delta = (dt.date() - now.date()).days
    disp  = dt.strftime("%d/%m/%Y")

    if delta < 0:
        return "🔴", f"{disp} <i>(đã quá hạn {abs(delta)} ngày)</i>", delta
    elif delta == 0:
        return "🔴", f"{disp} <b>(HÔM NAY!)</b>", delta
    elif delta == 1:
        return "🟠", f"{disp} <b>(ngày mai!)</b>", delta
    elif delta <= 3:
        return "🟠", f"{disp} <i>(còn {delta} ngày)</i>", delta
    elif delta <= 7:
        return "🟡", f"{disp} <i>(còn {delta} ngày)</i>", delta
    else:
        return "🟢", f"{disp} <i>(còn {delta} ngày)</i>", delta


def format_message(a: dict, header: str = "🔔  <b>BÀI TẬP MỚI TRÊN OLM!</b>") -> tuple:
    """
    Trả về (text, reply_markup) để gửi kèm nút inline.
    Hiển thị đầy đủ: môn, tên bài, lớp, giáo viên, loại, ngày giao, deadline, trạng thái.
    """
    now = datetime.now().strftime("%H:%M  %d/%m/%Y")

    lines = [
        "━━━━━━━━━━━━━━━━━━━━━━━",
        header,
        "━━━━━━━━━━━━━━━━━━━━━━━",
        "",
    ]

    if a.get("subject"):
        lines.append(f"📌  Môn:  <b>{a['subject']}</b>")

    lines.append(f"📝  Tên bài:  <b>{a['title']}</b>")

    if a.get("loai"):
        lines.append(f"📂  Loại:  {a['loai']}")

    if a.get("classroom"):
        lines.append(f"🏫  Lớp:  {a['classroom']}")

    if a.get("teacher"):
        lines.append(f"👨‍🏫  Giáo viên:  {a['teacher']}")

    if a.get("assigned"):
        lines.append(f"📅  Ngày giao:  {a['assigned']}")

    urg_emoji, deadline_display, days_left = _deadline_info(a.get("deadline", ""))
    if deadline_display:
        lines.append(f"{urg_emoji}  Hạn nộp:  {deadline_display}")

    if a.get("status"):
        lines.append(f"📊  Trạng thái:  {a['status']}")

    if days_left is not None:
        if days_left < 0:
            lines += ["", "⚠️  <b>Bài này đã quá hạn!</b>"]
        elif days_left == 0:
            lines += ["", "🚨  <b>DEADLINE HÔM NAY — Nộp ngay!</b>"]
        elif days_left == 1:
            lines += ["", "⚡  <b>Còn 1 ngày — Gấp lên!</b>"]

    lines += [
        "",
        f"🕐  <i>Phát hiện lúc {now}</i>",
        "━━━━━━━━━━━━━━━━━━━━━━━",
    ]

    reply_markup = {
        "inline_keyboard": [[
            {"text": "📖  Xem bài tập ngay →", "url": a["link"]},
        ]]
    }

    return "\n".join(lines), reply_markup

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
    now_str = datetime.now().strftime("%H:%M  %d/%m/%Y")
    send_telegram(
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🤖  <b>OLM Bot đã khởi động!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "✅  Bot đang chạy và theo dõi bài tập.\n"
        "📡  Sẽ kiểm tra mỗi <b>5 phút</b> một lần.\n"
        "🔔  Thông báo ngay khi có <b>bài tập mới</b>.\n\n"
        f"🕐  <i>Khởi động lúc {now_str}</i>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━"
    )

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
                send_telegram(
                    "━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"📋  <b>Đã đồng bộ {len(assignments)} bài tập hiện có.</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                    "✅  Lịch sử đã lưu — bot sẽ chỉ báo bài <b>MỚI</b> từ bây giờ.\n\n"
                    "🕐  <i>Từ lần kiểm tra tiếp theo, bạn sẽ nhận thông báo ngay\n"
                    "khi OLM có bài tập mới được giao.</i>\n"
                    "━━━━━━━━━━━━━━━━━━━━━━━"
                )
                first_run = False
                _log(f"  → Lần đầu: lưu {len(assignments)} bài")
            else:
                new_count = 0
                for a in assignments:
                    if a["id"] not in seen:
                        _log(f"  🆕 Bài mới: {a['title']}")
                        msg_text, msg_markup = format_message(a)
                        if send_telegram(msg_text, reply_markup=msg_markup):
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
        Lấy <b>Chat ID số</b>: nhắn <b>/start</b> cho <b>@userinfobot</b> → copy số ID (vd: <code>123456789</code>).<br>
        ⚠️ Nếu dùng <b>@username</b>, bot phải được <b>nhắn tin trước</b> (send /start) mới gửi được.
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

    <!-- === GỬI TẤT CẢ BÀI TẬP === -->
    <div class="card full">
      <div class="card-title">📚 Gửi Danh Sách Bài Tập Lên Telegram</div>
      <p class="hint">
        Nhấn nút bên dưới để bot lấy <b>toàn bộ bài tập hiện tại</b> trên OLM và gửi ngay lên Telegram.<br>
        Mỗi bài sẽ hiển thị đầy đủ: môn học, tên bài, lớp, giáo viên, ngày giao, hạn nộp, trạng thái.
      </p>
      <div class="row-btn">
        <button type="button" class="btn btn-primary" onclick="sendAllAssignments()" id="btn-send-all">
          📤 Gửi tất cả bài tập lên Telegram
        </button>
        <button type="button" class="btn btn-success btn-sm" onclick="previewAssignments()" id="btn-preview">
          👁️ Xem trước (JSON)
        </button>
      </div>
      <div id="send-all-result" style="margin-top:12px;font-size:13px;display:none"></div>
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

  async function sendAllAssignments() {
    const btn = document.getElementById('btn-send-all');
    const box = document.getElementById('send-all-result');
    btn.disabled = true;
    btn.textContent = '⏳ Đang lấy bài tập và gửi...';
    box.style.display = 'none';
    try {
      const r = await fetch('/send-all', {method:'POST'});
      const d = await r.json();
      box.style.display = 'block';
      if (d.ok) {
        box.style.background = '#a6e3a120';
        box.style.border = '1px solid #a6e3a1';
        box.style.color = '#a6e3a1';
        box.style.padding = '10px 14px';
        box.style.borderRadius = '8px';
        box.innerHTML = `✅ Đã gửi <b>${d.sent}/${d.total}</b> bài tập lên Telegram!${d.failed ? ' (' + d.failed + ' bài lỗi)' : ''}`;
      } else {
        box.style.background = '#f38ba820';
        box.style.border = '1px solid #f38ba8';
        box.style.color = '#f38ba8';
        box.style.padding = '10px 14px';
        box.style.borderRadius = '8px';
        box.textContent = '❌ ' + (d.error || 'Lỗi không xác định');
      }
    } catch(e) {
      box.style.display = 'block';
      box.textContent = '❌ Lỗi kết nối: ' + e;
    } finally {
      btn.disabled = false;
      btn.textContent = '📤 Gửi tất cả bài tập lên Telegram';
    }
  }

  async function previewAssignments() {
    const btn = document.getElementById('btn-preview');
    const box = document.getElementById('send-all-result');
    btn.disabled = true;
    btn.textContent = '⏳ Đang tải...';
    try {
      const r = await fetch('/list-assignments');
      const d = await r.json();
      box.style.display = 'block';
      box.style.background = '#1e1e2e';
      box.style.border = '1px solid #45475a';
      box.style.color = '#f9e2af';
      box.style.padding = '10px 14px';
      box.style.borderRadius = '8px';
      box.style.fontFamily = 'monospace';
      box.style.fontSize = '11px';
      box.style.whiteSpace = 'pre-wrap';
      box.style.maxHeight = '300px';
      box.style.overflowY = 'auto';
      box.textContent = `Tổng: ${d.total} bài (${d.fetched_at})\n\n` +
        d.assignments.map((a,i) =>
          `[${i+1}] ${a.subject ? '['+a.subject+'] ' : ''}${a.title}\n` +
          `     Lớp: ${a.classroom||'-'}  GV: ${a.teacher||'-'}\n` +
          `     Giao: ${a.assigned||'-'}  Hạn: ${a.deadline||'-'}\n` +
          `     TT: ${a.status||'-'}`
        ).join('\n\n');
    } catch(e) {
      box.textContent = '❌ Lỗi: ' + e;
    } finally {
      btn.disabled = false;
      btn.textContent = '👁️ Xem trước (JSON)';
    }
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

    cfg = load_config()

    # Cập nhật token nếu có nhập mới
    if token:
        cfg["telegram_token"] = token

    # Chat ID: nếu nhập mới thì lưu, nếu không nhập mà đã có sẵn thì giữ
    if chat_id:
        # Nếu user nhập "@luongtuyen20" → lưu nguyên, bot sẽ xử lý
        cfg["telegram_chat_id"] = chat_id
    elif not cfg.get("telegram_chat_id"):
        return redirect(url_for("admin") + "?err=Chat+ID+không+được+để+trống!+Nhập+Chat+ID+vào+ô+bên+dưới.")

    cfg["updated_at"] = datetime.now().strftime("%H:%M %d/%m/%Y")
    save_config(cfg)
    _log(f"[Admin] Telegram cập nhật — Chat ID: {cfg['telegram_chat_id']}")
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

@app.route("/list-assignments", methods=["GET"])
@login_required
def list_assignments():
    """API JSON: trả về toàn bộ bài tập hiện tại."""
    assignments = fetch_assignments()
    return jsonify({
        "total"      : len(assignments),
        "assignments": assignments,
        "fetched_at" : datetime.now().strftime("%H:%M:%S %d/%m/%Y"),
    })

@app.route("/send-all", methods=["POST"])
@login_required
def send_all():
    """Gửi TOÀN BỘ bài tập hiện tại lên Telegram ngay lập tức."""
    assignments = fetch_assignments()
    if not assignments:
        _log("[Admin] Send-all: không lấy được bài tập")
        return jsonify({"ok": False, "error": "Không lấy được bài tập — kiểm tra cookie OLM.", "total": 0})

    now_str = datetime.now().strftime("%H:%M  %d/%m/%Y")

    # Gửi tin tổng quan trước
    send_telegram(
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📋  <b>DANH SÁCH BÀI TẬP OLM</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📊  Tổng số bài:  <b>{len(assignments)} bài tập</b>\n"
        f"🕐  Cập nhật lúc:  <i>{now_str}</i>\n\n"
        "⬇️  Danh sách chi tiết bên dưới...\n"
        "━━━━━━━━━━━━━━━━━━━━━━━"
    )
    time.sleep(0.5)

    sent = 0
    failed = 0
    for i, a in enumerate(assignments, 1):
        header = f"📚  <b>[{i}/{len(assignments)}] BÀI TẬP OLM</b>"
        msg_text, msg_markup = format_message(a, header=header)
        if send_telegram(msg_text, reply_markup=msg_markup):
            sent += 1
        else:
            failed += 1
        time.sleep(0.4)   # tránh rate-limit Telegram

    _log(f"[Admin] Send-all: {sent}/{len(assignments)} bài gửi thành công")

    # Gửi tóm tắt cuối
    time.sleep(0.5)
    send_telegram(
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"✅  <b>Đã gửi xong {sent}/{len(assignments)} bài tập!</b>\n"
        f"{'❌  ' + str(failed) + ' bài gửi thất bại.' if failed else ''}"
        "━━━━━━━━━━━━━━━━━━━━━━━"
    )

    return jsonify({"ok": True, "total": len(assignments), "sent": sent, "failed": failed})

@app.route("/test-telegram", methods=["POST"])
@login_required
def test_telegram():
    cfg   = load_config()
    token = cfg.get("telegram_token", "").strip()
    chat  = cfg.get("telegram_chat_id", "").strip()

    if not token:
        return jsonify({"ok": False, "error": "Bot Token chưa được cấu hình!"})
    if not chat:
        return jsonify({"ok": False, "error": "Chat ID chưa được cấu hình! Hãy nhập Chat ID vào ô bên dưới và lưu."})

    # Thử gửi và lấy lỗi chi tiết
    if chat.lstrip("-").isdigit():
        chat_id = int(chat)
    else:
        chat_id = chat if chat.startswith("@") else f"@{chat}"

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id"   : chat_id,
            "text"      : (
                "━━━━━━━━━━━━━━━━━━━━━━━\n"
                "🔔  <b>TEST — OLM Bot hoạt động!</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                "✅  Kết nối Telegram thành công!\n"
                "📡  Bot sẽ gửi thông báo vào đây\n"
                "       khi có bài tập mới trên OLM.\n\n"
                f"🕐  <i>Test lúc {datetime.now().strftime('%H:%M  %d/%m/%Y')}</i>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━"
            ),
            "parse_mode": "HTML",
        }, timeout=10)
        if r.status_code == 200:
            _log("[Admin] Test Telegram: Gửi thành công ✅")
            return jsonify({"ok": True, "error": ""})
        try:
            tg_err = r.json().get("description", r.text[:300])
        except Exception:
            tg_err = r.text[:300]
        _log(f"[Admin] Test Telegram thất bại: {tg_err}")
        return jsonify({"ok": False, "error": f"Telegram lỗi: {tg_err}"})
    except Exception as e:
        _log(f"[Admin] Test Telegram exception: {e}")
        return jsonify({"ok": False, "error": str(e)})

# ══════════════════════════════════════════════════════
#  🚀 Khởi động
# ══════════════════════════════════════════════════════

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    threading.Thread(target=bot_loop, daemon=True).start()
    threading.Thread(target=ping_loop, daemon=True).start()

    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
