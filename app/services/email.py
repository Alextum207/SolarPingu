from __future__ import annotations

import smtplib
import html as html_lib
from email.message import EmailMessage
from typing import Any
from urllib.parse import quote

from app import db
from app.config import settings
from app.models import ProfitabilityDecision, SolarLeadIntake


def booking_link(lead_id: str) -> str:
    base = (settings.booking_base_url or f"{settings.public_base_url}/book").rstrip("/")
    return f"{base}/{lead_id}"


def _booking_link_with_mode(lead_id: str, mode: str) -> str:
    return f"{booking_link(lead_id)}?mode={quote(mode)}"


def _send_email(
    recipient: str,
    subject: str,
    body: str,
    lead_id: str | None,
    html_body: str | None = None,
    attachments: list[dict[str, Any]] | None = None,
) -> dict:
    attachments = attachments or []
    if not (settings.smtp_host and settings.smtp_user and settings.smtp_password):
        db.add_email_event(
            lead_id=lead_id,
            recipient=recipient,
            subject=subject,
            body=body,
            status="demo_logged",
            provider_response="SMTP credentials missing; email stored for demo.",
        )
        return {
            "sent": False,
            "status": "demo_logged",
            "subject": subject,
            "body": body,
            "html_body": html_body,
            "attachments": [attachment.get("filename") for attachment in attachments],
        }

    message = EmailMessage()
    message["From"] = settings.from_email
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body)
    if html_body:
        message.add_alternative(html_body, subtype="html")
    for attachment in attachments:
        content = attachment.get("content")
        if not isinstance(content, bytes):
            continue
        content_type = str(attachment.get("content_type") or "application/octet-stream")
        maintype, _, subtype = content_type.partition("/")
        message.add_attachment(
            content,
            maintype=maintype or "application",
            subtype=subtype or "octet-stream",
            filename=str(attachment.get("filename") or "call-audio.mp3"),
        )
    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as smtp:
            smtp.starttls()
            smtp.login(settings.smtp_user, settings.smtp_password)
            smtp.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        db.add_email_event(
            lead_id=lead_id,
            recipient=recipient,
            subject=subject,
            body=body,
            status="demo_logged",
            provider_response=f"SMTP failed; email stored for demo: {exc}",
        )
        return {
            "sent": False,
            "status": "demo_logged",
            "subject": subject,
            "body": body,
            "html_body": html_body,
        }
    db.add_email_event(
        lead_id=lead_id,
        recipient=recipient,
        subject=subject,
        body=body,
        status="sent",
        provider_response="SMTP sent",
    )
    return {
        "sent": True,
        "status": "sent",
        "subject": subject,
        "html_body": html_body,
        "attachments": [attachment.get("filename") for attachment in attachments],
    }


def send_decision_email(
    lead: SolarLeadIntake,
    profitability: ProfitabilityDecision,
) -> dict:
    if profitability.decision == "PURSUE":
        link = booking_link(lead.lead_id or "")
        subject = "Ihre Solar-Anfrage ist eingegangen"
        body = (
            f"Hallo {lead.name},\n\n"
            "vielen Dank, Ihre Daten sind bei uns eingegangen. Wir pruefen Ihre Angaben "
            "und moechten im naechsten Schritt kurz mit Ihnen klaeren, was fuer Sie am besten passt.\n\n"
            f"Hier koennen Sie einen freien Termin auswaehlen: {link}\n\n"
            "Sie koennen zwischen einem Online-Termin und einem Vor-Ort-Termin mit einer echten Person waehlen.\n\n"
            "Viele Gruesse\nSolar Lead OS"
        )
        html_body = _customer_booking_html(
            headline="Ihre Daten sind eingegangen",
            intro=(
                "Vielen Dank fuer Ihre Angaben. Waehlen Sie jetzt einfach einen passenden "
                "freien Termin aus."
            ),
            lead=lead,
            primary_url=link,
        )
        return _send_email(str(lead.email), subject, body, lead.lead_id, html_body)
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


def send_customer_booking_followup(
    lead: SolarLeadIntake,
    *,
    summary: str | None = None,
    should_continue: bool = True,
) -> dict:
    del summary
    if not should_continue:
        subject = "Ihre Solar-Anfrage"
        body = (
            f"Hallo {lead.name},\n\n"
            "danke fuer das Gespraech und Ihre Angaben. Nach der aktuellen Einschaetzung "
            "moechten wir an dieser Stelle noch keinen weiteren Termin vorschlagen.\n\n"
            "Falls sich bei Budget, Zeitplan oder Projektumfang etwas aendert, koennen "
            "wir die Anfrage gern erneut pruefen.\n\n"
            "Viele Gruesse\nSolar Lead OS"
        )
        html_body = _customer_status_html(
            headline="Danke fuer Ihre Anfrage",
            intro=(
                "Danke fuer das Gespraech und Ihre Angaben. Nach der aktuellen "
                "Einschaetzung moechten wir an dieser Stelle noch keinen weiteren "
                "Termin vorschlagen."
            ),
            lead=lead,
        )
        return _send_email(str(lead.email), subject, body, lead.lead_id, html_body)

    link = booking_link(lead.lead_id or "")
    subject = "Naechster Schritt: Ihren Solar-Termin buchen"
    body = (
        f"Hallo {lead.name},\n\n"
        "danke fuer das Gespraech. Wir moechten mit Ihnen weiterarbeiten und als "
        "naechsten Schritt einen Termin mit einer echten Person vereinbaren.\n\n"
        f"Hier koennen Sie einen freien Termin auswaehlen: {link}\n\n"
        "Sie koennen online starten oder direkt einen Vor-Ort-Termin fuer die konkrete Planung buchen.\n\n"
    )
    body += "Viele Gruesse\nSolar Lead OS"
    html_body = _customer_booking_html(
        headline="Wir moechten mit Ihnen weiterarbeiten",
        intro=(
            "Danke fuer das Gespraech. Buchen Sie jetzt einen passenden Termin: online "
            "oder als Vor-Ort-Planung mit einer echten Person."
        ),
        lead=lead,
        primary_url=link,
    )
    return _send_email(str(lead.email), subject, body, lead.lead_id, html_body)


def send_installer_booking_request(
    *,
    lead: SolarLeadIntake,
    installer: dict[str, Any],
    selected_start: Any,
    selected_end: Any,
    mode: str,
    confirm_url: str,
    dashboard_url: str,
) -> dict:
    recipient = str(installer.get("email") or installer.get("calendar_id") or settings.staff_notify_email)
    mode_label = "Online-Termin" if mode == "online" else "Vor-Ort-Termin"
    subject = f"Bitte Termin final bestaetigen: {lead.name}"
    body = "\n".join(
        [
            f"Hallo {installer.get('name') or 'Team'},",
            "",
            f"der Kunde hat einen {mode_label} angefragt.",
            "",
            f"Kunde: {lead.name}",
            f"Telefon: {lead.phone}",
            f"Email: {lead.email}",
            f"Adresse: {lead.address}",
            f"Terminwunsch: {selected_start.strftime('%d.%m.%Y %H:%M Uhr')}",
            "",
            f"Dashboard mit allen Lead-Infos: {dashboard_url}",
            f"Final bestaetigen: {confirm_url}",
            "",
            "Bitte bestaetige den Termin final. Danach schicken wir dem Kunden automatisch die Bestaetigung.",
        ]
    )
    esc = html_lib.escape
    html_body = f"""<!doctype html>
<html>
  <body style="margin:0;background:#f4f7f2;font-family:Arial,sans-serif;color:#172018;">
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f4f7f2;padding:24px 0;">
      <tr><td align="center">
        <table role="presentation" width="680" cellspacing="0" cellpadding="0" style="width:680px;max-width:94%;background:#ffffff;border:1px solid #dbe6d6;border-radius:8px;overflow:hidden;">
          <tr><td style="padding:24px 28px;background:#15351f;color:#ffffff;">
            <h1 style="margin:0;font-size:24px;line-height:1.25;">Termin final bestaetigen</h1>
          </td></tr>
          <tr><td style="padding:24px 28px;">
            <p style="line-height:1.55;margin:0 0 16px;">Der Kunde hat einen <strong>{esc(mode_label)}</strong> angefragt.</p>
            <ul style="line-height:1.65;margin:0 0 20px;padding-left:20px;">
              <li>Kunde: {esc(lead.name)}</li>
              <li>Telefon: {esc(lead.phone)}</li>
              <li>Email: {esc(str(lead.email))}</li>
              <li>Adresse: {esc(lead.address)}</li>
              <li>Terminwunsch: {esc(selected_start.strftime('%d.%m.%Y %H:%M Uhr'))}</li>
            </ul>
            <p style="margin:0 0 12px;">
              <a href="{esc(confirm_url)}" style="display:inline-block;background:#1d6f42;color:#ffffff;text-decoration:none;font-weight:bold;padding:14px 18px;border-radius:6px;">Termin final bestaetigen</a>
            </p>
            <p style="margin:0;">
              <a href="{esc(dashboard_url)}" style="display:inline-block;border:1px solid #dbe6d6;color:#15351f;text-decoration:none;font-weight:bold;padding:13px 16px;border-radius:6px;">Lead im Dashboard ansehen</a>
            </p>
          </td></tr>
        </table>
      </td></tr>
    </table>
  </body>
</html>"""
    return _send_email(recipient, subject, body, lead.lead_id, html_body)


def send_customer_appointment_confirmed(
    *,
    lead: SolarLeadIntake,
    selected_start: Any,
    selected_end: Any,
    mode: str,
    installer_name: str,
) -> dict:
    mode_label = "Online-Termin" if mode == "online" else "Vor-Ort-Termin"
    subject = "Ihr Solar-Termin ist bestaetigt"
    body = "\n".join(
        [
            f"Hallo {lead.name},",
            "",
            "Ihr Termin wurde final bestaetigt.",
            "",
            f"Art: {mode_label}",
            f"Termin: {selected_start.strftime('%d.%m.%Y %H:%M Uhr')}",
            f"Ansprechpartner: {installer_name}",
            "",
            "Wir freuen uns auf das Gespraech.",
            "",
            "Viele Gruesse",
            "Solar Lead OS",
        ]
    )
    esc = html_lib.escape
    html_body = f"""<!doctype html>
<html>
  <body style="margin:0;background:#f4f7f2;font-family:Arial,sans-serif;color:#172018;">
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f4f7f2;padding:24px 0;">
      <tr><td align="center">
        <table role="presentation" width="620" cellspacing="0" cellpadding="0" style="width:620px;max-width:94%;background:#ffffff;border:1px solid #dbe6d6;border-radius:8px;overflow:hidden;">
          <tr><td style="padding:24px 28px;background:#15351f;color:#ffffff;">
            <h1 style="margin:0;font-size:24px;line-height:1.25;">Termin bestaetigt</h1>
          </td></tr>
          <tr><td style="padding:24px 28px;">
            <p style="line-height:1.55;margin:0 0 18px;">Hallo {esc(lead.name)}, Ihr Termin wurde final bestaetigt.</p>
            <ul style="line-height:1.65;margin:0 0 20px;padding-left:20px;">
              <li>Art: {esc(mode_label)}</li>
              <li>Termin: {esc(selected_start.strftime('%d.%m.%Y %H:%M Uhr'))}</li>
              <li>Ansprechpartner: {esc(installer_name)}</li>
            </ul>
            <p style="line-height:1.55;margin:0;">Wir freuen uns auf das Gespraech.</p>
          </td></tr>
        </table>
      </td></tr>
    </table>
  </body>
</html>"""
    return _send_email(str(lead.email), subject, body, lead.lead_id, html_body)


def notify_staff(lead_id: str, message: str) -> dict:
    db.add_staff_notification(
        lead_id=lead_id,
        channel="email_or_demo_log",
        message=message,
        status="created",
    )
    return _send_email(settings.staff_notify_email, f"Solar Lead closed: {lead_id}", message, lead_id)


def send_conversation_summary(
    *,
    lead_id: str,
    lead_name: str,
    lead_email: str,
    lead_phone: str,
    source: str,
    transcript: str = "",
    conversation_turns: list[dict[str, Any]] | None = None,
    qualification: dict[str, Any] | None = None,
    voice_result: dict[str, Any] | None = None,
    call_summary: str | None = None,
    planning_context: dict[str, Any] | None = None,
) -> dict:
    qualification = qualification or {}
    voice_result = voice_result or {}
    planning_context = planning_context or {}
    subject = f"Solar Lead Gespraechszusammenfassung: {lead_name} ({lead_id})"
    lead_info = _lead_information_lines(
        lead_name=lead_name,
        lead_email=lead_email,
        lead_phone=lead_phone,
        planning_context=planning_context,
        qualification=qualification,
    )
    summary = _summary_line(call_summary, qualification, voice_result, transcript)
    confirm_url = _confirm_url(lead_id, planning_context)
    dashboard_url = _dashboard_url(lead_id)
    appointment_lines = _appointment_option_lines(lead_id, planning_context, qualification)
    recording_lines = _call_recording_lines(planning_context)
    body = "\n".join(
        [
            "Solar Lead OS - Gespraechszusammenfassung",
            "",
            f"Quelle: {source}",
            f"Lead ID: {lead_id}",
            "",
            "Lead information",
            *lead_info,
            "",
            "Agent-2-Dashboard",
            f"- Lead-Details, Systemplanung, Dachansicht und Handoff: {dashboard_url}",
            "",
            "Freie Termine aus dem Telefonat auswaehlen",
            *appointment_lines,
            "",
            "Call-Audio",
            *recording_lines,
            "",
            "Klare naechste Schritte",
            *_next_step_lines(qualification, voice_result),
            "",
            "Gespraechszusammenfassung",
            summary,
            "",
            *_conversation_lines(conversation_turns, transcript),
        ]
    )
    html_body = _conversation_summary_html(
        lead_id=lead_id,
        source=source,
        lead_info=lead_info,
        dashboard_url=dashboard_url,
        confirm_url=confirm_url,
        appointment_options_html=_appointment_options_html(lead_id, planning_context, qualification),
        recording_html=_call_recording_html(planning_context),
        next_step_lines=_next_step_lines(qualification, voice_result),
        summary=summary,
        conversation_lines=_conversation_lines(conversation_turns, transcript),
    )
    return _send_email(settings.conversation_summary_email, subject, body, lead_id, html_body)


def send_call_recording_email(
    *,
    lead_id: str,
    lead_name: str,
    lead_email: str,
    lead_phone: str,
    recording: dict[str, Any],
    attachment: dict[str, Any] | None = None,
) -> dict:
    download_url = str(recording.get("download_url") or recording.get("recording_url") or "")
    duration = recording.get("duration")
    subject = f"Solar Lead Call-Audio: {lead_name} ({lead_id})"
    body = "\n".join(
        [
            "Solar Lead OS - Call-Audio",
            "",
            f"Lead ID: {lead_id}",
            f"Name: {lead_name}",
            f"Email: {lead_email}",
            f"Telefon: {lead_phone}",
            f"Dauer: {duration or 'unbekannt'} Sekunden",
            "",
            f"Download: {download_url}",
            "",
            "Hinweis: Wenn ein Audio-Anhang enthalten ist, kann er direkt aus dieser Mail heruntergeladen werden.",
        ]
    )
    escaped_url = html_lib.escape(download_url)
    html_body = f"""<!doctype html>
<html>
  <body style="margin:0;background:#f4f7f2;font-family:Arial,sans-serif;color:#172018;">
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f4f7f2;padding:24px 0;">
      <tr><td align="center">
        <table role="presentation" width="640" cellspacing="0" cellpadding="0" style="width:640px;max-width:94%;background:#ffffff;border:1px solid #dbe6d6;border-radius:8px;overflow:hidden;">
          <tr><td style="padding:24px 28px;background:#15351f;color:#ffffff;">
            <h1 style="margin:0;font-size:24px;line-height:1.25;">Call-Audio verfuegbar</h1>
          </td></tr>
          <tr><td style="padding:24px 28px;">
            <p style="line-height:1.55;margin:0 0 16px;">Die Aufnahme fuer <strong>{html_lib.escape(lead_name)}</strong> ist verfuegbar.</p>
            <p style="margin:0 0 22px;">
              <a href="{escaped_url}" style="display:inline-block;background:#1d6f42;color:#ffffff;text-decoration:none;font-weight:bold;padding:14px 18px;border-radius:6px;">
                Call-Audio herunterladen
              </a>
            </p>
            <p style="color:#4b5b4e;font-size:14px;margin:0;">Lead: {html_lib.escape(lead_id)} | Dauer: {html_lib.escape(str(duration or 'unbekannt'))} Sekunden</p>
          </td></tr>
        </table>
      </td></tr>
    </table>
  </body>
</html>"""
    attachments = [attachment] if attachment else []
    return _send_email(
        settings.conversation_summary_email,
        subject,
        body,
        lead_id,
        html_body,
        attachments=attachments,
    )


def _lead_information_lines(
    *,
    lead_name: str,
    lead_email: str,
    lead_phone: str,
    planning_context: dict[str, Any],
    qualification: dict[str, Any],
) -> list[str]:
    lead = planning_context.get("lead") or {}
    concern = (
        qualification.get("main_concern")
        or lead.get("main_concern")
        or _join_values(qualification.get("objections") or [])
        or "Noch nicht eindeutig."
    )
    return [
        f"- Name: {lead_name}",
        f"- Adresse: {lead.get('address', 'unbekannt')}",
        f"- Email: {lead_email}",
        f"- Telefon: {lead_phone}",
        f"- Concerns: {concern}",
    ]


def _panel_plan_image_url(lead_id: str) -> str:
    return f"{settings.public_base_url}/api/leads/{lead_id}/panel-plan.png"


def _dashboard_url(lead_id: str) -> str:
    return f"{settings.public_base_url}/dashboard/leads/{lead_id}"


def _call_recording_lines(planning_context: dict[str, Any]) -> list[str]:
    recording = planning_context.get("call_recording") or {}
    download_url = recording.get("download_url")
    if not download_url:
        return ["- Noch keine Aufnahme verfuegbar. Sie wird nach Call-Ende automatisch nachgereicht."]
    duration = recording.get("duration")
    suffix = f" ({duration} Sekunden)" if duration else ""
    return [f"- Audio-Download{suffix}: {download_url}"]


def _call_recording_html(planning_context: dict[str, Any]) -> str:
    recording = planning_context.get("call_recording") or {}
    download_url = recording.get("download_url")
    if not download_url:
        return (
            "<p style=\"margin:0 0 24px;color:#4b5b4e;line-height:1.55;\">"
            "Noch keine Aufnahme verfuegbar. Sie wird nach Call-Ende automatisch nachgereicht.</p>"
        )
    duration = recording.get("duration")
    detail = f"Call-Dauer: {html_lib.escape(str(duration))} Sekunden" if duration else "Call-Audio"
    return f"""
            <p style="margin:0 0 10px;color:#4b5b4e;line-height:1.55;">{detail}</p>
            <p style="margin:0 0 24px;">
              <a href="{html_lib.escape(str(download_url))}" style="display:inline-block;background:#1d6f42;color:#ffffff;text-decoration:none;font-weight:bold;padding:12px 16px;border-radius:6px;">
                Call-Audio herunterladen
              </a>
            </p>
    """


def _confirm_url(lead_id: str, planning_context: dict[str, Any]) -> str:
    installers = planning_context.get("installers") or []
    if installers:
        for installer in installers:
            slots = installer.get("available_slots") or []
            if slots:
                first_slot = slots[0]
                slot_value = (
                    str(first_slot.get("value") or "")
                    if isinstance(first_slot, dict)
                    else str(first_slot)
                )
                installer_id = quote(str(installer.get("id") or ""))
                slot_query = f"&slot={quote(slot_value)}" if slot_value else ""
                return (
                    f"{settings.public_base_url}/installer/confirm/{lead_id}"
                    f"?installer_id={installer_id}{slot_query}"
                )
    slots = planning_context.get("available_slots") or []
    slot_value = ""
    if slots:
        first_slot = slots[0]
        if isinstance(first_slot, dict):
            slot_value = str(first_slot.get("value") or "")
        else:
            slot_value = str(first_slot)
    query = f"?slot={quote(slot_value)}" if slot_value else ""
    return f"{settings.public_base_url}/installer/confirm/{lead_id}{query}"


def _customer_booking_html(
    *,
    headline: str,
    intro: str,
    lead: SolarLeadIntake,
    primary_url: str,
) -> str:
    esc = html_lib.escape
    online_url = _booking_link_with_mode(lead.lead_id or "", "online")
    in_person_url = _booking_link_with_mode(lead.lead_id or "", "in_person")
    return f"""<!doctype html>
<html>
  <body style="margin:0;background:#f4f7f2;font-family:Arial,sans-serif;color:#172018;">
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f4f7f2;padding:24px 0;">
      <tr><td align="center">
        <table role="presentation" width="640" cellspacing="0" cellpadding="0" style="width:640px;max-width:94%;background:#ffffff;border:1px solid #dbe6d6;border-radius:8px;overflow:hidden;">
          <tr><td style="padding:24px 28px;background:#15351f;color:#ffffff;">
            <h1 style="margin:0;font-size:24px;line-height:1.25;">{esc(headline)}</h1>
          </td></tr>
          <tr><td style="padding:24px 28px;">
            <p style="line-height:1.55;margin:0 0 18px;">Hallo {esc(lead.name)},</p>
            <p style="line-height:1.55;margin:0 0 18px;">{esc(intro)}</p>
            <p style="margin:0 0 20px;">
              <a href="{esc(primary_url)}" style="display:inline-block;background:#1d6f42;color:#ffffff;text-decoration:none;font-weight:bold;padding:14px 18px;border-radius:6px;">
                Freie Termine ansehen
              </a>
            </p>
            <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="margin-top:8px;">
              <tr>
                <td style="padding:8px 6px 8px 0;">
                  <a href="{esc(online_url)}" style="display:block;border:1px solid #dbe6d6;color:#15351f;text-decoration:none;font-weight:bold;padding:13px;border-radius:6px;">Online-Termin buchen</a>
                </td>
                <td style="padding:8px 0 8px 6px;">
                  <a href="{esc(in_person_url)}" style="display:block;border:1px solid #dbe6d6;color:#15351f;text-decoration:none;font-weight:bold;padding:13px;border-radius:6px;">Vor-Ort-Termin buchen</a>
                </td>
              </tr>
            </table>
          </td></tr>
        </table>
      </td></tr>
    </table>
  </body>
</html>"""


def _customer_status_html(
    *,
    headline: str,
    intro: str,
    lead: SolarLeadIntake,
) -> str:
    esc = html_lib.escape
    return f"""<!doctype html>
<html>
  <body style="margin:0;background:#f4f7f2;font-family:Arial,sans-serif;color:#172018;">
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f4f7f2;padding:24px 0;">
      <tr><td align="center">
        <table role="presentation" width="640" cellspacing="0" cellpadding="0" style="width:640px;max-width:94%;background:#ffffff;border:1px solid #dbe6d6;border-radius:8px;overflow:hidden;">
          <tr><td style="padding:24px 28px;background:#15351f;color:#ffffff;">
            <h1 style="margin:0;font-size:24px;line-height:1.25;">{esc(headline)}</h1>
          </td></tr>
          <tr><td style="padding:24px 28px;">
            <p style="line-height:1.55;margin:0 0 18px;">Hallo {esc(lead.name)},</p>
            <p style="line-height:1.55;margin:0;">{esc(intro)}</p>
          </td></tr>
        </table>
      </td></tr>
    </table>
  </body>
</html>"""


def _appointment_option_lines(
    lead_id: str,
    planning_context: dict[str, Any],
    qualification: dict[str, Any],
) -> list[str]:
    installers = _installer_options(planning_context)
    preferred = _preferred_slot_tokens(qualification, planning_context)
    lines: list[str] = []
    if installers:
        for installer in installers:
            label = installer.get("name") or installer.get("id") or "Handwerker"
            region = installer.get("region") or "Standardgebiet"
            slots = installer.get("available_slots") or []
            if not slots:
                lines.append(f"- {label} ({region}): aktuell kein freier Slot auslesbar.")
                continue
            lines.append(f"- {label} ({region})")
            for slot in slots:
                slot_label = _slot_label(slot)
                marker = " [im Call besprochen]" if _slot_matches_preference(slot, preferred) else ""
                lines.append(
                    f"  {slot_label}{marker}: "
                    f"{_confirm_url_for_slot(lead_id, installer.get('id'), slot)}"
                )
        return lines
    confirm_url = _confirm_url(lead_id, planning_context)
    return [f"- Handwerker-Button: {confirm_url}"]


def _appointment_options_html(
    lead_id: str,
    planning_context: dict[str, Any],
    qualification: dict[str, Any],
) -> str:
    installers = _installer_options(planning_context)
    preferred = _preferred_slot_tokens(qualification, planning_context)
    if not installers:
        return ""
    cards: list[str] = []
    for installer in installers:
        name = html_lib.escape(str(installer.get("name") or installer.get("id") or "Handwerker"))
        region = html_lib.escape(str(installer.get("region") or "Standardgebiet"))
        slots = installer.get("available_slots") or []
        if slots:
            slot_buttons = "".join(
                _slot_button_html(
                    lead_id,
                    installer.get("id"),
                    slot,
                    _slot_matches_preference(slot, preferred),
                )
                for slot in slots
            )
        else:
            slot_buttons = (
                "<p style=\"margin:8px 0 0;color:#6d4b14;font-size:14px;\">"
                "Keine freien Termine auslesbar.</p>"
            )
        cards.append(
            f"""
            <div style="border:1px solid #dbe6d6;border-radius:8px;padding:14px;margin:0 0 12px;background:#fbfdf9;">
              <div style="font-weight:bold;font-size:16px;">{name}</div>
              <div style="color:#4b5b4e;font-size:13px;margin-top:2px;">{region}</div>
              <div style="margin-top:10px;">{slot_buttons}</div>
            </div>
            """
        )
    return "".join(cards)


def _installer_options(planning_context: dict[str, Any]) -> list[dict[str, Any]]:
    installers = planning_context.get("installers") or []
    if installers:
        return [installer for installer in installers if isinstance(installer, dict)]
    slots = planning_context.get("available_slots") or []
    if not slots:
        return []
    return [
        {
            "id": None,
            "name": "Handwerker",
            "region": "Standardgebiet",
            "available_slots": slots,
        }
    ]


def _slot_label(slot: Any) -> str:
    if isinstance(slot, dict):
        return str(slot.get("label") or slot.get("value") or "Termin")
    return str(slot)


def _slot_value(slot: Any) -> str:
    if isinstance(slot, dict):
        return str(slot.get("value") or slot.get("start") or slot.get("label") or "")
    return str(slot)


def _confirm_url_for_slot(lead_id: str, installer_id: Any, slot: Any) -> str:
    params = []
    if installer_id:
        params.append(f"installer_id={quote(str(installer_id))}")
    slot_value = _slot_value(slot)
    if slot_value:
        params.append(f"slot={quote(slot_value)}")
    query = f"?{'&'.join(params)}" if params else ""
    return f"{settings.public_base_url}/installer/confirm/{lead_id}{query}"


def _preferred_slot_tokens(
    qualification: dict[str, Any],
    planning_context: dict[str, Any],
) -> list[str]:
    raw_values = [
        qualification.get("preferred_installer_slot"),
        qualification.get("preferred_installer_slots"),
        qualification.get("selected_slot"),
        qualification.get("selected_slot_label"),
        planning_context.get("preferred_installer_slot"),
        planning_context.get("preferred_installer_slots"),
        planning_context.get("selected_slot"),
        planning_context.get("selected_slot_label"),
    ]
    tokens: list[str] = []
    for value in raw_values:
        if isinstance(value, list):
            tokens.extend(str(item).lower() for item in value if item)
        elif value:
            tokens.append(str(value).lower())
    return tokens


def _slot_matches_preference(slot: Any, preferred: list[str]) -> bool:
    if not preferred:
        return False
    haystack = f"{_slot_label(slot)} {_slot_value(slot)}".lower()
    return any(token and token in haystack for token in preferred)


def _slot_button_html(
    lead_id: str,
    installer_id: Any,
    slot: Any,
    preferred: bool,
) -> str:
    url = html_lib.escape(_confirm_url_for_slot(lead_id, installer_id, slot))
    label = html_lib.escape(_slot_label(slot))
    badge = (
        "<span style=\"display:inline-block;margin-left:8px;color:#1d6f42;font-size:12px;font-weight:bold;\">im Call besprochen</span>"
        if preferred
        else ""
    )
    return (
        f"<div style=\"margin:8px 0;\">"
        f"<a href=\"{url}\" style=\"display:inline-block;background:#1d6f42;color:#ffffff;text-decoration:none;font-weight:bold;padding:11px 14px;border-radius:6px;\">"
        f"{label}</a>{badge}</div>"
    )


def _panel_caption(planning_context: dict[str, Any]) -> str:
    offer = planning_context.get("offer") or {}
    profitability = planning_context.get("profitability") or {}
    solar = planning_context.get("solar") or {}
    potential = solar.get("solar_potential") or {}
    system_size = offer.get("system_size_kwp") or profitability.get("estimated_kwp")
    roof_area = potential.get("roof_area_m2")
    price = offer.get("price_range") or {}
    parts = ["Grobe Systemplanung"]
    if system_size:
        parts.append(f"{system_size} kWp")
    if roof_area:
        parts.append(f"ca. {roof_area} m2 belegbare Dachflaeche")
    if price:
        parts.append(f"{price.get('min')} bis {price.get('max')} {price.get('currency', 'EUR')}")
    return " - ".join(parts)


def _conversation_summary_html(
    *,
    lead_id: str,
    source: str,
    lead_info: list[str],
    dashboard_url: str,
    confirm_url: str,
    appointment_options_html: str,
    recording_html: str,
    next_step_lines: list[str],
    summary: str,
    conversation_lines: list[str],
) -> str:
    def esc(value: Any) -> str:
        return html_lib.escape(str(value))

    def list_html(items: list[str]) -> str:
        return "".join(f"<li>{esc(item.removeprefix('- '))}</li>" for item in items)

    return f"""<!doctype html>
<html>
  <body style="margin:0;background:#f4f7f2;font-family:Arial,sans-serif;color:#172018;">
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f4f7f2;padding:24px 0;">
      <tr><td align="center">
        <table role="presentation" width="680" cellspacing="0" cellpadding="0" style="width:680px;max-width:94%;background:#ffffff;border:1px solid #dbe6d6;border-radius:8px;overflow:hidden;">
          <tr><td style="padding:24px 28px;background:#15351f;color:#ffffff;">
            <div style="font-size:13px;letter-spacing:.04em;text-transform:uppercase;">{esc(source)}</div>
            <h1 style="margin:8px 0 0;font-size:24px;line-height:1.25;">Lead-Handoff fuer Handwerker</h1>
          </td></tr>
          <tr><td style="padding:24px 28px;">
            <h2 style="font-size:18px;margin:0 0 10px;">Lead information</h2>
            <ul style="margin:0 0 24px;padding-left:20px;line-height:1.55;">{list_html(lead_info)}</ul>

            <h2 style="font-size:18px;margin:0 0 10px;">Agent-2-Dashboard</h2>
            <p style="line-height:1.55;margin:0 0 14px;color:#4b5b4e;">
              Lead-Details, Systemplanung, Dachansicht und Handoff liegen im Dashboard.
            </p>
            <p style="margin:0 0 24px;">
              <a href="{esc(dashboard_url)}" style="display:inline-block;background:#15351f;color:#ffffff;text-decoration:none;font-weight:bold;padding:14px 18px;border-radius:6px;">
                Agent-2-Details im Dashboard oeffnen
              </a>
            </p>

            <p style="margin:0 0 22px;">
              <a href="{esc(confirm_url)}" style="display:inline-block;background:#1d6f42;color:#ffffff;text-decoration:none;font-weight:bold;padding:14px 18px;border-radius:6px;">
                Ersten passenden Termin bestaetigen
              </a>
            </p>

            <h2 style="font-size:18px;margin:0 0 10px;">Freie Termine aus dem Telefonat auswaehlen</h2>
            {appointment_options_html}

            <h2 style="font-size:18px;margin:0 0 10px;">Call-Audio</h2>
            {recording_html}

            <h2 style="font-size:18px;margin:0 0 10px;">Naechste Schritte</h2>
            <ul style="margin:0 0 24px;padding-left:20px;line-height:1.55;">{list_html(next_step_lines)}</ul>

            <h2 style="font-size:18px;margin:0 0 10px;">Gespraechszusammenfassung</h2>
            <p style="line-height:1.55;margin:0 0 14px;">{esc(summary)}</p>
            <ul style="margin:0;padding-left:20px;line-height:1.55;">{list_html(conversation_lines)}</ul>
          </td></tr>
        </table>
      </td></tr>
    </table>
  </body>
</html>"""


def _summary_line(
    call_summary: str | None,
    qualification: dict[str, Any],
    voice_result: dict[str, Any],
    transcript: str,
) -> str:
    if call_summary:
        return call_summary
    if qualification.get("call_summary"):
        return str(qualification["call_summary"])
    if voice_result.get("response_text"):
        return str(voice_result["response_text"])
    if transcript:
        return transcript[:700]
    return "Noch keine auswertbare Zusammenfassung vorhanden."


def _qualification_lines(qualification: dict[str, Any]) -> list[str]:
    fields = [
        ("Bedarf", "need"),
        ("Hauptsorge", "main_concern"),
        ("Einwaende", "objections"),
        ("Kaufbereitschaft", "buying_readiness"),
        ("Gewuenschtes Ergebnis", "desired_outcome"),
        ("Outcome", "call_outcome"),
        ("Confidence", "confidence_score"),
    ]
    lines = [
        f"- {label}: {qualification.get(key, 'unbekannt')}"
        for label, key in fields
        if qualification.get(key) not in (None, "", [])
    ]
    objections = qualification.get("objections")
    if objections:
        lines.append(f"- Einwaende: {_join_values(objections)}")
    return lines or ["- Noch keine strukturierten Qualifikationsdaten vorhanden."]


def _planning_lines(planning_context: dict[str, Any]) -> list[str]:
    profitability = planning_context.get("profitability") or {}
    offer = planning_context.get("offer") or {}
    solar = planning_context.get("solar") or planning_context.get("solar_enrichment") or {}
    slots = planning_context.get("available_slots") or []
    handoff = planning_context.get("handoff") or {}
    lines: list[str] = []

    if profitability:
        lines.append(
            "- Wirtschaftlichkeit: "
            f"{profitability.get('decision', 'unbekannt')} "
            f"(Score {profitability.get('score', 'n/a')}, "
            f"Ressource {profitability.get('resource_level', 'n/a')})."
        )
        if profitability.get("estimated_kwp"):
            lines.append(
                "- Grobe Systemplanung: "
                f"{profitability.get('estimated_kwp')} kWp, "
                f"{profitability.get('estimated_price_min')} bis "
                f"{profitability.get('estimated_price_max')} EUR."
            )
    if offer:
        price = offer.get("price_range") or {}
        if offer.get("package_name"):
            lines.append(f"- Angebotsrichtung: {offer.get('package_name')}.")
        if price:
            lines.append(
                "- Preisrahmen laut Agent 2: "
                f"{price.get('min')} bis {price.get('max')} {price.get('currency', 'EUR')}."
            )
        if offer.get("next_steps"):
            lines.append(f"- Agent-2-Naechste Schritte: {_join_values(offer.get('next_steps'))}.")
    if solar:
        source = solar.get("source") or solar.get("data_source")
        if source:
            lines.append(f"- Solar-/Dachquelle: {source}.")
        if solar.get("roof_area_m2"):
            lines.append(f"- Geschaetzte Dachflaeche: {solar.get('roof_area_m2')} m2.")
    if slots:
        slot_labels = [
            str(slot.get("label") or slot.get("value") or slot)
            for slot in slots[:3]
        ]
        lines.append(f"- Naechste freie Handwerker-Termine: {_join_values(slot_labels)}.")
    if handoff.get("demo_url"):
        lines.append(f"- Interner Agent-2/Handoff-Link: {handoff.get('demo_url')}.")
    return lines or ["- Agent-2-Planungsdaten liegen noch nicht vor."]


def _next_step_lines(
    qualification: dict[str, Any],
    voice_result: dict[str, Any],
) -> list[str]:
    intent = str(voice_result.get("intent") or qualification.get("call_outcome") or "").lower()
    if intent in {"closed", "ready_to_book", "booked"}:
        return [
            "- Einen der freien Handwerker-Termine fuer ein finales Vor-Ort-Planungsgespraech bestaetigen.",
            "- Agent-2-Plan, Angebotsspanne und offene Sorgen im Termin gezielt vorbereiten.",
            "- Nach dem Termin finale Gesamtplanung und verbindliches Angebot ausarbeiten.",
        ]
    if intent in {"opt_out", "do_not_contact"}:
        return [
            "- Kontaktwunsch respektieren und Lead nicht weiter aktiv anrufen.",
            "- Status im CRM auf Opt-out setzen.",
        ]
    missing = qualification.get("missing_fields")
    if missing:
        return [
            f"- Fehlende Angaben klaeren: {_join_values(missing)}.",
            "- Danach Wirtschaftlichkeit und Angebot erneut bewerten.",
        ]
    return [
        "- Wichtigste Sorge des Leads im Follow-up direkt adressieren.",
        "- Wenn genug Vertrauen da ist, auf Vor-Ort-Planungsgespraech closen.",
        "- Agent-2-Plan und freie Handwerker-Termine bereithalten.",
    ]


def _conversation_lines(
    conversation_turns: list[dict[str, Any]] | None,
    transcript: str,
) -> list[str]:
    if conversation_turns:
        return [
            f"- {turn.get('speaker', 'Sprecher')}: {turn.get('text', '')}"
            for turn in conversation_turns
            if turn.get("text")
        ]
    if transcript:
        return [transcript]
    return ["Kein Transcript vorhanden."]


def _join_values(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    return str(value)
