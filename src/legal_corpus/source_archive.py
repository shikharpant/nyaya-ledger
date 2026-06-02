"""Source archive and deterministic text extraction helpers."""

from __future__ import annotations

import hashlib
from html.parser import HTMLParser
import json
import shutil
from pathlib import Path
from typing import Any


SOURCE_NAMES = ("source.txt", "source.pdf", "source.html")
PDF_EXTRACT_TEXT_OPTIONS = {"x_tolerance": 1}


def sha256_file(path: Path) -> str:
    """Return the SHA-256 checksum for a file."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _yaml_scalar(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value)
    if not text:
        return '""'
    if any(ch in text for ch in ":#[]{}\n") or text.strip() != text:
        return json.dumps(text, ensure_ascii=False)
    return text


def write_metadata_yaml(path: Path, metadata: dict[str, Any]) -> None:
    """Write a small YAML subset for source metadata.

    The writer intentionally supports only flat metadata because these files are
    evidence manifests, not rich configuration.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{key}: {_yaml_scalar(value)}" for key, value in sorted(metadata.items())]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def read_metadata_yaml(path: Path) -> dict[str, str]:
    """Read the simple flat YAML subset emitted by write_metadata_yaml."""
    metadata: dict[str, str] = {}
    if not path.exists():
        return metadata
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        value = value.strip()
        if value.startswith('"') and value.endswith('"'):
            value = json.loads(value)
        metadata[key.strip()] = value
    return metadata


def archive_source(input_path: Path, archive_dir: Path, metadata: dict[str, Any]) -> Path:
    """Copy a source file into an immutable source archive directory."""
    if input_path.suffix.lower() == ".pdf":
        target_name = "source.pdf"
    elif input_path.suffix.lower() in {".html", ".htm"}:
        target_name = "source.html"
    else:
        target_name = "source.txt"

    archive_dir.mkdir(parents=True, exist_ok=True)
    target_path = archive_dir / target_name
    shutil.copyfile(input_path, target_path)

    source_metadata = dict(metadata)
    source_metadata["source_file"] = target_name
    source_metadata["source_sha256"] = sha256_file(target_path)
    write_metadata_yaml(archive_dir / "metadata.yaml", source_metadata)
    return target_path


def find_source_file(archive_dir: Path) -> Path:
    for name in SOURCE_NAMES:
        candidate = archive_dir / name
        if candidate.exists():
            return candidate
    raise FileNotFoundError(f"No source file found in {archive_dir}")


def _extract_pdf_text(path: Path) -> list[dict[str, Any]]:
    try:
        import pdfplumber
    except ImportError as exc:
        raise RuntimeError("PDF extraction requires pdfplumber") from exc

    pages = []
    offset = 0
    with pdfplumber.open(path) as pdf:
        for index, page in enumerate(pdf.pages, start=1):
            text = page.extract_text(**PDF_EXTRACT_TEXT_OPTIONS) or ""
            start = offset
            end = start + len(text)
            pages.append({"page_number": index, "start": start, "end": end, "text": text})
            offset = end + 2
    return pages


class _TextHTMLParser(HTMLParser):
    """Extract visible text from simple official HTML source documents."""

    BLOCK_TAGS = {
        "address",
        "article",
        "aside",
        "blockquote",
        "br",
        "caption",
        "dd",
        "div",
        "dl",
        "dt",
        "fieldset",
        "figcaption",
        "figure",
        "footer",
        "form",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "tbody",
        "td",
        "tfoot",
        "th",
        "thead",
        "tr",
        "ul",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript"}:
            self._skip_depth += 1
            return
        if tag in self.BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self._skip_depth:
            self._skip_depth -= 1
            return
        if tag in self.BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = " ".join(data.split())
        if text:
            self._chunks.append(text)
            self._chunks.append(" ")

    def text(self) -> str:
        lines = []
        for raw_line in "".join(self._chunks).splitlines():
            line = " ".join(raw_line.split())
            if line:
                lines.append(line)
        return "\n".join(lines)


def _extract_html_text(path: Path) -> list[dict[str, Any]]:
    parser = _TextHTMLParser()
    parser.feed(path.read_text(encoding="utf-8", errors="replace"))
    text = parser.text()
    return [{"page_number": 1, "start": 0, "end": len(text), "text": text}]


def extract_source_text(archive_dir: Path) -> dict[str, Any]:
    """Extract source text and preserve page/character offsets."""
    source_path = find_source_file(archive_dir)
    suffix = source_path.suffix.lower()

    if suffix == ".pdf":
        pages = _extract_pdf_text(source_path)
    elif suffix in {".html", ".htm"}:
        pages = _extract_html_text(source_path)
    else:
        text = source_path.read_text(encoding="utf-8")
        pages = [{"page_number": 1, "start": 0, "end": len(text), "text": text}]

    full_text = "\n\n".join(page["text"] for page in pages)
    extracted = {
        "source_file": source_path.name,
        "source_sha256": sha256_file(source_path),
        "text": full_text,
        "pages": pages,
    }
    output_path = archive_dir / "extracted_text.json"
    output_path.write_text(json.dumps(extracted, indent=2, ensure_ascii=False), encoding="utf-8")
    return extracted


def build_paragraph_structure(extracted: dict[str, Any]) -> dict[str, Any]:
    """Build deterministic paragraph spans from extracted text."""
    from .structure_parser import parse_structure_paragraphs

    return parse_structure_paragraphs(extracted)


def write_structure_json(archive_dir: Path, structure: dict[str, Any]) -> Path:
    output_path = archive_dir / "structure.json"
    output_path.write_text(json.dumps(structure, indent=2, ensure_ascii=False), encoding="utf-8")
    return output_path
