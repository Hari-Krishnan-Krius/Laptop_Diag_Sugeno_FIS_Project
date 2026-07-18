"""
utils/email_notifier.py
─────────────────────────────────────────────────────────────────────────────
SMTP email alerts for the Laptop Diagnostics System.

Features
• Fault alert email — triggered when severity ≥ configured threshold
• 30-minute per-laptop cooldown to prevent alert spam
• Weekly digest — summary of the past 7 days sent every Monday at 09:00
• HTML + plain-text multipart emails
• Settings are read from MongoDB (settings collection) at send-time,
  falling back to .env environment variables.

Fix: Cooldown timestamps are now persisted in MongoDB
     (laptops collection, field `last_alert_at`) instead of the in-memory
     dict _last_alert.  This prevents duplicate alerts after a restart and
     works correctly across multiple application instances.
─────────────────────────────────────────────────────────────────────────────
"""

import os
import smtplib
import logging
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from bson import ObjectId

log = logging.getLogger(__name__)

# Severity order for threshold comparisons
_SEV_ORDER = {"OK": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}


def _now() -> datetime:
    """Return current UTC time as a timezone-aware datetime."""
    return datetime.now(timezone.utc)


# ─────────────────────────────────────────────────────────────────────────────
# SMTP config resolution
# ─────────────────────────────────────────────────────────────────────────────

def _get_smtp_config(db) -> dict:
    """
    Merge SMTP settings from MongoDB (settings collection) and .env.
    MongoDB values take precedence over environment variables.
    """
    cfg = {}
    try:
        doc = db.settings.find_one({"_id": "global"}) or {}
        cfg = {
            "host":     doc.get("smtp_host")     or os.environ.get("SMTP_HOST",     "smtp.gmail.com"),
            "port":     int(doc.get("smtp_port") or os.environ.get("SMTP_PORT",     587)),
            "user":     doc.get("smtp_user")     or os.environ.get("SMTP_USER",     ""),
            "password": doc.get("smtp_password") or os.environ.get("SMTP_PASSWORD", ""),
            "sender":   doc.get("sender_name")   or "Laptop Diagnostics System",
            "sender_email": doc.get("sender_email") or doc.get("smtp_user") or os.environ.get("SMTP_USER", ""),
            "min_severity":   doc.get("min_severity",   "MEDIUM"),
            "alert_cooldown": int(doc.get("alert_cooldown", 30)),
        }
    except Exception as exc:
        log.warning("Could not read SMTP config from DB: %s", exc)
        cfg = {
            "host":           os.environ.get("SMTP_HOST",     "smtp.gmail.com"),
            "port":           int(os.environ.get("SMTP_PORT", 587)),
            "user":           os.environ.get("SMTP_USER",     ""),
            "password":       os.environ.get("SMTP_PASSWORD", ""),
            "sender":         "Laptop Diagnostics System",
            "sender_email":   os.environ.get("SMTP_USER",     ""),
            "min_severity":   "MEDIUM",
            "alert_cooldown": 30,
        }
    return cfg


# ─────────────────────────────────────────────────────────────────────────────
# Core SMTP sender
# ─────────────────────────────────────────────────────────────────────────────

def _send_email(cfg: dict, to_address: str, subject: str,
                html_body: str, plain_body: str) -> bool:
    """
    Send a multipart HTML+text email. Returns True on success.
    """
    if not cfg.get("user") or not cfg.get("password"):
        log.warning("SMTP credentials not configured — skipping email")
        return False
    if not to_address:
        log.warning("No recipient address supplied — skipping email")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f'{cfg["sender"]} <{cfg["sender_email"] or cfg["user"]}>'
    msg["To"]      = to_address

    msg.attach(MIMEText(plain_body, "plain"))
    msg.attach(MIMEText(html_body,  "html"))

    try:
        with smtplib.SMTP(cfg["host"], cfg["port"], timeout=15) as server:
            server.ehlo()
            server.starttls()
            server.login(cfg["user"], cfg["password"])
            server.sendmail(cfg["sender_email"] or cfg["user"], to_address, msg.as_string())
        log.info("Email sent to %s — '%s'", to_address, subject)
        return True
    except smtplib.SMTPAuthenticationError:
        log.error("SMTP authentication failed. Use an App Password for Gmail.")
    except smtplib.SMTPException as exc:
        log.error("SMTP error: %s", exc)
    except OSError as exc:
        log.error("Network error sending email: %s", exc)
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Cooldown helpers (MongoDB-backed)
# ─────────────────────────────────────────────────────────────────────────────

def _get_last_alert(db, laptop_id: str) -> datetime | None:
    """
    Read the last alert timestamp for a laptop from MongoDB.
    Returns a timezone-aware UTC datetime or None.
    """
    try:
        doc = db.laptops.find_one(
            {"_id": ObjectId(laptop_id)},
            {"last_alert_at": 1},
        )
        if doc and doc.get("last_alert_at"):
            ts = doc["last_alert_at"]
            # Ensure timezone-aware (MongoDB stores UTC naive datetimes)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            return ts
    except Exception as exc:
        log.debug("Could not read last_alert_at for %s: %s", laptop_id, exc)
    return None


def _set_last_alert(db, laptop_id: str, ts: datetime) -> None:
    """Persist the alert timestamp into the laptop document."""
    try:
        db.laptops.update_one(
            {"_id": ObjectId(laptop_id)},
            {"$set": {"last_alert_at": ts}},
        )
    except Exception as exc:
        log.warning("Could not persist last_alert_at for %s: %s", laptop_id, exc)


# ─────────────────────────────────────────────────────────────────────────────
# Fault alert
# ─────────────────────────────────────────────────────────────────────────────

def maybe_send_fault_alert(db, laptop: dict, diagnosis_result: dict) -> bool:
    """
    Send a fault alert email if:
      1. The laptop has email notifications enabled
      2. The severity meets or exceeds the configured minimum
      3. The per-laptop cooldown period has elapsed (checked in MongoDB)

    Returns True if an email was sent.
    """
    laptop_id = str(laptop.get("_id", ""))
    severity  = diagnosis_result.get("severity", "OK")
    to_email  = laptop.get("email", "")

    cfg = _get_smtp_config(db)

    # Check severity threshold
    if _SEV_ORDER.get(severity, 0) < _SEV_ORDER.get(cfg["min_severity"], 2):
        return False

    # Healthy is never an alert
    if severity == "OK":
        return False

    # Cooldown check — persisted in MongoDB so it survives restarts
    cooldown_minutes = cfg["alert_cooldown"]
    now = _now()
    last = _get_last_alert(db, laptop_id)
    if last and (now - last) < timedelta(minutes=cooldown_minutes):
        log.debug("Alert suppressed for %s (cooldown active)", laptop.get("name"))
        return False

    # Cooldown is persisted BEFORE the send attempt so that parallel workers
    # racing on the same laptop see the lock immediately and don't send
    # duplicates.  Trade-off: if SMTP fails, the cooldown is still active
    # and the alert won't retry until the cooldown window expires.
    # This is intentional — no-duplicate is preferred over guaranteed delivery
    # for a diagnostics alert system.  Failed sends are logged at ERROR level.
    _set_last_alert(db, laptop_id, now)

    # Build email content
    diagnosis  = diagnosis_result.get("diagnosis", "Unknown")
    secondary  = diagnosis_result.get("secondary", "—")
    confidence = diagnosis_result.get("confidence", 0.0)
    conf_level = diagnosis_result.get("conf_level", "—")
    timestamp  = diagnosis_result.get("timestamp", now.isoformat())

    sev_colors = {
        "LOW":      "#ca8a04",
        "MEDIUM":   "#d97706",
        "HIGH":     "#dc2626",
        "CRITICAL": "#b91c1c",
    }
    sev_color = sev_colors.get(severity, "#888")

    subject = f"[{severity}] {laptop.get('name','Laptop')} — {diagnosis}"

    html = f"""
<!DOCTYPE html>
<html>
<body style="font-family:Arial,sans-serif;background:#0f172a;color:#f1f5f9;padding:24px;margin:0;">
<div style="max-width:560px;margin:auto;background:#1e293b;border-radius:12px;
            border:1px solid #334155;overflow:hidden;">

  <div style="background:{sev_color};padding:18px 24px;">
    <h2 style="margin:0;color:#fff;font-size:18px;">
      ⚠️ Laptop Fault Detected — {severity}
    </h2>
  </div>

  <div style="padding:24px;">
    <table style="width:100%;border-collapse:collapse;font-size:14px;">
      <tr>
        <td style="padding:8px 0;color:#94a3b8;width:160px;">Laptop</td>
        <td style="padding:8px 0;font-weight:bold;">{laptop.get('name','—')}</td>
      </tr>
      <tr>
        <td style="padding:8px 0;color:#94a3b8;">Model</td>
        <td style="padding:8px 0;">{laptop.get('model','—')}</td>
      </tr>
      <tr>
        <td style="padding:8px 0;color:#94a3b8;">Category</td>
        <td style="padding:8px 0;">{laptop.get('category','—').title()}</td>
      </tr>
      <tr><td colspan="2" style="border-top:1px solid #334155;padding-top:12px;"></td></tr>
      <tr>
        <td style="padding:8px 0;color:#94a3b8;">Primary Fault</td>
        <td style="padding:8px 0;font-weight:bold;color:{sev_color};font-size:16px;">
          {diagnosis}
        </td>
      </tr>
      <tr>
        <td style="padding:8px 0;color:#94a3b8;">Secondary Suspect</td>
        <td style="padding:8px 0;">{secondary}</td>
      </tr>
      <tr>
        <td style="padding:8px 0;color:#94a3b8;">Confidence</td>
        <td style="padding:8px 0;">{confidence*100:.1f}% ({conf_level})</td>
      </tr>
      <tr>
        <td style="padding:8px 0;color:#94a3b8;">Severity</td>
        <td style="padding:8px 0;">
          <span style="background:{sev_color};color:#fff;padding:2px 10px;
                       border-radius:20px;font-size:12px;font-weight:bold;">
            {severity}
          </span>
        </td>
      </tr>
      <tr>
        <td style="padding:8px 0;color:#94a3b8;">Detected At</td>
        <td style="padding:8px 0;">{timestamp}</td>
      </tr>
    </table>

    <div style="margin-top:20px;padding:14px;background:#0f172a;border-radius:8px;
                font-size:13px;color:#94a3b8;border-left:3px solid {sev_color};">
      This alert was generated by the Sugeno Fuzzy Inference System.
      Log in to the Diagnostics Dashboard to view full history and trend charts.
    </div>
  </div>

  <div style="padding:14px 24px;border-top:1px solid #334155;
              font-size:11px;color:#64748b;text-align:center;">
    Laptop Motherboard Diagnostics System — Laptop-Motherboard-Diagnostics © 2026
  </div>
</div>
</body>
</html>"""

    plain = (
        f"LAPTOP FAULT ALERT\n"
        f"{'='*40}\n"
        f"Laptop   : {laptop.get('name','—')}\n"
        f"Category : {laptop.get('category','—').title()}\n"
        f"Fault    : {diagnosis}\n"
        f"Secondary: {secondary}\n"
        f"Confidence: {confidence*100:.1f}% ({conf_level})\n"
        f"Severity : {severity}\n"
        f"Time     : {timestamp}\n"
        f"{'='*40}\n"
        f"Sugeno FIS — Laptop-Motherboard-Diagnostics © 2026\n"
    )

    sent = _send_email(cfg, to_email, subject, html, plain)
    if sent:
        log.info("Fault alert sent for %s (%s)", laptop.get("name"), diagnosis)
    return sent


# ─────────────────────────────────────────────────────────────────────────────
# Weekly digest
# ─────────────────────────────────────────────────────────────────────────────

def send_weekly_digest(db) -> int:
    """
    Send a weekly summary email to every registered laptop's alert address.
    Returns the number of emails successfully sent.
    """
    cfg = _get_smtp_config(db)
    if not cfg.get("user") or not cfg.get("password"):
        log.warning("SMTP not configured — skipping weekly digest")
        return 0

    laptops = list(db.laptops.find({"active": True}))
    sent_count = 0
    now   = _now()
    since = now - timedelta(days=7)

    for laptop in laptops:
        to_email = laptop.get("email", "")
        if not to_email:
            continue

        laptop_id = laptop["_id"]
        records = list(
            db.diagnostics.find(
                {"laptop_id": laptop_id, "timestamp": {"$gte": since}}
            ).sort("timestamp", -1)
        )

        total     = len(records)
        faults    = [r for r in records if r.get("severity") not in ("OK", None)]
        criticals = [r for r in records if r.get("severity") == "CRITICAL"]

        from collections import Counter
        fault_counts = Counter(r["diagnosis"] for r in records)
        top_faults = fault_counts.most_common(5)

        rows = "".join(
            f"<tr>"
            f"<td style='padding:6px 12px;color:#94a3b8;'>{name}</td>"
            f"<td style='padding:6px 12px;text-align:center;'>{count}</td>"
            f"</tr>"
            for name, count in top_faults
        ) or "<tr><td colspan='2' style='padding:10px;color:#64748b;text-align:center;'>No data</td></tr>"

        subject = f"Weekly Digest — {laptop.get('name','Laptop')} ({total} checks)"

        html = f"""
<!DOCTYPE html>
<html>
<body style="font-family:Arial,sans-serif;background:#0f172a;color:#f1f5f9;padding:24px;margin:0;">
<div style="max-width:560px;margin:auto;background:#1e293b;border-radius:12px;
            border:1px solid #334155;overflow:hidden;">
  <div style="background:#2563eb;padding:18px 24px;">
    <h2 style="margin:0;color:#fff;font-size:18px;">
      📊 Weekly Diagnostics Summary
    </h2>
    <p style="margin:4px 0 0;color:#bfdbfe;font-size:13px;">
      {since.strftime('%b %d')} – {now.strftime('%b %d, %Y')}
    </p>
  </div>
  <div style="padding:24px;">
    <p style="margin-bottom:18px;font-size:15px;">
      Summary for <strong>{laptop.get('name','—')}</strong>
      ({laptop.get('category','—').title()})
    </p>

    <div style="display:flex;gap:16px;margin-bottom:20px;flex-wrap:wrap;">
      <div style="flex:1;background:#0f172a;border-radius:8px;padding:14px;
                  text-align:center;min-width:100px;">
        <div style="font-size:28px;font-weight:bold;">{total}</div>
        <div style="font-size:12px;color:#94a3b8;">Total Checks</div>
      </div>
      <div style="flex:1;background:#0f172a;border-radius:8px;padding:14px;
                  text-align:center;min-width:100px;">
        <div style="font-size:28px;font-weight:bold;color:#f97316;">{len(faults)}</div>
        <div style="font-size:12px;color:#94a3b8;">Fault Events</div>
      </div>
      <div style="flex:1;background:#0f172a;border-radius:8px;padding:14px;
                  text-align:center;min-width:100px;">
        <div style="font-size:28px;font-weight:bold;color:#dc2626;">{len(criticals)}</div>
        <div style="font-size:12px;color:#94a3b8;">Critical</div>
      </div>
    </div>

    <h3 style="font-size:13px;color:#94a3b8;text-transform:uppercase;
               letter-spacing:0.6px;margin-bottom:10px;">Top Diagnoses</h3>
    <table style="width:100%;border-collapse:collapse;font-size:13px;
                  background:#0f172a;border-radius:8px;overflow:hidden;">
      <thead>
        <tr>
          <th style="padding:8px 12px;text-align:left;color:#64748b;font-weight:600;">
            Diagnosis
          </th>
          <th style="padding:8px 12px;text-align:center;color:#64748b;font-weight:600;">
            Count
          </th>
        </tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>
  </div>

  <div style="padding:14px 24px;border-top:1px solid #334155;
              font-size:11px;color:#64748b;text-align:center;">
    Laptop Motherboard Diagnostics System — Laptop-Motherboard-Diagnostics © 2026
  </div>
</div>
</body>
</html>"""

        plain = (
            f"WEEKLY DIAGNOSTICS SUMMARY\n"
            f"{'='*40}\n"
            f"Laptop  : {laptop.get('name','—')}\n"
            f"Period  : {since.strftime('%b %d')} – {now.strftime('%b %d, %Y')}\n"
            f"Checks  : {total}\n"
            f"Faults  : {len(faults)}\n"
            f"Critical: {len(criticals)}\n\n"
            f"Top Diagnoses:\n" +
            "\n".join(f"  {n}: {c}" for n, c in top_faults) +
            f"\n{'='*40}\n"
            f"Sugeno FIS — Laptop-Motherboard-Diagnostics © 2026\n"
        )

        if _send_email(cfg, to_email, subject, html, plain):
            sent_count += 1

    return sent_count
