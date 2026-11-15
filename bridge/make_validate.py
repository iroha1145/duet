"""Make-only OOXML validation inside attachment_extract's bounded child.

Read-only package checks precede mature Office readers. Every XML part is
parsed without DTD/entities/external resolution before those readers see it.
This module never renders, executes formulas/macros, or follows external URLs.
"""
import io
import posixpath
from pathlib import PurePosixPath

try:
    from .attachment_extract import _Package, R
    from .attachment_policy import AttachmentError
except ImportError:
    if __package__:
        raise
    from attachment_extract import _Package, R
    from attachment_policy import AttachmentError

CT = "{http://schemas.openxmlformats.org/package/2006/content-types}"
OFFICE_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
RELS_MIME = "application/vnd.openxmlformats-package.relationships+xml"
SPECS = {
    ".docx": ("word/document.xml", "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
              "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}document"),
    ".pptx": ("ppt/presentation.xml", "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml",
              "{http://schemas.openxmlformats.org/presentationml/2006/main}presentation"),
    ".xlsx": ("xl/workbook.xml", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
              "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}workbook"),
}


def invalid(message):
    raise AttachmentError(message, 422, "invalid_office_package")


def validate_package(data, name):
    suffix = PurePosixPath(name).suffix.lower()
    if suffix not in SPECS:
        invalid("Make Office validation requires DOCX, PPTX or XLSX")
    main, main_mime, root_tag = SPECS[suffix]
    package = _Package(data)
    try:
        manifest = package.xml("[Content_Types].xml")
        if manifest.tag != CT + "Types":
            invalid("Office content type manifest has the wrong root")
        defaults, overrides = {}, {}
        for item in manifest:
            mime = item.get("ContentType", "")
            if not mime or len(mime) > 256:
                invalid("Office package has an invalid content type")
            if item.tag == CT + "Default":
                key = item.get("Extension", "")
                if not key or key in defaults or any(char in key for char in "/\\:"):
                    invalid("Office package has duplicate or invalid default types")
                defaults[key] = mime
            elif item.tag == CT + "Override":
                part = item.get("PartName", "")
                if not part.startswith("/") or part[1:] not in package.names or part in overrides:
                    invalid("Office content type override references an invalid part")
                overrides[part] = mime
            else:
                invalid("Office content type manifest has an unknown entry")
        if overrides.get("/" + main) != main_mime:
            invalid("Office main content type does not match its file extension")
        root_relationships = None
        for member in sorted(package.names):
            if member == "[Content_Types].xml" or member.endswith("/"):
                continue
            extension = "rels" if member.endswith(".rels") else PurePosixPath(member).suffix.lstrip(".")
            mime = overrides.get("/" + member, defaults.get(extension))
            if mime is None:
                invalid("Office package part has no content type")
            xml = None
            if mime.endswith("+xml") or mime in {"application/xml", "text/xml"}:
                xml = package.xml(member)
            if member.endswith(".rels"):
                if mime != RELS_MIME or xml is None or xml.tag != R + "Relationships":
                    invalid("Office relationship part has an invalid content type or root")
                if member == "_rels/.rels":
                    source = ""
                    root_relationships = xml
                else:
                    part = PurePosixPath(member)
                    if part.parent.name != "_rels":
                        invalid("Office relationship part has an invalid name")
                    source = str(part.parent.parent / part.name[:-5])
                    if source not in package.names:
                        invalid("Office relationships have no source part")
                ids = set()
                for relation in xml:
                    rid, target = relation.get("Id"), relation.get("Target", "")
                    if (relation.tag != R + "Relationship" or not rid or rid in ids or not target
                            or not relation.get("Type") or relation.get("TargetMode") not in {None, "Internal", "External"}):
                        invalid("Office package has duplicate or invalid relationships")
                    ids.add(rid)
                    if relation.get("TargetMode") == "External":
                        continue  # Never read/fetch external relations here.
                    if any(char in target for char in "\\:\0"):
                        invalid("Office package has an invalid internal relationship")
                    resolved = posixpath.normpath(target.lstrip("/") if target.startswith("/")
                                                   else str(PurePosixPath(source).parent / target))
                    if resolved not in package.names or resolved.startswith("../"):
                        invalid("Office relationship target is absent or escapes its package")
        if root_relationships is None:
            invalid("Office package is missing its root relationships")
        roots = [relation for relation in root_relationships if relation.get("Type") == OFFICE_REL]
        if len(roots) != 1 or roots[0].get("TargetMode") == "External" or roots[0].get("Target", "").lstrip("/") != main:
            invalid("Office package has no matching Office document root")
        if package.xml(main).tag != root_tag:
            invalid("Office document root does not match its declared format")
        # Mature readers exercise the actual package contracts rather than
        # treating a ZIP with text-bearing XML as a usable Office document.
        if suffix == ".docx":
            from docx import Document
            Document(io.BytesIO(data))
        elif suffix == ".pptx":
            from pptx import Presentation
            Presentation(io.BytesIO(data))
        else:
            from openpyxl import load_workbook
            workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=False, keep_links=False)
            workbook.close()
    except AttachmentError:
        raise
    except ImportError:
        raise AttachmentError("The installed Office reader is unavailable", 503, "extractor_unavailable") from None
    except Exception:
        invalid("Office package cannot be opened by its Office reader")
    finally:
        package.close()
