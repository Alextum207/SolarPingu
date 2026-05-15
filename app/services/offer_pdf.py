from __future__ import annotations

from io import BytesIO
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.platypus import (
    Image as PlatypusImage,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from app.config import settings
from app.models import OfferDraft, ProfitabilityDecision, SolarLeadIntake


def offer_pdf_dir() -> Path:
    path = Path(settings.sqlite_path).parent / "offers"
    path.mkdir(parents=True, exist_ok=True)
    return path


def offer_pdf_path(lead_id: str) -> Path:
    return offer_pdf_dir() / f"{lead_id}.pdf"


def offer_pdf_url(lead_id: str) -> str:
    return f"{settings.public_base_url}/api/leads/{lead_id}/offer.pdf"


def _money(value: int) -> str:
    return f"{value:,.0f} EUR".replace(",", ".")


def _agent2_image_url(solar_enrichment: dict) -> str | None:
    url = solar_enrichment.get("roofImageUrl")
    if not url:
        return None
    cleaned = str(url).strip()
    if not cleaned:
        return None
    if cleaned.startswith(("http://", "https://", "data:")):
        return cleaned
    if cleaned.startswith("/"):
        return f"{settings.agent2_base_url}{cleaned}"
    return cleaned


def _fetch_image_bytes(url: str) -> bytes | None:
    if url.startswith("data:"):
        return None
    try:
        request = Request(url, headers={"User-Agent": "SolarPingu-Agent1/1.0"})
        with urlopen(request, timeout=8) as response:
            content_type = response.headers.get("content-type", "")
            if content_type and not content_type.startswith("image/"):
                return None
            return response.read(4_000_000)
    except (OSError, URLError, ValueError):
        return None


def _roof_image_flowable(url: str) -> PlatypusImage | None:
    image_bytes = _fetch_image_bytes(url)
    if not image_bytes:
        return None
    image_buffer = BytesIO(image_bytes)
    try:
        width, height = ImageReader(image_buffer).getSize()
    except Exception:
        return None
    image_buffer.seek(0)
    max_width = 164 * mm
    max_height = 78 * mm
    scale = min(max_width / width, max_height / height, 1)
    return PlatypusImage(image_buffer, width=width * scale, height=height * scale)


def generate_offer_pdf(
    lead: SolarLeadIntake,
    profitability: ProfitabilityDecision,
    offer: OfferDraft,
    solar_enrichment: dict,
) -> Path:
    path = offer_pdf_path(lead.lead_id or offer.lead_id)
    styles = getSampleStyleSheet()
    title = ParagraphStyle(
        "SolarTitle",
        parent=styles["Title"],
        fontName="Helvetica-Bold",
        fontSize=24,
        leading=28,
        textColor=colors.HexColor("#14351f"),
        alignment=TA_CENTER,
        spaceAfter=10,
    )
    h2 = ParagraphStyle(
        "SolarH2",
        parent=styles["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=14,
        leading=17,
        textColor=colors.HexColor("#1d6f42"),
        spaceBefore=12,
        spaceAfter=8,
    )
    body = ParagraphStyle(
        "SolarBody",
        parent=styles["BodyText"],
        fontName="Helvetica",
        fontSize=10,
        leading=14,
        textColor=colors.HexColor("#1a241d"),
    )
    small = ParagraphStyle(
        "SolarSmall",
        parent=body,
        fontSize=8.5,
        leading=11,
        textColor=colors.HexColor("#536158"),
    )

    doc = SimpleDocTemplate(
        str(path),
        pagesize=A4,
        rightMargin=18 * mm,
        leftMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=16 * mm,
    )
    story = [
        Paragraph("Solar Lead OS", title),
        Paragraph("Vorläufiges Projektangebot", h2),
        Paragraph(
            "Dieses Angebot wurde automatisch aus Formularangaben, Solar-Potenzial, "
            "Profitabilitätslogik und Gemini-Angebotslogik erzeugt. Es ist eine "
            "belastbare Demo-Vorprüfung, kein finaler Installationsvertrag.",
            body,
        ),
        Spacer(1, 8),
    ]

    summary = [
        ["Kunde", lead.name],
        ["Adresse", lead.address],
        ["Empfohlenes Paket", offer.package_name],
        ["Anlagengröße", f"{offer.system_size_kwp:.1f} kWp"],
        ["Speicher", "Ja" if offer.includes_battery else "Optional"],
        ["Preisrange", f"{_money(offer.price_range.min)} - {_money(offer.price_range.max)}"],
        ["Profitabilitäts-Score", f"{profitability.score}/100 ({profitability.decision})"],
    ]
    table = Table(summary, colWidths=[48 * mm, 116 * mm])
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef7ee")),
                ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#cfe0cf")),
                ("INNERGRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#dce8d9")),
                ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ]
        )
    )
    story += [table, Spacer(1, 10)]

    solar = solar_enrichment.get("solar_potential", {})
    roof_image_url = _agent2_image_url(solar_enrichment)
    roof_image = _roof_image_flowable(roof_image_url) if roof_image_url else None
    story += [
        Paragraph("Wirtschaftliche Entscheidung", h2),
        Paragraph(" ".join(profitability.reasons) or "Projekt wurde regelbasiert bewertet.", body),
        Paragraph(
            f"Geschätzte Marge: {_money(profitability.estimated_margin)}. "
            f"Payback-Schätzung: {profitability.payback_years or 'n/a'} Jahre. "
            f"Solarquelle: {solar_enrichment.get('source', 'fallback')}.",
            body,
        ),
        Paragraph("Anlagenkonzept", h2),
        Paragraph(
            f"Geschätzte Jahresproduktion: {solar.get('yearly_energy_kwh', 'n/a')} kWh. "
            f"Dachfläche für PV: {solar.get('roof_area_m2', 'n/a')} m². "
            f"Konfidenz: {round(float(solar.get('confidence', 0)) * 100)}%.",
            body,
        ),
    ]
    if roof_image is not None:
        story += [
            Paragraph("Agent-2 Dachbild", h2),
            roof_image,
            Paragraph(
                f"Quelle: {solar_enrichment.get('roofImageSource') or 'Agent 2 / Maps Static'}. "
                "Das Bild dient als visuelle Vorpruefung und ersetzt keine Vor-Ort-Pruefung.",
                small,
            ),
        ]
    elif solar_enrichment.get("imageWarning"):
        story.append(
            Paragraph(f"Agent-2 Dachbild: {solar_enrichment.get('imageWarning')}", small)
        )
    story.append(Paragraph("Kundennutzen", h2))
    for item in offer.value_pitch:
        story.append(Paragraph(f"- {item}", body))
    story.append(Paragraph("Annahmen", h2))
    for item in offer.assumptions:
        story.append(Paragraph(f"- {item}", body))
    story.append(Paragraph("Nächste Schritte", h2))
    for item in offer.next_steps:
        story.append(Paragraph(f"- {item}", body))
    story += [
        Spacer(1, 14),
        Paragraph(
            "Hinweis: Finale Preise hängen von Dachbelegung, Zählerschrank, Statik, "
            "Gerüst, Netzanschluss und Vor-Ort-Prüfung ab.",
            small,
        ),
    ]
    doc.build(story)
    return path
