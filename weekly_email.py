"""Email delivery helper for weekly reports."""

import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


def send_report_email(cfg: dict, html: str, today: str, report_meta: dict) -> bool:
    if not cfg.get("email", {}).get("enabled", False):
        print("Email 發送已關閉，略過寄信")
        return False
    smtp_user = os.environ.get("SMTP_USERNAME", "").strip()
    smtp_pass = os.environ.get("SMTP_PASSWORD", "").strip()
    report_to = os.environ.get("REPORT_EMAIL_TO", "").strip()
    if not smtp_user or not smtp_pass or not report_to:
        print("⚠️  未設定 SMTP_USERNAME / SMTP_PASSWORD / REPORT_EMAIL_TO，跳過發信")
        return False
    ec = cfg["email"]
    msg = MIMEMultipart("alternative")
    msg["Subject"] = ec["subject"].format(
        date=today,
        week=report_meta["week"],
        week_key=report_meta["week_key"],
    )
    msg["From"] = smtp_user
    msg["To"] = report_to
    msg.attach(MIMEText(html, "html", "utf-8"))
    smtp = smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30)
    try:
        recipients = [addr.strip() for addr in report_to.split(",") if addr.strip()]
        smtp.login(smtp_user, smtp_pass)
        smtp.sendmail(smtp_user, recipients, msg.as_string())
        smtp.quit()
    except Exception:
        smtp.close()
        raise
    return True
