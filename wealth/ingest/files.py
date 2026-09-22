"""Single entry point for uploaded files: safety checks, sniffing and dispatch."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterable

from .common import envelope
from .llm import extraction_request
from .model import build_proposal
from .pdf import ingest_pdf, provenance_for
from .safety import (IMAGE_KINDS, LIMITS, MAX_ROWS, UnsafeInput, check_xlsx, flag_instructions, mark_untrusted,
                     read_upload, resolve_upload, sniff)
from .statement import parse_statement_text
from .tabular import parse_export, read_rows


_MEDIA = {"pdf": "application/pdf", "csv": "text/csv", "png": "image/png", "jpeg": "image/jpeg", "gif": "image/gif",
          "webp": "image/webp", "heic": "image/heic", "tiff": "image/tiff",
          "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}


def ingest_file(path: str | os.PathLike[str], *, allowed_roots: Iterable[str | os.PathLike[str]] | None = None,
                **options: Any) -> dict[str, Any]:
    """Read a local upload safely and return an ingest proposal (never saved).

    Options: ``owner_id``, ``preset`` and ``aliases`` (exports), ``currency`` and
    ``as_of`` overrides (exports), ``tolerance``, and ``source_text`` (text the
    host extracted from an image).
    """
    try:
        resolved = resolve_upload(path, allowed_roots)
        data = read_upload(resolved)
    except UnsafeInput as exc:
        return envelope("rejected", {"reason": "unsafe_input"}, warnings=[str(exc)])
    return ingest_bytes(data, resolved.name, **options)


def ingest_bytes(data: bytes, filename: str, *, owner_id: str = "self", preset: str | None = None,
                 aliases: dict[str, list[str]] | None = None, currency: str | None = None, as_of: str | None = None,
                 tolerance: Any = None, source_text: str | None = None) -> dict[str, Any]:
    kind = sniff(data)
    group = "image" if kind in IMAGE_KINDS else kind
    name = Path(filename or "upload").name
    if group not in LIMITS:
        return envelope("rejected", {"reason": "unsupported_type", "detected": kind},
                        warnings=[f"Unsupported file content ({kind}); upload a PDF, CSV, XLSX or image statement."])
    if len(data) > LIMITS[group]:
        return envelope("rejected", {"reason": "too_large", "detected": kind},
                        warnings=[f"The {group.upper()} upload exceeds {LIMITS[group] // (1024 * 1024)} MB."])
    try:
        if kind == "pdf":
            report = ingest_pdf(data, name, owner_id=owner_id, aliases=aliases, tolerance=tolerance)
        elif group == "image":
            report = _ingest_image(data, name, kind, owner_id=owner_id, aliases=aliases, tolerance=tolerance,
                                   source_text=source_text)
        else:
            report = _ingest_table(data, name, kind, owner_id=owner_id, preset=preset, aliases=aliases,
                                   currency=currency, as_of=as_of, tolerance=tolerance)
        return mark_untrusted(report)  # page text and descriptions are the file's words, not instructions
    except UnsafeInput as exc:
        return envelope("rejected", {"reason": "unsafe_input"}, warnings=[str(exc)])
    except (ValueError, KeyError, IndexError) as exc:
        return envelope("rejected", {"reason": "unreadable", "detected": kind},
                        warnings=[f"The file could not be read as a statement ({type(exc).__name__}: {str(exc)[:120]})."])


def _ingest_table(data: bytes, name: str, kind: str, *, owner_id, preset, aliases, currency, as_of, tolerance) -> dict[str, Any]:
    warnings = check_xlsx(data) if kind == "xlsx" else []
    rows = read_rows(data, kind)
    parsed = parse_export(rows, preset=preset, aliases=aliases, currency=currency, as_of=as_of)
    provenance = provenance_for(data, name, _MEDIA[kind], rows=len(rows), parser=f"export:{parsed['preset'] or 'generic'}")
    flag_instructions(provenance, (cell for row in rows[:MAX_ROWS] for cell in row))
    if not parsed["parsed"]:
        text = "\n".join(",".join(row) for row in rows[:2000])
        request = extraction_request([(1, text)], provenance=provenance,
                                     reason="No holdings or transaction header matched the known column aliases.")
        return envelope("needs_extraction", {"provenance": provenance, "extraction_request": request},
                        warnings=warnings + ["Pass aliases={field: [header, ...]} for a custom export, or use the extraction request."],
                        sources=[provenance["ref"]])
    return build_proposal(parsed["statement"], kind="document", provenance=provenance, owner_id=owner_id,
                          confidence=parsed["confidence"], warnings=warnings + parsed.get("warnings", []),
                          assumptions=parsed["notes"],
                          tolerance=tolerance)


def _ingest_image(data: bytes, name: str, kind: str, *, owner_id, aliases, tolerance, source_text) -> dict[str, Any]:
    provenance = provenance_for(data, name, _MEDIA[kind], parser="host-text")
    flag_instructions(provenance, [source_text or ""])
    if source_text:
        parsed = parse_statement_text([(1, source_text)], aliases=aliases)
        if parsed["parsed"]:
            return build_proposal(parsed["statement"], kind="document", provenance=provenance, owner_id=owner_id,
                                  confidence=parsed["confidence"], assumptions=parsed["notes"], tolerance=tolerance,
                                  warnings=["Values come from text the host read from an image; confirm them against the image."])
        request = extraction_request([(1, source_text)], provenance=provenance,
                                     reason="The host-supplied image text did not match a known layout.")
    else:
        request = extraction_request([], provenance=provenance,
                                     reason="Images are not read by Wealth (no OCR). The host model may read the image; "
                                            "supply its text as source_text so numbers can be verified.")
    request["image"] = True
    return envelope("needs_extraction", {"provenance": provenance, "extraction_request": request},
                    sources=[provenance["ref"]])
