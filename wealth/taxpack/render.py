"""CSV files, the printable HTML page and file exports of a tax pack report."""
from __future__ import annotations

import csv
import html
import io
import json
from typing import Any, Iterable, Mapping

from .book import _cols, _d


# ----------------------------------------------------------------- CSV and HTML


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value)


def csv_files(report: Mapping[str, Any], language: str | None = None) -> dict[str, str]:
    """One CSV per section (its table), plus pendientes, deadlines and reconciliation.  Empty cell = unknown."""
    result = report.get("result") or {}
    lang = language or result.get("language") or "es"
    lang = lang if lang in {"es", "en"} else "es"
    year = result.get("tax_year")
    files: dict[str, str] = {}

    def write(name: str, columns: list[dict], rows: list[Mapping[str, Any]]) -> None:
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow([c.get(lang) or c["key"] for c in columns])
        for row in rows:
            writer.writerow([_cell(row.get(c["key"])) for c in columns])
        files[f"tax-pack-{year}-{name}.csv"] = buffer.getvalue()

    recon_rows = []
    for sid in result.get("section_order") or []:
        section = result["sections"][sid]
        table = section.get("table") or {}
        if table.get("columns"):
            write(sid, table["columns"], table.get("rows") or [])
        for item in section.get("reconciliation") or []:
            recon_rows.append({"section": sid, **item})
    write("pendientes", _cols(("section", "Sección", "Section"), ("key", "Clave", "Key"),
                              ("detail_es", "Qué falta", "What is missing (es)"),
                              ("detail", "Qué falta (en)", "What is missing")), result.get("pendientes") or [])
    write("deadlines", _cols(("date", "Fecha", "Date"), ("jurisdiction", "País", "Jurisdiction"),
                             ("es", "Qué", "What (es)"), ("en", "Qué (en)", "What"), ("basis", "Fundamento", "Basis")),
          result.get("deadlines") or [])
    if recon_rows:
        write("reconciliation", _cols(("section", "Sección", "Section"), ("item", "Concepto", "Item"),
                                      ("ours", "Nuestro cálculo", "Our computation"),
                                      ("document", "Documento", "Document"), ("difference", "Diferencia", "Difference"),
                                      ("source_of_truth", "Fuente de verdad", "Source of truth")), recon_rows)
    return files


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _bi(es: str, en: str) -> str:
    return f'<span class="l-es" lang="es">{_e(es)}</span><span class="l-en" lang="en">{_e(en)}</span>'


_UNKNOWN = '<span class="unk">' + _bi("desconocido", "unknown") + "</span>"


def _num(value: Any) -> str:
    """Money strings (with a decimal point) get thousands separators; other values print as they are."""
    if value is None:
        return _UNKNOWN
    if isinstance(value, bool):
        return _bi("sí", "yes") if value else _bi("no", "no")
    if isinstance(value, str) and "." in value:
        number = _d(value)
        if number is not None and value.lstrip("-").replace(".", "", 1).isdigit():
            places = max(2, len(value.split(".", 1)[1]))  # rates and factors keep their precision
            return _e(f"{number:,.{places}f}")
    if isinstance(value, (dict, list)):
        return _e(json.dumps(value, ensure_ascii=False, default=str))
    return _e(value)


_SUMMARY_LABELS = {
    "net_result_mxn": ("Resultado neto Art. 129", "Net Art. 129 result"),
    "carry_used_mxn": ("Pérdidas anteriores aplicadas", "Prior losses used"),
    "taxable_gain_mxn": ("Ganancia gravable", "Taxable gain"),
    "tax_10pct_mxn": ("ISR 10% a pagar", "10% tax due"),
    "new_loss_carryforward_mxn": ("Pérdida nueva por amortizar", "New loss to carry forward"),
    "new_loss_expires": ("Vence (año)", "Expires (year)"),
    "real_interest_mxn": ("Interés real a acumular", "Real interest to accumulate"),
    "real_interest_loss_mxn": ("Pérdida real (Art. 134)", "Real interest loss (Art. 134)"),
    "retention_mxn": ("ISR retenido acreditable", "Creditable ISR withheld"),
    "nominal_interest_mxn": ("Interés nominal", "Nominal interest"),
    "inflation_factor": ("Factor de inflación del año", "Inflation factor for the year"),
    "known_tax_mxn": ("Impuesto conocido", "Known tax"),
    "net_estimated_mexican_tax_mxn": ("Impuesto mexicano estimado", "Estimated Mexican tax"),
    "short_term_before_carryover_usd": ("Corto plazo (antes de arrastre)", "Short term (before carryover)"),
    "long_term_before_carryover_usd": ("Largo plazo (antes de arrastre)", "Long term (before carryover)"),
    "capital_gain_distributions_usd": ("Distribuciones de ganancias (1099-DIV 2a)", "Capital gain distributions (2a)"),
    "net_short_term_usd": ("Neto corto plazo", "Net short term"),
    "net_long_term_usd": ("Neto largo plazo", "Net long term"),
    "net_capital_gain_or_loss_usd": ("Ganancia o pérdida neta", "Net capital gain or loss"),
    "deductible_loss_usd": ("Pérdida deducible este año", "Loss deductible this year"),
    "loss_limit_usd": ("Límite de pérdida deducible", "Deductible loss limit"),
    "dividends_usd": ("Dividendos", "Dividends"), "qualified_dividends_usd": ("Dividendos calificados", "Qualified"),
    "interest_usd": ("Intereses", "Interest"), "total_usd": ("Total", "Total"),
    "ordinary_dividends_to_report_usd": ("Dividendos ordinarios a declarar", "Ordinary dividends to report"),
    "interest_to_report_usd": ("Intereses a declarar", "Interest to report"),
    "uma_daily_mxn": ("UMA diaria", "Daily UMA"), "ppr_from_ledger_mxn": ("Aportaciones PPR (ledger)", "PPR deposits (ledger)"),
}


def _summary_html(section: Mapping[str, Any]) -> str:
    summary = section.get("summary") or {}
    parts = []
    scalars = [(k, v) for k, v in summary.items() if k in _SUMMARY_LABELS and not isinstance(v, (dict, list))]
    if scalars:
        parts.append('<dl class="ticket">' + "".join(
            f'<div class="row{" total" if k in {"tax_10pct_mxn", "net_capital_gain_or_loss_usd", "real_interest_mxn"} else ""}">'
            f"<dt>{_bi(*_SUMMARY_LABELS[k])}</dt><dd class=\"v\">{_num(v)}</dd></div>" for k, v in scalars) + "</dl>")
    brokers = summary.get("brokers")
    if isinstance(brokers, list) and brokers and isinstance(brokers[0], dict) and "declared_net_mxn" in brokers[0]:
        head = ("<tr><th>" + _bi("Casa de bolsa", "Broker") + "</th><th>" + _bi("Ganancias", "Gains") + "</th><th>"
                + _bi("Pérdidas", "Losses") + "</th><th>" + _bi("Neto (cálculo)", "Net (ours)") + "</th><th>"
                + _bi("Neto (constancia)", "Net (constancia)") + "</th><th>" + _bi("A declarar", "Declared")
                + "</th></tr>")
        body = "".join(f"<tr><th>{_e(b['broker'])}</th><td>{_num(b['gains_mxn'])}</td><td>{_num(b['losses_mxn'])}</td>"
                       f"<td>{_num(b['net_mxn'])}</td><td>{_num(b['constancia_net_mxn'])}</td>"
                       f"<td>{_num(b['declared_net_mxn'])}</td></tr>" for b in brokers)
        parts.append(f'<table class="grid"><thead>{head}</thead><tbody>{body}</tbody></table>')
    for key in ("fbar", "form_8938"):
        block = summary.get(key)
        if isinstance(block, dict):
            label = ("FBAR (FinCEN 114)", "FBAR (FinCEN 114)") if key == "fbar" else ("Formulario 8938", "Form 8938")
            required = block.get("required")
            verdict = (_bi("Requerido", "Required") if required is True else _bi("No requerido", "Not required")
                       if required is False else _bi("Desconocido: faltan saldos", "Unknown: balances missing"))
            detail = (f"{_num(block.get('aggregate_max_value_usd_at_least') or block.get('max_value_usd_at_least'))} "
                      f"USD ≥ · {_bi('umbral', 'threshold')} "
                      f"{_num(block.get('threshold_usd') or block.get('threshold_any_time_usd'))} USD")
            parts.append(f'<div class="flag"><strong>{_bi(*label)}: {verdict}</strong>'
                         f'<div class="sub">{detail}</div><div class="sub">{_e(block.get("rule"))}</div></div>')
    for key in ("contributions", "rmd"):
        block = summary.get(key)
        if isinstance(block, dict):
            parts.append('<dl class="ticket">' + "".join(
                f'<div class="row"><dt>{_e(k)}</dt><dd class="v">{_num(v)}</dd></div>'
                for k, v in block.items() if k != "item") + "</dl>")
    return "".join(parts)


def _table_html(section: Mapping[str, Any]) -> str:
    table = section.get("table") or {}
    columns, rows = table.get("columns") or [], table.get("rows") or []
    if not columns or not rows:
        return ""
    head = "".join(f"<th>{_bi(c['es'], c['en'])}</th>" for c in columns)
    body = "".join("<tr>" + "".join(f"<td>{_num(row.get(c['key']))}</td>" for c in columns) + "</tr>" for row in rows)
    return f'<div class="scroll"><table class="grid"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def _recon_html(section: Mapping[str, Any]) -> str:
    rows = section.get("reconciliation") or []
    if not rows:
        return ""
    head = ("<tr><th>" + _bi("Concepto", "Item") + "</th><th>" + _bi("Nuestro cálculo", "Our computation")
            + "</th><th>" + _bi("Documento", "Document") + "</th><th>" + _bi("Diferencia", "Difference") + "</th></tr>")
    body = "".join(f"<tr><th>{_e(r['item'])}</th><td>{_num(r['ours'])}</td><td>{_num(r['document'])}</td>"
                   f"<td>{_num(r['difference'])}</td></tr>" for r in rows)
    return (f'<h3>{_bi("Conciliación con la constancia (la constancia manda)", "Reconciliation with the document (the document wins)")}'
            f'</h3><table class="grid recon"><thead>{head}</thead><tbody>{body}</tbody></table>')


_STATUS = {"ready": ("completo", "complete"), "partial": ("parcial", "partial"),
           "needs_input": ("faltan datos", "needs input"), "not_applicable": ("sin movimientos", "no activity")}

_CSS = """
:root{--canvas:#FBF8F2;--ink:#2B2522;--ink-2:#4A413C;--muted:#6E625B;--hairline:#D9CEC6;--vermilion:#C84335;
--sans:Inter,ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
--mono:"Roboto Mono",ui-monospace,SFMono-Regular,Menlo,monospace;color-scheme:light}
*{box-sizing:border-box}html{background:var(--canvas)}
body{margin:0;background:var(--canvas);color:var(--ink);font:400 15px/24px var(--sans);font-variant-numeric:tabular-nums}
.page{width:min(960px,100%);margin:0 auto;padding:16px 24px 96px}
.top{display:flex;justify-content:space-between;align-items:center;min-height:48px}
.top button{min-height:40px;border:0;background:none;cursor:pointer;font:500 13px var(--mono);color:var(--muted)}
.top button[aria-pressed=true]{color:var(--ink);text-decoration:underline;text-underline-offset:6px}
.origin{margin:24px 0 0;font:400 13px/20px var(--sans);color:var(--muted)}
h1{margin:6px 0 0;font:700 40px/44px var(--sans);letter-spacing:-.04em}
.meta{margin-top:12px;padding-bottom:8px;border-bottom:1.5px solid var(--ink);font:400 13px/20px var(--mono);color:var(--ink-2)}
section.part{margin-top:40px;padding-top:12px;border-top:1px solid var(--hairline)}
h2{margin:0 0 10px;font:600 18px/26px var(--sans)}h3{margin:18px 0 6px;font:600 14px/20px var(--sans);color:var(--ink-2)}
.status{font:400 12px var(--mono);color:var(--muted);margin-left:8px}
dl.ticket{margin:0}.row{display:flex;justify-content:space-between;gap:16px;padding:8px 0;border-top:1px solid var(--hairline)}
.row:first-child{border-top:0}.row dt{color:var(--ink-2)}.row dd{margin:0}.v{font:400 14px var(--mono);white-space:nowrap}
.row.total{border-top:1.5px solid var(--ink)}.row.total dt,.row.total .v{font-weight:600;color:var(--ink)}
.scroll{overflow-x:auto}
table.grid{width:100%;border-collapse:collapse;margin-top:8px;font-size:13px}
table.grid th,table.grid td{padding:6px 8px 6px 0;border-top:1px solid var(--hairline);text-align:right;vertical-align:baseline}
table.grid thead th{border-top:0;font:400 11px/16px var(--mono);color:var(--muted);text-align:right}
table.grid th:first-child,table.grid td:first-child,table.grid tbody th{text-align:left}
table.grid td{font-family:var(--mono);white-space:nowrap}
.unk{color:var(--vermilion);font-style:italic}
.flag{margin-top:10px;padding:8px 0;border-top:1px solid var(--hairline)}
.sub{font:400 12px/18px var(--mono);color:var(--muted)}
ul.notes{margin:8px 0 0;padding-left:18px;color:var(--muted);font-size:13px;line-height:20px}
ol.pend{margin:0;padding-left:22px}ol.pend li{padding:6px 0;border-top:1px solid var(--hairline)}
.foot{margin-top:40px;font:400 12px/18px var(--mono);color:var(--muted)}
html[data-lang=es] .l-en,html[data-lang=en] .l-es{display:none}
@page{margin:14mm 12mm 16mm}
@media print{html,body{background:#fff}body{font-size:9.5pt;line-height:1.4}.page{width:auto;padding:0}
.top{display:none!important}h1{font-size:22pt;line-height:1.1}section.part{margin-top:12pt;padding-top:6pt;break-inside:auto}
h2{font-size:12pt;break-after:avoid}table.grid{font-size:8pt}table.grid tr{break-inside:avoid}.scroll{overflow:visible}
.row{padding:3pt 0}a{text-decoration:none}}
@media (max-width:480px){h1{font-size:30px;line-height:34px}.page{padding:8px 16px 64px}}
"""


def render_html(report: Mapping[str, Any], language: str | None = None) -> str:
    """A self-contained printable page (es/en toggle; print to PDF from the browser)."""
    result = report.get("result") or {}
    lang = language or result.get("language") or "es"
    lang = lang if lang in {"es", "en"} else "es"
    year = result.get("tax_year")
    status = _STATUS.get(report.get("status"), (report.get("status"), report.get("status")))
    out = [f'<!doctype html><html lang="{lang}" data-lang="{lang}"><head><meta charset="utf-8">'
           '<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="color-scheme" content="light">'
           f'<meta name="referrer" content="no-referrer"><title>Tax pack {_e(year)} · Wealth</title>'
           f"<style>{_CSS}</style></head><body><div class=\"page\">"
           '<div class="top"><button type="button" onclick="window.print()">'
           + _bi("Imprimir / PDF", "Print / PDF") + '</button><div role="group" aria-label="Language">'
           f'<button type="button" data-lang="es" aria-pressed="{str(lang == "es").lower()}">ES</button>'
           f'<button type="button" data-lang="en" aria-pressed="{str(lang == "en").lower()}">EN</button></div></div>'
           f'<p class="origin">{_bi("Paquete fiscal para tu contador", "Tax pack for your CPA")}'
           f'{" · " + _e(result.get("person")) if result.get("person") else ""}</p>'
           f"<h1>{_bi(f'Paquete fiscal {year}', f'Tax pack {year}')}</h1>"
           f'<div class="meta">{_e(", ".join(result.get("jurisdictions") or []))} · {_bi(*status)} · '
           f'{_bi("preparado el", "prepared")} {_e(result.get("prepared_on"))}</div>']
    pend = result.get("pendientes") or []
    out.append(f'<section class="part"><h2>{_bi("Pendientes", "Pending items")}'
               f'<span class="status">{len(pend)}</span></h2>')
    if pend:
        out.append('<ol class="pend">' + "".join(
            f'<li>{_bi(p.get("detail_es") or p["detail"], p["detail"])}<div class="sub">{_e(p["section"])} · '
            f'{_e(p["key"])}</div></li>' for p in pend) + "</ol>")
    else:
        out.append(f'<p>{_bi("Nada pendiente.", "Nothing pending.")}</p>')
    out.append("</section>")
    deadlines = result.get("deadlines") or []
    if deadlines:
        out.append(f'<section class="part"><h2>{_bi("Fechas clave", "Key deadlines")}</h2><dl class="ticket">'
                   + "".join(f'<div class="row"><dt>{_bi(d["es"], d["en"])}<div class="sub">{_e(d["jurisdiction"])} · '
                             f'{_e(d["basis"])}</div></dt><dd class="v">{_e(d["date"])}</dd></div>' for d in deadlines)
                   + "</dl></section>")
    for number, sid in enumerate(result.get("section_order") or [], start=1):
        section = result["sections"][sid]
        st = _STATUS.get(section["status"], (section["status"], section["status"]))
        out.append(f'<section class="part" id="{_e(sid)}"><h2>{number}. {_bi(section["title"]["es"], section["title"]["en"])}'
                   f'<span class="status">{_e(section.get("currency") or "")} · {_bi(*st)}</span></h2>')
        out.append(_summary_html(section))
        out.append(_table_html(section))
        out.append(_recon_html(section))
        notes = [*section.get("warnings", []), *section.get("assumptions", [])]
        if notes:
            out.append(f'<h3>{_bi("Notas y supuestos", "Notes and assumptions")}</h3><ul class="notes">'
                       + "".join(f"<li>{_e(n)}</li>" for n in notes) + "</ul>")
        if section.get("sources"):
            out.append(f'<h3>{_bi("Fuentes", "Sources")}</h3><ul class="notes">' + "".join(
                f'<li>{_e(s.get("title") or s.get("ref"))}{" — " + _e(s["url"]) if s.get("url") else ""}</li>'
                for s in section["sources"] if isinstance(s, dict)) + "</ul>")
        out.append("</section>")
    out.append(f'<p class="foot">{_bi("Preparado por Wealth con tus estados de cuenta, constancias y datos guardados. No es una declaración: revísalo con tu contador. Un dato vacío es desconocido, nunca cero.", "Prepared by Wealth from your statements, documents and saved facts. Not a return: review it with your CPA. An empty figure is unknown, never zero.")}</p>')
    out.append("</div><script>document.querySelectorAll('[data-lang]').forEach(function(b){if(b.tagName!=='BUTTON')return;"
               "b.addEventListener('click',function(){var l=b.getAttribute('data-lang');document.documentElement.setAttribute('data-lang',l);"
               "document.documentElement.lang=l;document.querySelectorAll('button[data-lang]').forEach(function(x){"
               "x.setAttribute('aria-pressed',String(x===b));});});});</script></body></html>")
    return "".join(out)


def write_exports(report: Mapping[str, Any], out_dir: Any, language: str | None = None,
                  formats: Iterable[str] = ("json", "csv", "html")) -> list[str]:
    """Write the pack as JSON, one CSV per section and the printable HTML; returns the paths written."""
    from pathlib import Path
    folder = Path(out_dir).expanduser()
    folder.mkdir(parents=True, exist_ok=True)
    year = (report.get("result") or {}).get("tax_year")
    written = []
    formats = set(formats)
    if "json" in formats:
        path = folder / f"tax-pack-{year}.json"
        body = {k: v for k, v in report.items() if k != "views"}
        path.write_text(json.dumps(body, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        written.append(str(path))
    if "csv" in formats:
        for name, text in csv_files(report, language).items():
            path = folder / name
            path.write_text(text, encoding="utf-8")
            written.append(str(path))
    if "html" in formats:
        path = folder / f"tax-pack-{year}.html"
        path.write_text(render_html(report, language), encoding="utf-8")
        written.append(str(path))
    return written
