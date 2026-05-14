from __future__ import annotations

import smtplib
from email.message import EmailMessage

from app import db
from app.config import settings
from app.models import ProfitabilityDecision, SolarLeadIntake


def booking_link(lead_id: str) -> str:
    base = (settings.booking_base_url or f"{settings.public_base_url}/book").rstrip("/")
    return f"{base}/{lead_id}"


def _send_email(recipient: str, subject: str, body: str, lead_id: str | None) -> dict:
    if not (settings.smtp_host and settings.smtp_user and settings.smtp_password):
        db.add_email_event(
            lead_id=lead_id,
            recipient=recipient,
            subject=subject,
            body=body,
            status="demo_logged",
            provider_response="SMTP credentials missing; email stored for demo.",
        )
        return {"sent": False, "status": "demo_logged", "subject": subject, "body": body}

    message = EmailMessage()
    message["From"] = settings.from_email
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body)
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as smtp:
        smtp.starttls()
        smtp.login(settings.smtp_user, settings.smtp_password)
        smtp.send_message(message)
    db.add_email_event(
        lead_id=lead_id,
        recipient=recipient,
        subject=subject,
        body=body,
        status="sent",
        provider_response="SMTP sent",
    )
    return {"sent": True, "status": "sent", "subject": subject}


def send_decision_email(
    lead: SolarLeadIntake,
    profitability: ProfitabilityDecision,
) -> dict:
    if profitability.decision == "PURSUE":
        subject = "Ihr Solar-Projekt sieht wirtschaftlich interessant aus"
        body = (
            f"Hallo {lead.name},\n\n"
            "vielen Dank für Ihre Angaben. Unser System hat das Projekt geprüft und "
            "es sieht für eine vertiefte Beratung wirtschaftlich interessant aus.\n\n"
            f"Bitte wählen Sie hier einen passenden Termin: {booking_link(lead.lead_id or '')}\n\n"
            "Im Termin klären wir Dachdetails, Speicheroptionen und die nächste Angebotstiefe.\n\n"
            "Viele Grüße\nSolar Lead OS"
        )
    elif profitability.decision == "NURTURE":
        subject = "Noch ein paar Details zu Ihrem Solar-Projekt"
        body = (
            f"Hallo {lead.name},\n\n"
            "danke für Ihre Anfrage. Für eine belastbare Einschätzung fehlen noch ein paar Details. "
            f"Sie können diese hier ergänzen: {booking_link(lead.lead_id or '')}\n\n"
            "Viele Grüße\nSolar Lead OS"
        )
    else:
        subject = "Ihre Solar-Anfrage"
        body = (
            f"Hallo {lead.name},\n\n"
            "vielen Dank für Ihre Angaben. Nach der ersten Prüfung passt das Projekt aktuell "
            "leider nicht gut zu unseren wirtschaftlichen Kriterien. Wenn sich Eigentümerstatus, "
            "Budget oder Projektumfang ändern, melden Sie sich gern wieder.\n\n"
            "Viele Grüße\nSolar Lead OS"
        )
    return _send_email(str(lead.email), subject, body, lead.lead_id)


def notify_staff(lead_id: str, message: str) -> dict:
    db.add_staff_notification(
        lead_id=lead_id,
        channel="email_or_demo_log",
        message=message,
        status="created",
    )
    return _send_email(settings.staff_notify_email, f"Solar Lead closed: {lead_id}", message, lead_id)
