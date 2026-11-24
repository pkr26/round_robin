"""Deterministic exports from one immutable saved report, without external resources."""

from io import BytesIO
from html import escape as html_escape
import json
from pathlib import Path
import re
import threading
from xml.sax.saxutils import escape

from reportlab import rl_config
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import HRFlowable, LongTable, Paragraph, SimpleDocTemplate, Spacer, TableStyle

MAX_EXPORT_BYTES = 10 * 1024 * 1024
_FONT_LOCK = threading.Lock()


class ExportFailure(ValueError):
    pass


class UnsupportedPdfText(ExportFailure):
    """The bundled font cannot faithfully render the requested saved text."""


def plain(value) -> str:
    if value is None:
        return "Unknown"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def cost_amount(value, currency: str) -> str:
    """Retain the recorded currency while distinguishing unknown amounts from zero."""
    return f"{plain(value)} {currency}".strip()


def describe(value) -> str:
    if isinstance(value, list):
        return "\n".join(describe(item) for item in value)
    if isinstance(value, dict):
        if "concern" in value:
            scope = "; ".join(
                f"{key}: {', '.join(plain(item) for item in value[key]) if isinstance(value[key], list) else plain(value[key])}"
                for key in ("origin", "source_step_id", "source_provider", "source_stage", "target_reference_id", "option_id", "requirement_id", "reference_ids")
                if key in value and value[key] is not None and value[key] != []
            )
            history = []
            if isinstance(value.get("events"), list):
                events = "\n".join(
                    "; ".join(
                        f"{key}: {', '.join(plain(item) for item in field) if isinstance(field, list) else plain(field)}"
                        for key, field in event.items()
                    ) if isinstance(event, dict) else plain(event)
                    for event in value["events"]
                )
                if events:
                    history.append(events)
            history.extend(plain(value[key]) for key in ("responses", "resolutions") if key in value)
            return f"{value.get('severity', 'material')}: {value['concern']}\nStatus: {value.get('status', 'unresolved')}. Issue: {value.get('id', 'unavailable')}.\n" + (scope + "\n" if scope else "") + ("\n".join(history) if history else plain([]))
        if "question" in value:
            return "\n".join(part for part in [value.get("question"), f"Affects: {value['affected_conclusion']}" if value.get("affected_conclusion") else None, f"Check: {value['verification_method']}" if value.get("verification_method") else None, value.get("request_text"), f"Responsible: {value['responsible_person']}" if value.get("responsible_person") else None, f"Revisit: {value['revisit_date']}" if value.get("revisit_date") else None] if part)
        if "description" in value:
            return f"{value['description']}\nAssessment: {value.get('status', 'not assessed')}. Basis: {', '.join(value.get('reference_ids', [])) or 'no supporting record supplied'}."
        if "provider" in value:
            return f"{value['provider']} - {value.get('role', '')}: {value.get('status', 'unknown')}. {value.get('notes', '')}"
        if "request_count" in value or "actual_cost" in value:
            usage = f"Usage: {value.get('status', 'unknown')}. Provider requests: {value.get('request_count', 'unknown')}. Confirmed cost: {value.get('actual_cost') if value.get('actual_cost') is not None else 'unknown'} {value.get('currency', 'USD')}."
            if "conservative_exposure_usd" in value:
                usage += f" Conservative exposure: {plain(value['conservative_exposure_usd'])} {value.get('currency', 'USD')}."
            if value.get("unknown_operation_ids"):
                usage += f" Unresolved operations: {', '.join(plain(item) for item in value['unknown_operation_ids'])}."
            return usage
    return plain(value)


def describe_constraint(item: dict, *, include_provenance: bool = True, reference_separator: str = ", ") -> str:
    """Keep the saved assessment and its supporting and conflicting scope together."""
    result = f"{item.get('requirement_id')}: {item.get('result')}."
    if item.get("disputed"):
        result += " Disputed: yes."
    result += f" {item.get('reason', '')}"
    if not include_provenance:
        return result
    result += f" Basis: {reference_separator.join(item.get('basis_refs', [])) or 'none recorded'}."
    if item.get("counter_basis_refs"):
        result += f" Counter-evidence: {reference_separator.join(item['counter_basis_refs'])}."
    if item.get("method"):
        result += f" Method: {item['method']}."
    if item.get("assessor"):
        result += f" Assessor: {item['assessor']}."
    if item.get("assessment_scope"):
        scope = item["assessment_scope"]
        result += f" Scope: {scope}" + ("" if scope.endswith((".", "!", "?")) else ".")
    return result


def report_markdown(report: dict, *, title: str, sources: list[dict]) -> str:
    # Report structure is ours; every interpolated value remains literal text.
    # A blockquote alone does not neutralize Markdown images or raw HTML.
    report = _markdown_values(report)
    sources = _markdown_values(sources)
    title = markdown_literal(title)
    recommendation = report.get("recommendation")
    summary = recommendation.get("summary") if isinstance(recommendation, dict) else recommendation
    lines = [f"# {title}", "", f"Saved report {report.get('id', 'unknown')} | version {report.get('version')} | event cutoff {report.get('event_cutoff')}", "", "SIMULATED: no provider calls or independently validated conclusions." if report.get("simulated") or report.get("fictional") else "Provider-supported decision brief; scope and caveats remain applicable.", "", f"Execution: {report.get('execution_status', 'unknown')}", f"Decision readiness: {report.get('readiness') or 'unavailable'}", "", "## Recommendation", plain(summary or report.get("readiness_unavailable_reason") or "No recommendation established."), ""]
    if isinstance(recommendation, dict) and recommendation.get("conditions"):
        lines += ["Conditions:", *[f"- {condition}" for condition in recommendation["conditions"]], ""]
    for key, heading in [("readiness_reasons", "Readiness basis"), ("main_reasons", "Reasons"), ("essential_dependencies", "Essential dependencies"), ("dissent", "Dissent and unresolved objections"), ("issues", "Complete objection and resolution ledger"), ("evidence_assessments", "Scoped evidence assessments"), ("change_summary", "Changes from the parent version")]:
        values = report.get(key) or []
        if values:
            lines += [f"## {heading}", *[f"- {describe(value)}" for value in values], ""]
    for key, heading in [("readiness_dimensions", "Readiness scope and checks"), ("decision_insights", "Option ordering and sensitivity")]:
        if report.get(key):
            lines += [f"## {heading}", describe(report[key]), ""]
    lines += ["## Option comparison"]
    for option in report.get("comparison", []):
        lines += [f"### {option.get('label', option.get('option_id'))}", f"All-in total: {cost_amount(option.get('total'), option.get('currency', ''))} over {option.get('horizon_months')} months.", f"Known subtotal: {cost_amount(option.get('known_subtotal'), option.get('currency', ''))}. Missing: {', '.join(option.get('missing_components', [])) or 'none recorded'}.", *[f"- {describe_constraint(item)}" for item in option.get("constraint_results", [])], plain(option.get("caveat", "")), ""]
    for key, heading in [("tradeoff", "Decisive tradeoff"), ("largest_uncertainty", "Largest uncertainty"), ("what_would_change_answer", "What would change the answer"), ("next_action", "Next action")]:
        lines += [f"## {heading}", describe(report.get(key)), ""]
    for item in report.get("next_evidence", []):
        lines += [f"### {item.get('question', 'Next evidence')}", describe(item), ""]
    lines += ["## Calculations"]
    for calculation in report.get("calculations", []):
        lines += [f"### {calculation.get('label', calculation.get('id'))}", f"{calculation.get('formula')} = {calculation.get('output') if calculation.get('output') is not None else 'Unknown'} {calculation.get('unit', '')}", f"Rounding: {calculation.get('rounding')}", *[f"- {item.get('label')}: {item.get('amount')} {item.get('unit')} (reference {item.get('source_ref')})" for item in calculation.get("inputs", [])], ""]
    lines += ["## Exact stored evidence"]
    for source in sources:
        metadata = f"Origin: {source.get('origin', 'user_supplied')}. Status: {source.get('verification_status', 'not_checked')}. Recorded: {source.get('verified_at', 'unknown')}"
        if source.get("time_sensitive") is not None:
            metadata += f". Time-sensitive: {'yes' if source['time_sensitive'] else 'no'}"
        lines += [f"### {source.get('source_title', source.get('title', source.get('id')))}", f"Reference {source.get('id')} | {source.get('locator', 'stored text')} | SHA-256 {source.get('sha256')}", metadata, "> " + plain(source.get("text", source.get("passage", ""))).replace("\n", "\n> "), plain(source.get("scope", "No independent support verification is implied.")), ""]
    lines += ["## Coverage and usage", describe(report.get("coverage", [])), describe(report.get("usage", {})), f"Stopping reason: {report.get('stop_reason')}", ""]
    return "\n".join(lines)


def markdown_literal(value: str) -> str:
    escaped = html_escape(value, quote=False)
    # Break every tilde pair so neither fenced code nor strikethrough can form.
    escaped = re.sub(r"([\\`*_{}\[\]()#+!|\-.])", r"\\\1", escaped)
    escaped = re.sub(r"~{2,}", lambda match: "&#126;~" * (len(match.group()) // 2) + ("&#126;" if len(match.group()) % 2 else ""), escaped)
    # An equals-only line can turn preceding saved text into a heading.
    escaped = re.sub(r"(?m)(?:^|(?<=\r))[ \t]*=+[ \t]*(?=\r|$)", lambda match: match.group().replace("=", "&#61;", 1), escaped)
    # GFM linkification runs on untrusted text and can truncate escaped URLs.
    escaped = re.sub(r"(?i)\b(https?|ftp|mailto):", r"\1&#58;", escaped)
    # Keep an indented text line from being interpreted as a code block.
    return re.sub(r"(?m)(?:^|(?<=\r))(?: {4,}| {0,3}\t)", lambda match: ("&#9;" if match.group()[0] == "\t" else "&#32;") + match.group()[1:], escaped)


def _markdown_values(value):
    if isinstance(value, str):
        return markdown_literal(value)
    if isinstance(value, list):
        return [_markdown_values(item) for item in value]
    if isinstance(value, dict):
        return {key: _markdown_values(item) for key, item in value.items()}
    return value


def _register_font():
    with _FONT_LOCK:
        registered = set(pdfmetrics.getRegisteredFontNames())
        if "RRVera" not in registered or "RRVeraBold" not in registered:
            import reportlab
            font_directory = Path(reportlab.__file__).parent / "fonts"
        if "RRVera" not in registered:
            pdfmetrics.registerFont(TTFont("RRVera", str(font_directory / "Vera.ttf")))
        if "RRVeraBold" not in registered:
            pdfmetrics.registerFont(TTFont("RRVeraBold", str(font_directory / "VeraBd.ttf")))


def report_pdf(report: dict, *, title: str, sources: list[dict]) -> bytes:
    _register_font()
    output = BytesIO()
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle("RRBody", fontName="RRVera", fontSize=9, leading=14, textColor=colors.HexColor("#26372e"), spaceAfter=8, wordWrap="CJK"))
    styles.add(ParagraphStyle("RRHeading", parent=styles["RRBody"], fontName="RRVeraBold", fontSize=15, leading=20, spaceBefore=14, spaceAfter=9, keepWithNext=True))
    styles.add(ParagraphStyle("RRSmall", parent=styles["RRBody"], fontSize=7, leading=11, textColor=colors.HexColor("#61705e")))
    styles.add(ParagraphStyle("RRTitle", parent=styles["RRHeading"], fontSize=25, leading=32, spaceAfter=16))
    body = styles["RRBody"]

    def paragraph(value, style=body):
        # User/source text is escaped; Paragraph cannot load a remote image/link.
        rendered = plain(value)
        glyphs = pdfmetrics.getFont(style.fontName).face.charToGlyph
        if any(not character.isspace() and not glyphs.get(ord(character)) for character in rendered):
            raise UnsupportedPdfText("The embedded PDF font does not support all saved text; use the exact JSON or Markdown export")
        return Paragraph(escape(rendered).replace("\n", "<br/>"), style)

    story = [paragraph("ROUND ROBIN / SAVED DECISION BRIEF", styles["RRSmall"]), Spacer(1, 10), paragraph(title, styles["RRTitle"]), paragraph(f"Report {report.get('id')} | version {report.get('version')} | event cutoff {report.get('event_cutoff')}", styles["RRSmall"]), HRFlowable(width="100%", thickness=1, color=colors.HexColor("#cbd7c4")), Spacer(1, 12)]
    if report.get("simulated") or report.get("fictional"):
        story.append(paragraph("SIMULATED OUTPUT: no provider calls or independent real-world verification.", styles["RRSmall"]))
    story.append(paragraph(f"Execution: {report.get('execution_status')} | Readiness: {report.get('readiness') or 'unavailable'}"))
    recommendation = report.get("recommendation")
    story += [paragraph("Recommendation", styles["RRHeading"]), paragraph(recommendation.get("summary") if isinstance(recommendation, dict) else recommendation or report.get("readiness_unavailable_reason") or "No recommendation established.")]
    if isinstance(recommendation, dict):
        story.extend(paragraph("Condition: " + value) for value in recommendation.get("conditions", []))
    for value in report.get("readiness_reasons", []):
        story.append(paragraph(value))
    story.append(paragraph("Comparable costs and hard requirements", styles["RRHeading"]))
    rows = [[paragraph("Option", styles["RRSmall"]), paragraph("All-in total", styles["RRSmall"]), paragraph("Requirement results", styles["RRSmall"])]]
    for option in report.get("comparison", []):
        currency = option.get("currency", "")
        amount = cost_amount(option.get("total"), currency)
        subtotal = cost_amount(option.get("known_subtotal"), currency)
        missing = option.get("missing_components", [])
        missing_summary = f"{len(missing)} component{'s' if len(missing) != 1 else ''}; see details below" if missing else "none recorded"
        detail = "\n".join(describe_constraint(item, include_provenance=False) for item in option.get("constraint_results", []))
        rows.append([paragraph(option.get("label", option.get("option_id"))), paragraph(f"{amount}\n{option.get('horizon_months')} months\nKnown subtotal {subtotal}\nMissing: {missing_summary}"), paragraph(detail)])
    if len(rows) > 1:
        table = LongTable(rows, colWidths=[1.65 * inch, 1.35 * inch, 3.65 * inch], repeatRows=1, hAlign="LEFT", splitByRow=1, splitInRow=1)
        table.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eaf0e6")), ("LINEBELOW", (0, 0), (-1, -1), .4, colors.HexColor("#dfe5dc")), ("TOPPADDING", (0, 0), (-1, -1), 9), ("BOTTOMPADDING", (0, 0), (-1, -1), 9)]))
        story.append(table)
    comparisons = report.get("comparison", [])
    if comparisons:
        story.append(paragraph("Option cost caveats", styles["RRHeading"]))
        for option in comparisons:
            story.append(paragraph(f"Option: {option.get('label', option.get('option_id'))} | {option.get('option_id')}", styles["RRHeading"]))
            story.append(paragraph(option.get("caveat", "")))
            if option.get("missing_components"):
                story.append(paragraph("Missing components:", styles["RRSmall"]))
                story.extend(paragraph(item) for item in option["missing_components"])
    assessments = [(option, option.get("constraint_results", [])) for option in report.get("comparison", []) if option.get("constraint_results")]
    if assessments:
        story.append(paragraph("Requirement assessment provenance", styles["RRHeading"]))
        for option, constraints in assessments:
            story.append(paragraph(f"Option: {option.get('label', option.get('option_id'))} | {option.get('option_id')}", styles["RRHeading"]))
            story.extend(paragraph(describe_constraint(item, reference_separator=",\n")) for item in constraints)
    for field, label in [("main_reasons", "Main reasons"), ("essential_dependencies", "Essential dependencies"), ("tradeoff", "Decisive tradeoff"), ("largest_uncertainty", "Largest uncertainty"), ("what_would_change_answer", "What changes the answer"), ("dissent", "Dissent"), ("issues", "Complete objection and resolution ledger"), ("evidence_assessments", "Scoped evidence assessments"), ("readiness_dimensions", "Readiness scope and checks"), ("decision_insights", "Option ordering and sensitivity"), ("next_action", "Next action"), ("next_evidence", "Next evidence and quote/site-check brief"), ("change_summary", "Changes from parent")]:
        value = report.get(field)
        if value:
            story.append(paragraph(label, styles["RRHeading"]))
            story.extend(paragraph(describe(item)) for item in value) if isinstance(value, list) else story.append(paragraph(describe(value)))
    for calculation in report.get("calculations", []):
        story += [paragraph(calculation.get("label", "Calculation"), styles["RRHeading"]), paragraph(f"{calculation.get('formula')} = {calculation.get('output') if calculation.get('output') is not None else 'Unknown'} {calculation.get('unit', '')}"), paragraph(calculation.get("rounding"), styles["RRSmall"])]
        story.extend(paragraph(f"{item.get('label')}: {item.get('amount')} {item.get('unit')} | reference {item.get('source_ref')}", styles["RRSmall"]) for item in calculation.get("inputs", []))
    for source in sources:
        metadata = f"Origin {source.get('origin', 'user_supplied')} | {source.get('verification_status', 'not_checked')} | Recorded: {source.get('verified_at', 'unknown')} | SHA-256 {source.get('sha256')}"
        if source.get("time_sensitive") is not None:
            metadata += f" | Time-sensitive: {'yes' if source['time_sensitive'] else 'no'}"
        story += [paragraph(source.get("source_title", source.get("title", "Stored evidence")), styles["RRHeading"]), paragraph(f"{source.get('id')} | {source.get('locator', 'stored text')}", styles["RRSmall"]), paragraph(source.get("text", source.get("passage", ""))), paragraph(metadata, styles["RRSmall"]), paragraph(source.get("scope", "No independent verification implied."), styles["RRSmall"])]
    story += [paragraph("Coverage, usage and stopping reason", styles["RRHeading"]), paragraph(describe(report.get("coverage"))), paragraph(describe(report.get("usage"))), paragraph(report.get("stop_reason"))]
    identifier = f"v{report.get('version')} / cutoff {report.get('event_cutoff')}"

    def footer(canvas, document):
        canvas.saveState()
        canvas.setFont("RRVera", 7)
        canvas.setFillColor(colors.HexColor("#61705e"))
        canvas.drawString(42, 26, f"Round Robin | {identifier}")
        canvas.drawRightString(570, 26, f"Page {document.page}")
        canvas.restoreState()

    SimpleDocTemplate(output, pagesize=(612, 792), rightMargin=42, leftMargin=42, topMargin=40, bottomMargin=44, title=title, author="Round Robin").build(story, onFirstPage=footer, onLaterPages=footer)
    return output.getvalue()


def render_saved_report(report: dict, format: str, *, title: str, sources: list[dict] | None = None) -> tuple[bytes, str]:
    source_records = sources or []
    if format == "json":
        content, media = (json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode(), "application/json"
    elif format == "markdown":
        content, media = report_markdown(report, title=title, sources=source_records).encode(), "text/markdown; charset=utf-8"
    elif format == "pdf":
        content, media = report_pdf(report, title=title, sources=source_records), "application/pdf"
    else:
        raise ExportFailure("Unsupported saved-report export format")
    if len(content) > MAX_EXPORT_BYTES:
        raise ExportFailure("Export exceeds ten MiB")
    return content, media
