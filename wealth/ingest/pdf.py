"""PDF statements: bounded, page-referenced text extraction and deterministic parsing.

pypdf is used because it is already a runtime dependency, is pure Python and
maintained by the py-pdf project, exposes encryption state and the document
structure needed to flag JavaScript/embedded files, and its layout mode keeps
the column alignment the table parser relies on.  pdfplumber would add
pdfminer.six plus a native renderer for table geometry this parser does not need.
"""

from __future__ import annotations

import hashlib
import io
from pathlib import Path
from typing import Any

from .common import envelope
from .llm import extraction_request
from .model import build_proposal
from .redact import redact_text
from .safety import MAX_PDF_PAGES, pdf_risks
from .statement import parse_statement_text


_ENCRYPTED = ("This PDF is password-protected. Save an unlocked copy (for example with Print > Save as PDF) and "
              "upload that. Wealth does not ask for, accept or store document passwords.")


def provenance_for(data: bytes, filename: str, media_type: str, **extra: Any) -> dict[str, Any]:
    sha = hashlib.sha256(data).hexdigest()
    name = redact_text(Path(filename or "upload").name)[:120]
    pages = extra.get("pages")
    ref = f"document:sha256:{sha[:16]} {name}" + (f" pages 1-{pages}" if pages else "")
    return {"kind": "document", "ref": ref, "sha256": sha, "filename": name, "media_type": media_type, **extra}


def extract_pdf(data: bytes, *, max_pages: int = MAX_PDF_PAGES) -> dict[str, Any]:
    """Return page texts (1-based page numbers), risk flags and encryption state."""
    from pypdf import PdfReader

    warnings: list[str] = []
    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
    except Exception as exc:
        return {"error": f"The PDF could not be opened ({type(exc).__name__})."}
    if reader.is_encrypted:
        try:
            unlocked = reader.decrypt("")
        except Exception:
            unlocked = 0
        if not unlocked:
            return {"encrypted": True, "risks": []}
        warnings.append("The PDF carried an owner-only restriction and opened without a password.")
    risks = pdf_risks(reader, data)
    total = len(reader.pages)
    pages: list[tuple[int, str]] = []
    for number, page in enumerate(reader.pages[:max_pages], 1):
        try:
            text = page.extract_text(extraction_mode="layout") or ""
        except Exception:
            try:
                text = page.extract_text() or ""
                warnings.append(f"Page {number} lost its column layout during extraction.")
            except Exception:
                text = ""
                warnings.append(f"Page {number} text could not be extracted.")
        pages.append((number, text))
    if total > max_pages:
        warnings.append(f"Only the first {max_pages} of {total} pages were read.")
    return {"encrypted": False, "pages": pages, "page_count": total, "truncated": total > max_pages,
            "risks": risks, "warnings": warnings}


def ingest_pdf(data: bytes, filename: str, *, owner_id: str = "self", aliases: dict[str, list[str]] | None = None,
               tolerance: Any = None) -> dict[str, Any]:
    extracted = extract_pdf(data)
    if extracted.get("error"):
        return envelope("rejected", {"provenance": provenance_for(data, filename, "application/pdf")},
                        warnings=[extracted["error"]])
    if extracted.get("encrypted"):
        provenance = provenance_for(data, filename, "application/pdf")
        return envelope("needs_input", {"provenance": provenance, "reason": "encrypted"},
                        missing=[{"key": "unlocked_file", "reason": "encrypted", "detail": _ENCRYPTED}],
                        sources=[provenance["ref"]])
    pages = extracted["pages"]
    provenance = provenance_for(data, filename, "application/pdf", pages=extracted["page_count"],
                                parser="pypdf-layout-heuristics")
    warnings = list(extracted["warnings"])
    reasons = []
    if extracted["risks"]:
        warnings.append("PDF contains active or embedded content (" + ", ".join(extracted["risks"]) +
                        "); it was read as text only and nothing was executed or extracted.")
        reasons.append("The PDF has active or embedded content; confirm it came from the institution.")
    if extracted["truncated"]:
        reasons.append("The statement was truncated at the page limit.")
    provenance["risk_flags"] = extracted["risks"]
    if not any(text.strip() for _, text in pages):
        request = extraction_request([], provenance=provenance,
                                     reason="The PDF has no text layer (likely scanned). Wealth includes no OCR.")
        return envelope("needs_extraction", {"provenance": provenance, "extraction_request": request},
                        warnings=warnings, sources=[provenance["ref"]])
    parsed = parse_statement_text(pages, aliases=aliases)
    if not parsed["parsed"]:
        request = extraction_request(pages, provenance=provenance,
                                     reason="No holdings table, balance or transaction list matched the known layouts.")
        return envelope("needs_extraction", {"provenance": provenance, "extraction_request": request},
                        warnings=warnings, sources=[provenance["ref"]])
    statement = parsed["statement"]
    provenance["as_of_page"] = statement.get("as_of_page")
    proposal = build_proposal(
        statement, kind="document", provenance=provenance, owner_id=owner_id, confidence=parsed["confidence"],
        warnings=warnings, assumptions=parsed["notes"], review_reasons=reasons, tolerance=tolerance,
    )
    if proposal["status"] == "needs_review" and proposal["result"]["reconciliation"]["status"] != "reconciled":
        proposal["result"]["extraction_request"] = extraction_request(
            pages, provenance=provenance,
            reason="Deterministic parsing did not reconcile; a structured re-extraction can be validated instead.",
        )
    return proposal
