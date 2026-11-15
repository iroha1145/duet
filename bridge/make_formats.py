"""Real Office Make families; previews are bounded text, never Office execution."""
import html
import importlib.util
import io
from pathlib import PurePosixPath
from uuid import NAMESPACE_URL, uuid5
from .make_web import WEB_FORMATS

FORMATS = {
    "document": {"extension": ".docx", "module": "docx", "library": "python-docx", "title": "本地文档", "icon_key": "file"},
    "deck": {"extension": ".pptx", "module": "pptx", "library": "python-pptx", "title": "本地演示文稿", "icon_key": "slides"},
    "sheet": {"extension": ".xlsx", "module": "openpyxl", "library": "openpyxl", "title": "本地电子表格", "icon_key": "spreadsheet"},
}

# ArtifactTypeListing.slug and AiboTaskMake.artifact_type_id use the original
# Android CodeArtifactId serializer, which requires a lowercase UUID. These
# are stable IDs for our own local catalog, not recovered vendor template IDs.
TYPE_IDS = {kind: str(uuid5(NAMESPACE_URL, "duet-local-make-type:" + kind))
            for kind in ("page", *FORMATS, *WEB_FORMATS)}
TYPE_FAMILIES = {ident: kind for kind, ident in TYPE_IDS.items()}


def resolve_type(value):
    """Normalize native IDs and legacy API aliases to internal family names."""
    if not isinstance(value, str):
        return None
    return value if value in TYPE_IDS else TYPE_FAMILIES.get(value)


def wire_type(value):
    return TYPE_IDS.get(resolve_type(value))


def family(name):
    suffix = PurePosixPath(name).suffix.lower()
    if suffix in {".html", ".htm"}:
        return "page"
    return next((key for key, value in FORMATS.items() if suffix == value["extension"]), None)


def available(kind):
    kind = resolve_type(kind)
    if kind == "page" or kind in WEB_FORMATS:
        return True
    if kind not in FORMATS:
        return False
    spec = importlib.util.find_spec(FORMATS[kind]["module"])
    return spec is not None and spec.origin is not None


def starter(kind, title):
    """Use established package writers, with no network or code supplied by a user."""
    stream = io.BytesIO()
    if kind == "document":
        from docx import Document
        result = Document()
        result.add_heading(title, 0)
    elif kind == "deck":
        from pptx import Presentation
        result = Presentation()
        result.slides.add_slide(result.slide_layouts[0]).shapes.title.text = title
    elif kind == "sheet":
        from openpyxl import Workbook
        result = Workbook()
        result.active["A1"] = title
        # Titles are literal text, including a leading '='; never formulas.
        result.active["A1"].data_type = "s"
    else:
        raise ValueError("Unknown Office Make family")
    result.save(stream)
    return stream.getvalue()


def preview(title, metadata, download_path, visual=None):
    """All document content is escaped text, not an HTML sanitization problem."""
    text = metadata.get("text", "")
    if metadata.get("status") not in {"ready", "no_text"}:
        text = metadata.get("reason", "Text preview is unavailable. Download the original file.")
    warnings = "\n".join(metadata.get("warnings", []))
    visual = visual or {"status": "unavailable", "reason": "Visual rendering was not requested"}
    if visual.get("status") == "ready":
        images = "".join('<figure><img loading="lazy" alt="Page ' + str(number) + ' of ' + str(visual["page_count"])
                         + '" src="data:image/jpeg;base64,' + page + '"><figcaption>Page ' + str(number) + '</figcaption></figure>'
                         for number, page in enumerate(visual["pages"], 1))
        # Bound optional text duplication so the original native 2 MiB source
        # limit is respected even for entity-heavy extracted text.
        excerpt = text[:64000]
        if len(excerpt) != len(text):
            excerpt += "\n[Text preview excerpt; download the original for complete content.]"
        body = ('<p>Visual preview rendered locally with LibreOffice and Poppler. Fonts and pagination may differ from Microsoft Office.</p>'
                + images + '<details><summary>Accessible text preview</summary><pre>' + html.escape(excerpt) + '</pre></details>')
    else:
        body = ('<p>Visual preview unavailable: ' + html.escape(visual.get("reason", "Unknown renderer status"))
                + '</p><p>Text preview of the original Office file. Layout, embedded images and formatting are not rendered.</p><pre>'
                + html.escape(text) + '</pre>')
    return ("<!doctype html><html><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<title>" + html.escape(title) + "</title><style>body{font:16px/1.6 system-ui;margin:1rem;overflow-wrap:anywhere}"
            "pre{font:inherit;white-space:pre-wrap;overflow-wrap:anywhere}figure{margin:1rem 0}img{display:block;max-width:100%;height:auto;border:1px solid #ddd}"
            "figcaption{font-size:.85rem}</style></head><body><main><h1>" + html.escape(title)
            + "</h1><p><a href=\"" + html.escape(download_path, quote=True) + "\" download>Download original file</a></p>"
            + body + "<p>" + html.escape(warnings) + "</p></main></body></html>").encode("utf-8")
