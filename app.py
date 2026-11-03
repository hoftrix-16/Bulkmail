import os
import json
import time
import random
import smtplib
import threading
import queue
import re
import io
import urllib.request
import pandas as pd
from datetime import datetime, date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid
from flask import Flask, render_template, request, jsonify, Response, stream_with_context

app = Flask(__name__)
app.secret_key = os.urandom(24)

# ── Global campaign state ──────────────────────────────────────────────────────
campaign = {
    "running": False,
    "paused": False,
    "stop_flag": False,
    "total": 0,
    "sent": 0,
    "failed": 0,
    "skipped": 0,
    "current_email": "",
    "status": "idle",          # idle | running | paused | done | stopped
    "logs": [],
    "daily_sent": 0,
    "daily_date": str(date.today()),
    "leads": [],
}

DAILY_LIMIT = 2000
event_queue: queue.Queue = queue.Queue(maxsize=500)
campaign_lock = threading.Lock()

# ── Helper ─────────────────────────────────────────────────────────────────────

def push_event(data: dict):
    """Push SSE event; drop oldest if full."""
    try:
        event_queue.put_nowait(data)
    except queue.Full:
        try:
            event_queue.get_nowait()
        except queue.Empty:
            pass
        event_queue.put_nowait(data)


def add_log(msg: str, level: str = "info"):
    ts = datetime.now().strftime("%H:%M:%S")
    entry = {"ts": ts, "msg": msg, "level": level}
    campaign["logs"].append(entry)
    if len(campaign["logs"]) > 1000:
        campaign["logs"] = campaign["logs"][-800:]
    push_event({"type": "log", **entry})


def push_progress():
    push_event({
        "type": "progress",
        "sent": campaign["sent"],
        "failed": campaign["failed"],
        "skipped": campaign["skipped"],
        "total": campaign["total"],
        "status": campaign["status"],
        "current_email": campaign["current_email"],
        "daily_sent": campaign["daily_sent"],
    })


def reset_daily_if_needed():
    today = str(date.today())
    if campaign["daily_date"] != today:
        campaign["daily_date"] = today
        campaign["daily_sent"] = 0


def render_template_string(template: str, row: dict) -> str:
    result = template
    for k, v in row.items():
        result = result.replace("{{" + k + "}}", str(v))
        result = result.replace("{{" + k.lower() + "}}", str(v))
    return result


def send_single_email(smtp_cfg: dict, to_email: str, to_name: str,
                       subject: str, body_html: str, body_plain: str) -> bool:
    """Send one email. Returns True on success."""
    for attempt in range(2):
        try:
            msg = MIMEMultipart("alternative")
            msg["From"] = f"{smtp_cfg['from_name']} <{smtp_cfg['from_email']}>"
            msg["To"] = f"{to_name} <{to_email}>" if to_name else to_email
            msg["Subject"] = subject
            msg["Date"] = formatdate(localtime=True)
            msg["Message-ID"] = make_msgid(domain=smtp_cfg["from_email"].split("@")[-1])
            msg["X-Mailer"] = "BulkMailer-Pro/1.0"

            if body_plain:
                msg.attach(MIMEText(body_plain, "plain", "utf-8"))
            if body_html:
                msg.attach(MIMEText(body_html, "html", "utf-8"))

            with smtplib.SMTP(smtp_cfg["host"], int(smtp_cfg["port"]), timeout=30) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(smtp_cfg["from_email"], smtp_cfg["password"])
                server.sendmail(smtp_cfg["from_email"], to_email, msg.as_string())
            return True

        except Exception as e:
            if attempt == 0:
                time.sleep(5)   # wait before retry
                add_log(f"Retry for {to_email} after error: {e}", "warn")
            else:
                add_log(f"FAILED {to_email}: {e}", "error")
                return False
    return False


def campaign_worker(smtp_cfg: dict, leads: list,
                    subject_tpl: str, body_html_tpl: str,
                    body_plain_tpl: str, gap_seconds: int):
    """Background thread that sends emails one by one."""
    with campaign_lock:
        campaign["running"] = True
        campaign["paused"] = False
        campaign["stop_flag"] = False
        campaign["status"] = "running"
        campaign["sent"] = 0
        campaign["failed"] = 0
        campaign["skipped"] = 0
        campaign["total"] = len(leads)
        campaign["logs"] = []

    add_log(f"Campaign started — {len(leads)} leads", "info")
    push_progress()

    for i, row in enumerate(leads):
        # ── Check stop ────────────────────────────────
        if campaign["stop_flag"]:
            add_log("Campaign stopped by user.", "warn")
            break

        # ── Handle pause ──────────────────────────────
        while campaign["paused"] and not campaign["stop_flag"]:
            time.sleep(1)

        if campaign["stop_flag"]:
            add_log("Campaign stopped by user.", "warn")
            break

        # ── Daily limit check ─────────────────────────
        reset_daily_if_needed()
        if campaign["daily_sent"] >= DAILY_LIMIT:
            add_log(f"Daily limit ({DAILY_LIMIT}) reached. Stopping.", "error")
            break

        to_email = str(row.get("email", "")).strip()
        to_name = str(row.get("name", "")).strip()

        if not to_email or "@" not in to_email:
            campaign["skipped"] += 1
            add_log(f"Skipped invalid email: {to_email}", "warn")
            push_progress()
            continue

        campaign["current_email"] = to_email

        subject = render_template_string(subject_tpl, row)
        body_html = render_template_string(body_html_tpl, row)
        body_plain = render_template_string(body_plain_tpl, row)

        add_log(f"Sending to {to_name} <{to_email}>...", "info")
        success = send_single_email(smtp_cfg, to_email, to_name,
                                    subject, body_html, body_plain)

        if success:
            campaign["sent"] += 1
            campaign["daily_sent"] += 1
            add_log(f"✓ Sent to {to_email}", "success")
        else:
            campaign["failed"] += 1

        push_progress()

        # ── Gap between emails (skip gap after last) ──
        if i < len(leads) - 1 and not campaign["stop_flag"]:
            jitter = random.randint(-30, 30)
            actual_gap = max(30, gap_seconds + jitter)
            add_log(f"Waiting {actual_gap}s before next email…", "info")

            # Sleep in small chunks so pause/stop is responsive
            for _ in range(actual_gap):
                if campaign["stop_flag"]:
                    break
                while campaign["paused"] and not campaign["stop_flag"]:
                    time.sleep(1)
                time.sleep(1)

    with campaign_lock:
        campaign["running"] = False
        campaign["paused"] = False
        campaign["current_email"] = ""
        if campaign["stop_flag"]:
            campaign["status"] = "stopped"
        else:
            campaign["status"] = "done"

    add_log(f"Campaign finished. Sent: {campaign['sent']} | Failed: {campaign['failed']} | Skipped: {campaign['skipped']}", "info")
    push_progress()


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/upload", methods=["POST"])
def upload():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "No file selected"}), 400

    try:
        fname = f.filename.lower()
        if fname.endswith(".csv"):
            df = pd.read_csv(f)
        elif fname.endswith((".xlsx", ".xls")):
            df = pd.read_excel(f)
        else:
            return jsonify({"error": "Only .csv or .xlsx files supported"}), 400

        # Normalise column names
        df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]

        if "email" not in df.columns:
            return jsonify({"error": "Spreadsheet must have an 'email' column"}), 400

        if "name" not in df.columns:
            df["name"] = ""

        # Fill NaN
        df = df.fillna("")
        leads = df.to_dict(orient="records")

        campaign["leads"] = leads
        preview = leads[:3]
        cols = list(df.columns)

        return jsonify({
            "total": len(leads),
            "columns": cols,
            "preview": preview,
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/start", methods=["POST"])
def start():
    if campaign["running"]:
        return jsonify({"error": "Campaign already running"}), 400

    data = request.json or {}
    leads = campaign.get("leads", [])
    if not leads:
        return jsonify({"error": "No leads loaded. Upload a spreadsheet first."}), 400

    required = ["host", "port", "from_email", "from_name", "password",
                "subject", "body_html"]
    for field in required:
        if not data.get(field, "").strip():
            return jsonify({"error": f"Missing required field: {field}"}), 400

    smtp_cfg = {
        "host": data["host"],
        "port": data["port"],
        "from_email": data["from_email"],
        "from_name": data["from_name"],
        "password": data["password"],
    }

    gap_seconds = int(data.get("gap_seconds", 180))
    subject_tpl = data["subject"]
    body_html_tpl = data["body_html"]
    body_plain_tpl = data.get("body_plain", "")

    t = threading.Thread(
        target=campaign_worker,
        args=(smtp_cfg, leads, subject_tpl, body_html_tpl, body_plain_tpl, gap_seconds),
        daemon=True,
    )
    t.start()

    return jsonify({"status": "started"})


@app.route("/pause", methods=["POST"])
def pause():
    if not campaign["running"]:
        return jsonify({"error": "No campaign running"}), 400
    campaign["paused"] = not campaign["paused"]
    campaign["status"] = "paused" if campaign["paused"] else "running"
    push_progress()
    return jsonify({"paused": campaign["paused"]})


@app.route("/stop", methods=["POST"])
def stop():
    campaign["stop_flag"] = True
    return jsonify({"status": "stopping"})


@app.route("/status")
def status():
    return jsonify({
        "running": campaign["running"],
        "paused": campaign["paused"],
        "status": campaign["status"],
        "sent": campaign["sent"],
        "failed": campaign["failed"],
        "skipped": campaign["skipped"],
        "total": campaign["total"],
        "daily_sent": campaign["daily_sent"],
        "daily_limit": DAILY_LIMIT,
        "current_email": campaign["current_email"],
    })


@app.route("/events")
def events():
    """SSE endpoint — streams real-time updates to browser."""
    def generate():
        # Send current state immediately
        yield f"data: {json.dumps({'type': 'progress', 'sent': campaign['sent'], 'failed': campaign['failed'], 'skipped': campaign['skipped'], 'total': campaign['total'], 'status': campaign['status'], 'current_email': campaign['current_email'], 'daily_sent': campaign['daily_sent']})}\n\n"

        while True:
            try:
                event = event_queue.get(timeout=15)
                yield f"data: {json.dumps(event)}\n\n"
            except queue.Empty:
                # Heartbeat
                yield f"data: {json.dumps({'type': 'ping'})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.route("/preview", methods=["POST"])
def preview():
    data = request.json or {}
    leads = campaign.get("leads", [])
    if not leads:
        return jsonify({"error": "No leads loaded"}), 400

    row = leads[0]
    subject = render_template_string(data.get("subject", ""), row)
    body_html = render_template_string(data.get("body_html", ""), row)
    body_plain = render_template_string(data.get("body_plain", ""), row)

    return jsonify({
        "to": f"{row.get('name', '')} <{row.get('email', '')}>",
        "subject": subject,
        "body_html": body_html,
        "body_plain": body_plain,
    })


@app.route("/clear_leads", methods=["POST"])
def clear_leads():
    if campaign["running"]:
        return jsonify({"error": "Cannot clear while campaign is running"}), 400
    campaign["leads"] = []
    campaign["status"] = "idle"
    campaign["sent"] = campaign["failed"] = campaign["skipped"] = campaign["total"] = 0
    campaign["logs"] = []
    return jsonify({"status": "cleared"})


@app.route("/fetch_sheets", methods=["POST"])
def fetch_sheets():
    """Fetch leads directly from a public Google Sheet."""
    data = request.json or {}
    url = data.get("url", "").strip()

    if not url:
        return jsonify({"error": "Please provide a Google Sheets URL"}), 400

    # Extract sheet ID from URL
    match = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", url)
    if not match:
        return jsonify({"error": "Invalid Google Sheets URL. Copy the full URL from your browser."}), 400

    sheet_id = match.group(1)

    # Extract gid (tab id) if present
    gid_match = re.search(r"[#&?]gid=(\d+)", url)
    gid = gid_match.group(1) if gid_match else "0"

    # Build CSV export URL
    csv_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"

    try:
        req = urllib.request.Request(csv_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()

        df = pd.read_csv(io.BytesIO(raw))

        # Normalise columns
        df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]

        if "email" not in df.columns:
            return jsonify({
                "error": f"No 'email' column found. Columns in your sheet: {', '.join(df.columns)}"
            }), 400

        if "name" not in df.columns:
            df["name"] = ""

        df = df.fillna("")
        leads = df.to_dict(orient="records")
        campaign["leads"] = leads

        return jsonify({
            "total": len(leads),
            "columns": list(df.columns),
            "preview": leads[:3],
        })

    except urllib.error.HTTPError as e:
        if e.code == 401 or e.code == 403:
            return jsonify({
                "error": "Sheet is private. Please set it to 'Anyone with the link can VIEW' and try again."
            }), 400
        return jsonify({"error": f"HTTP error {e.code}: {e.reason}"}), 400
    except Exception as e:
        return jsonify({"error": f"Failed to fetch sheet: {str(e)}"}), 500


if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0", port=5000, threaded=True)

