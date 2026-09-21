"""Upload safety: bounded reads, magic-byte sniffing, path checks, PDF risk flags."""

from __future__ import annotations

import io
import os
from pathlib import Path
import re
import stat
from typing import Any, Iterable
import zipfile


MB = 1024 * 1024
LIMITS = {"pdf": 25 * MB, "csv": 5 * MB, "xlsx": 15 * MB, "image": 20 * MB}
MAX_PDF_PAGES = 200
MAX_XLSX_UNCOMPRESSED = 100 * MB
MAX_ROWS = 50_000
IMAGE_KINDS = frozenset({"png", "jpeg", "gif", "webp", "heic", "tiff"})


class UnsafeInput(ValueError):
    """The upload was refused before parsing."""


def resolve_upload(path: str | os.PathLike[str], allowed_roots: Iterable[str | os.PathLike[str]] | None = None) -> Path:
    """Resolve a local upload path, refusing traversal, symlinks and non-regular files.

    When ``allowed_roots`` is supplied the resolved file must sit inside one of
    them; hosts should always pass their upload directory.
    """
    raw = os.fspath(path)
    if not raw or "\x00" in raw:
        raise UnsafeInput("upload path is empty or contains a NUL byte")
    candidate = Path(raw).expanduser()
    if ".." in candidate.parts:
        raise UnsafeInput("upload path must not contain '..'")
    if candidate.is_symlink():
        raise UnsafeInput("upload path must not be a symbolic link")
    try:
        resolved = candidate.resolve(strict=True)
    except (FileNotFoundError, RuntimeError) as exc:
        raise UnsafeInput("upload file does not exist") from exc
    if allowed_roots is not None:
        roots = [Path(root).expanduser().resolve() for root in allowed_roots]
        if not any(resolved == root or root in resolved.parents for root in roots):
            raise UnsafeInput("upload path is outside the allowed upload directories")
    return resolved


def read_upload(path: Path, max_bytes: int = max(LIMITS.values())) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise UnsafeInput("upload must be a regular file")
        if info.st_size > max_bytes:
            raise UnsafeInput(f"upload exceeds {max_bytes // MB} MB")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            data = handle.read(max_bytes + 1)
    finally:
        os.close(fd)
    if len(data) > max_bytes:
        raise UnsafeInput(f"upload exceeds {max_bytes // MB} MB")
    return data


def sniff(data: bytes) -> str:
    """Identify content from magic bytes, never from the file extension."""
    head = data[:16]
    if data[:1024].lstrip(b"\xef\xbb\xbf\r\n\t ").startswith(b"%PDF-") or b"%PDF-" in data[:1024]:
        return "pdf"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if head[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[4:8] == b"ftyp" and data[8:12] in (b"heic", b"heix", b"mif1", b"msf1", b"heim", b"hevc"):
        return "heic"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    if head.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                names = set(archive.namelist())
        except zipfile.BadZipFile:
            return "unknown"
        return "xlsx" if "xl/workbook.xml" in names and "[Content_Types].xml" in names else "zip"
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        return "ole"
    sample = data[:8192]
    if sample.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = sample[: len(sample) // 2 * 2].decode("utf-16", errors="replace")
        return "csv" if "\x00" not in text and text.count("�") <= 2 else "binary"
    if b"\x00" in sample:
        return "binary"
    try:
        sample.decode("utf-8")
        return "csv"
    except UnicodeDecodeError as exc:
        if exc.start >= len(sample) - 4:
            return "csv"
    try:
        text = sample.decode("cp1252")
    except UnicodeDecodeError:
        return "binary"
    printable = sum(ch.isprintable() or ch in "\r\n\t" for ch in text)
    return "csv" if printable >= 0.97 * max(1, len(text)) else "binary"


def check_xlsx(data: bytes) -> list[str]:
    """Refuse zip bombs; flag macro projects (never executed, only noted)."""
    warnings: list[str] = []
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        total = 0
        for info in archive.infolist():
            total += info.file_size
            if info.compress_size and info.file_size / info.compress_size > 200:
                raise UnsafeInput("workbook entry has an implausible compression ratio")
            if "vbaproject" in info.filename.lower():
                warnings.append("Workbook contains a macro project; macros were not executed.")
            if info.filename.lower().startswith("xl/embeddings/"):
                warnings.append("Workbook contains embedded objects; they were ignored.")
        if total > MAX_XLSX_UNCOMPRESSED:
            raise UnsafeInput("workbook expands beyond the uncompressed size limit")
    return list(dict.fromkeys(warnings))


_RISK_TOKENS = (
    (re.compile(rb"/JavaScript\b"), "javascript"),
    (re.compile(rb"/JS[\s/<(\[]"), "javascript"),
    (re.compile(rb"/EmbeddedFiles?\b"), "embedded_file"),
    (re.compile(rb"/Launch\b"), "launch_action"),
    (re.compile(rb"/RichMedia\b"), "rich_media"),
    (re.compile(rb"/XFA\b"), "xfa_form"),
    (re.compile(rb"/SubmitForm\b"), "submit_form"),
    (re.compile(rb"/ImportData\b"), "import_data"),
)
_ACTIVE_ACTIONS = {"/JavaScript": "javascript", "/Launch": "launch_action", "/SubmitForm": "submit_form",
                   "/ImportData": "import_data", "/GoToR": "remote_goto", "/GoToE": "embedded_goto",
                   "/RichMediaExecute": "rich_media"}


def _resolve(value: Any) -> Any:
    try:
        return value.get_object() if hasattr(value, "get_object") else value
    except Exception:
        return None


def _action_risks(action: Any, found: set[str], depth: int = 0) -> None:
    action = _resolve(action)
    if depth > 8 or not hasattr(action, "get"):
        return
    kind = action.get("/S")
    if kind in _ACTIVE_ACTIONS:
        found.add(_ACTIVE_ACTIONS[kind])
    if "/JS" in action:
        found.add("javascript")
    following = _resolve(action.get("/Next"))
    for item in following if isinstance(following, list) else [following] if following is not None else []:
        _action_risks(item, found, depth + 1)


def pdf_risks(reader: Any, data: bytes) -> list[str]:
    """Flag active or embedded PDF content.  Nothing is executed or extracted."""
    found = {label for pattern, label in _RISK_TOKENS if pattern.search(data)}
    try:
        root = _resolve(reader.trailer["/Root"])
        names = _resolve(root.get("/Names")) if root is not None else None
        if names is not None and hasattr(names, "get"):
            if "/JavaScript" in names:
                found.add("javascript")
            if "/EmbeddedFiles" in names:
                found.add("embedded_file")
        if root is not None:
            _action_risks(root.get("/OpenAction"), found)
            additional = _resolve(root.get("/AA"))
            if hasattr(additional, "values"):
                for action in additional.values():
                    _action_risks(action, found)
            form = _resolve(root.get("/AcroForm"))
            if hasattr(form, "get") and "/XFA" in form:
                found.add("xfa_form")
        for page in list(reader.pages)[:MAX_PDF_PAGES]:
            additional = _resolve(page.get("/AA"))
            if hasattr(additional, "values"):
                for action in additional.values():
                    _action_risks(action, found)
            for annotation in _resolve(page.get("/Annots")) or []:
                annotation = _resolve(annotation)
                if not hasattr(annotation, "get"):
                    continue
                _action_risks(annotation.get("/A"), found)
                if annotation.get("/Subtype") == "/FileAttachment":
                    found.add("embedded_file")
    except Exception:
        found.add("unreadable_structure")
    return sorted(found)
