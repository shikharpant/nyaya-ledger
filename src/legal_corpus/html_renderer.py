"""Static HTML rendering rebuilt from canonical corpus XML."""

from __future__ import annotations

import html
import json
import re
from pathlib import Path
from typing import Any

from .api_payload import build_api_payload


STYLE = """
:root {
  color-scheme: light;
  font-family: Arial, sans-serif;
  line-height: 1.5;
  color: #202124;
  background: #f7f8fa;
}
body {
  margin: 0;
}
header,
main {
  max-width: 960px;
  margin: 0 auto;
  padding: 24px;
}
header {
  border-bottom: 1px solid #d7dce2;
  background: #ffffff;
}
h1 {
  margin: 0 0 8px;
  font-size: 28px;
}
h2 {
  margin-top: 28px;
  font-size: 20px;
}
a {
  color: #0645ad;
}
.meta,
.crumb {
  color: #59636e;
  font-size: 14px;
}
.document-list {
  display: grid;
  gap: 12px;
  padding: 0;
  list-style: none;
}
.document-list li,
.provision,
.reference-list {
  background: #ffffff;
  border: 1px solid #d7dce2;
  border-radius: 6px;
  padding: 14px;
}
.document-title {
  font-weight: 700;
}
.text {
  white-space: pre-wrap;
}
.reference-list {
  list-style-position: inside;
}
.source-span {
  font-family: monospace;
}
""".strip()


def _slug(canonical_id: str) -> str:
    value = canonical_id.strip("/").replace("/", "__")
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value) or "index"


def _escape(value: str) -> str:
    return html.escape(value or "", quote=True)


def _document_href(document: dict[str, Any]) -> str:
    return f"documents/{_slug(document['canonical_id'])}.html"


def _reference_items(references: list[dict[str, str]]) -> str:
    if not references:
        return "<p class=\"meta\">No references recorded.</p>"
    items = []
    for ref in references:
        label = ref.get("showAs") or ref.get("target", "")
        items.append(
            "<li>"
            f"<span>{_escape(ref.get('type', 'REFERS_TO'))}</span>: "
            f"<code>{_escape(ref.get('target', ''))}</code> "
            f"<span class=\"meta\">{_escape(label)}</span>"
            "</li>"
        )
    return f"<ol class=\"reference-list\">{''.join(items)}</ol>"


def _source_span_text(span: dict[str, Any]) -> str:
    if not span:
        return ""
    start = span.get("sourceStart", "")
    end = span.get("sourceEnd", "")
    node_type = span.get("sourceNodeType", "")
    confidence = span.get("sourceConfidence", "")
    parts = [f"source {start}:{end}"]
    if node_type:
        parts.append(str(node_type))
    if confidence:
        parts.append(f"confidence {confidence}")
    return " · ".join(parts)


def _layout(title: str, body: str) -> str:
    return (
        "<!doctype html>\n"
        "<html lang=\"en\">\n"
        "<head>\n"
        "  <meta charset=\"utf-8\">\n"
        "  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        f"  <title>{_escape(title)}</title>\n"
        "  <link rel=\"stylesheet\" href=\"../assets/style.css\">\n"
        "</head>\n"
        "<body>\n"
        f"{body}\n"
        "</body>\n"
        "</html>\n"
    )


def _index_layout(title: str, body: str) -> str:
    return (
        "<!doctype html>\n"
        "<html lang=\"en\">\n"
        "<head>\n"
        "  <meta charset=\"utf-8\">\n"
        "  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        f"  <title>{_escape(title)}</title>\n"
        "  <link rel=\"stylesheet\" href=\"assets/style.css\">\n"
        "</head>\n"
        "<body>\n"
        f"{body}\n"
        "</body>\n"
        "</html>\n"
    )


def _render_index(payload: dict[str, Any]) -> str:
    rows = []
    for document in payload["documents"]:
        rows.append(
            "<li>"
            f"<div class=\"document-title\"><a href=\"{_escape(_document_href(document))}\">"
            f"{_escape(document.get('title') or document['canonical_id'])}</a></div>"
            f"<div class=\"meta\">{_escape(document.get('document_type', ''))} · "
            f"{_escape(document.get('canonical_id', ''))}</div>"
            "</li>"
        )
    body = (
        "<header>"
        "<h1>Git for Law Corpus</h1>"
        f"<div class=\"meta\">{payload['stats']['documents']} documents · "
        f"{payload['stats']['provisions']} provisions · {payload['stats']['references']} references</div>"
        "</header>"
        "<main>"
        "<h2>Documents</h2>"
        f"<ol class=\"document-list\">{''.join(rows)}</ol>"
        "</main>"
    )
    return _index_layout("Git for Law Corpus", body)


def _render_document(document: dict[str, Any], provisions: list[dict[str, Any]]) -> str:
    provision_html = []
    for provision in provisions:
        title = provision.get("title") or provision.get("number") or provision["canonical_id"]
        provision_html.append(
            "<section class=\"provision\">"
            f"<h2>{_escape(title)}</h2>"
            f"<div class=\"meta\"><code>{_escape(provision['canonical_id'])}</code></div>"
            f"<div class=\"meta source-span\">{_escape(_source_span_text(provision.get('source_span', {})))}</div>"
            f"<p class=\"text\">{_escape(provision.get('text', ''))}</p>"
            "</section>"
        )

    body = (
        "<header>"
        "<div class=\"crumb\"><a href=\"../index.html\">Corpus</a></div>"
        f"<h1>{_escape(document.get('title') or document['canonical_id'])}</h1>"
        f"<div class=\"meta\">{_escape(document.get('document_type', ''))} · "
        f"{_escape(document.get('canonical_id', ''))}</div>"
        f"<div class=\"meta\">Effective: {_escape(document.get('effective_from', ''))} · "
        f"Review: {_escape(document.get('review_status', ''))}</div>"
        "</header>"
        "<main>"
        "<h2>Text</h2>"
        f"<p class=\"text\">{_escape(document.get('text', ''))}</p>"
        f"<div class=\"meta source-span\">{len(document.get('source_spans', []))} source spans</div>"
        "<h2>References</h2>"
        f"{_reference_items(document.get('references', []))}"
        "<h2>Provisions</h2>"
        f"{''.join(provision_html)}"
        "</main>"
    )
    return _layout(document.get("title") or document["canonical_id"], body)


def build_html_site(corpus_dir: Path) -> dict[str, str]:
    payload = build_api_payload(corpus_dir)
    files: dict[str, str] = {
        "assets/style.css": STYLE + "\n",
        "index.html": _render_index(payload),
        "data/corpus_api.json": json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
    }
    provisions_by_document: dict[str, list[dict[str, Any]]] = {}
    for provision in payload["provisions"]:
        provisions_by_document.setdefault(provision.get("document_id", ""), []).append(provision)
    for document in payload["documents"]:
        path = _document_href(document)
        files[path] = _render_document(document, provisions_by_document.get(document["canonical_id"], []))
    return files


def write_html_site(corpus_dir: Path, output_dir: Path) -> dict[str, int]:
    files = build_html_site(corpus_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    for relative_path, content in files.items():
        path = output_dir / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return {"files": len(files), "documents": sum(1 for path in files if path.startswith("documents/"))}
