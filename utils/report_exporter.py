"""
utils/report_exporter.py
────────────────────────
Export scan / deletion results to CSV or PDF.
"""

from __future__ import annotations

import csv
import os
from datetime import datetime
from typing import Iterable

from models.resource import CloudResource


REPORTS_DIR = os.path.join(os.path.dirname(__file__), "..", "reports")


def _ensure_reports_dir() -> str:
    os.makedirs(REPORTS_DIR, exist_ok=True)
    return os.path.abspath(REPORTS_DIR)


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _clean_text(text: str) -> str:
    """Replace common unicode characters with ASCII equivalents for PDF export."""
    text = str(text)
    replacements = {
        "\u2013": "-",    # en dash
        "\u2014": "--",   # em dash
        "\u2018": "'",    # left single quote
        "\u2019": "'",    # right single quote
        "\u201c": '"',    # left double quote
        "\u201d": '"',    # right double quote
        "\u2022": "*",    # bullet
        "\u2026": "...",  # ellipsis
    }
    for search, replace in replacements.items():
        text = text.replace(search, replace)
    # Fallback: encode to latin-1 and ignore errors, then decode back
    return text.encode("latin-1", errors="ignore").decode("latin-1")


# ── CSV ───────────────────────────────────────────────────────────────────────

def export_csv(resources: Iterable[CloudResource], filename: str = "") -> str:
    """
    Write resources to a CSV file.
    Returns the absolute path of the created file.
    """
    out_dir = _ensure_reports_dir()
    if not filename:
        filename = f"cleanup_report_{_timestamp()}.csv"
    path = os.path.join(out_dir, filename)

    fieldnames = [
        "resource_id", "name", "resource_type", "provider",
        "region", "status", "estimated_cost_usd",
        "deletion_status", "error_message", "age_days", "tags",
    ]

    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for r in resources:
            writer.writerow({
                "resource_id":          r.resource_id,
                "name":                 r.name,
                "resource_type":        r.resource_type.value,
                "provider":             r.provider.value,
                "region":               r.region,
                "status":               r.status,
                "estimated_cost_usd":   f"{r.estimated_cost:.4f}",
                "deletion_status":      r.deletion_status.value if r.deletion_status else "",
                "error_message":        r.error_message or "",
                "age_days":             r.age_days,
                "tags":                 r.tags_summary,
            })
    return path


# ── PDF ───────────────────────────────────────────────────────────────────────

def export_pdf(resources: Iterable[CloudResource],
               title: str = "Cloud Janitor Report",
               filename: str = "") -> str:
    """
    Write resources to a PDF table using fpdf2.
    Returns the absolute path of the created file.
    """
    try:
        from fpdf import FPDF  # type: ignore[import]
    except ImportError:
        raise ImportError("fpdf2 is required: pip install fpdf2")

    out_dir = _ensure_reports_dir()
    if not filename:
        filename = f"cleanup_report_{_timestamp()}.pdf"
    path = os.path.join(out_dir, filename)

    res_list = list(resources)
    total_cost = sum(r.estimated_cost for r in res_list)

    pdf = FPDF(orientation="L", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=10)
    pdf.add_page()

    # ── Title ──────────────────────────────────────────────────────────
    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(0, 10, _clean_text(title), ln=True, align="C")
    pdf.set_font("Helvetica", "", 9)
    pdf.cell(0, 6,
             _clean_text(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}  |  "
             f"Total resources: {len(res_list)}  |  "
             f"Est. monthly savings: ${total_cost:,.2f}"),
             ln=True, align="C")
    pdf.ln(4)

    # ── Table header ───────────────────────────────────────────────────
    headers   = ["Name", "Type", "Provider", "Region", "Status", "Cost/mo", "Del. Status"]
    col_widths = [55, 32, 22, 35, 22, 22, 28]

    pdf.set_font("Helvetica", "B", 8)
    pdf.set_fill_color(40, 40, 40)
    pdf.set_text_color(255, 255, 255)
    for h, w in zip(headers, col_widths):
        pdf.cell(w, 7, h, border=1, fill=True, align="C")
    pdf.ln()

    # ── Table rows ─────────────────────────────────────────────────────
    pdf.set_font("Helvetica", "", 7)
    pdf.set_text_color(0, 0, 0)
    fill = False
    for r in res_list:
        pdf.set_fill_color(235, 235, 235) if fill else pdf.set_fill_color(255, 255, 255)
        row_data = [
            r.display_name[:40],
            r.resource_type.value,
            r.provider.value,
            r.region,
            r.status[:18],
            r.cost_label,
            r.deletion_status.value if r.deletion_status else "–",
        ]
        for val, w in zip(row_data, col_widths):
            pdf.cell(w, 6, _clean_text(val), border=1, fill=True)
        pdf.ln()
        fill = not fill

    pdf.output(path)
    return path
