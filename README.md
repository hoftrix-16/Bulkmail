# BulkMailer Pro — Hostinger SMTP

One-page bulk email sender with real-time progress, built with Python Flask.

---

## ✅ Features
- Upload leads via **Excel (.xlsx) or CSV**
- SMTP via **Hostinger Webmail** (smtp.hostinger.com:587)
- **Configurable gap** between emails: 1–15 minutes + ±30s randomization
- Real-time **live log** and **progress bar** via SSE
- **Pause / Resume / Stop** campaign any time
- **Daily limit**: 2000 emails/day with warning at 1800
- **Placeholder support**: `{{name}}`, `{{email}}`, `{{company}}`, etc.
- **Preview** first email before sending
- 1 automatic **retry** on failure
- Crash-proof — errors are logged, campaign continues

---

## 🚀 Setup & Run

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Run the app
```bash
python app.py
```

### 3. Open in browser
```
http://localhost:5000
```

---

## 📊 Spreadsheet Format

| name       | email               | company    |
|------------|---------------------|------------|
| Priya Shah | priya@example.com   | TechCorp   |
| Rahul Mehta| rahul@example.com   | StartupXYZ |

- **Required**: `email` column (case-insensitive)
- **Optional**: `name`, `company`, `phone` — usable as `{{name}}`, `{{company}}` in templates

---

## ✉️ Email Template Placeholders

Use `{{column_name}}` in both **Subject** and **Body**:

```
Subject: Hello {{name}}, special offer from us!
Body:    Hi {{name}}, we noticed {{company}} could benefit from...
```

---

## ⚙️ Hostinger SMTP Settings

| Field       | Value                  |
|-------------|------------------------|
| SMTP Host   | smtp.hostinger.com     |
| SMTP Port   | 587                    |
| Username    | your@yourdomain.com    |
| Password    | your webmail password  |

---

## 📌 Notes
- Use **3–5 minute gaps** to avoid spam filters
- Stay under **500 emails/day** for fresh domains
- Always warm up a new domain gradually

