from __future__ import annotations

from os import replace
from pathlib import Path, PurePosixPath
from posixpath import normpath
from tempfile import NamedTemporaryFile
from zipfile import ZIP_DEFLATED, ZipFile

from lxml import etree


_REMOVED_PART_PREFIXES = ("customxml/", "word/comments")
_REMOVED_PARTS = {
    "docprops/custom.xml",
    "docprops/thumbnail.jpeg",
    "word/people.xml",
}
_WORDPROCESSINGML_NAMESPACE = (
    "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
)
_ACCEPTED_REVISION_TAGS = {
    f"{{{_WORDPROCESSINGML_NAMESPACE}}}ins",
    f"{{{_WORDPROCESSINGML_NAMESPACE}}}moveTo",
}
_DISCARDED_REVISION_TAGS = {
    f"{{{_WORDPROCESSINGML_NAMESPACE}}}del",
    f"{{{_WORDPROCESSINGML_NAMESPACE}}}moveFrom",
}
_REMOVED_MARKUP_TAGS = {
    f"{{{_WORDPROCESSINGML_NAMESPACE}}}{local_name}"
    for local_name in {
        "commentRangeEnd",
        "commentRangeStart",
        "commentReference",
        "moveFromRangeEnd",
        "moveFromRangeStart",
        "moveToRangeEnd",
        "moveToRangeStart",
        "trackRevisions",
        "vanish",
        "webHidden",
    }
}


def sanitize_docx_package(path: Path) -> Path:
    """Remove patient-adjacent metadata and revision identifiers from a DOCX package."""

    package_path = path.resolve()
    temporary_path: Path | None = None
    try:
        with NamedTemporaryFile(
            prefix=f".{package_path.stem}-",
            suffix=".sanitizing.docx",
            dir=package_path.parent,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)

        with ZipFile(package_path) as source, ZipFile(
            temporary_path, "w", ZIP_DEFLATED
        ) as target:
            retained_names = {
                name.casefold()
                for name in source.namelist()
                if not _should_remove_part(name.casefold())
            }
            for info in source.infolist():
                name = info.filename
                lowered = name.casefold()
                if _should_remove_part(lowered):
                    continue

                data = source.read(name)
                if name == "[Content_Types].xml":
                    data = _sanitize_content_types(data, retained_names)
                elif lowered.endswith(".rels"):
                    data = _sanitize_relationships(data, name)
                elif lowered.endswith(".xml"):
                    data = _sanitize_xml(data)
                target.writestr(info, data)

        replace(temporary_path, package_path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return package_path


def _should_remove_part(lowered_name: str) -> bool:
    return lowered_name in _REMOVED_PARTS or lowered_name.startswith(
        _REMOVED_PART_PREFIXES
    )


def _sanitize_xml(data: bytes) -> bytes:
    root = etree.fromstring(data)
    etree.strip_elements(root, *_DISCARDED_REVISION_TAGS, with_tail=False)
    etree.strip_tags(root, *_ACCEPTED_REVISION_TAGS)

    for element in root.iter():
        for attribute in list(element.attrib):
            if etree.QName(attribute).localname.casefold().startswith("rsid"):
                del element.attrib[attribute]
    for element in list(root.iter()):
        for child in list(element):
            local_name = etree.QName(child).localname
            if child.tag in _REMOVED_MARKUP_TAGS or local_name in {
                "rsid",
                "rsidRoot",
                "rsids",
            }:
                element.remove(child)
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def _sanitize_content_types(data: bytes, retained_names: set[str]) -> bytes:
    root = etree.fromstring(data)
    namespace = "http://schemas.openxmlformats.org/package/2006/content-types"
    for override in list(root.findall(f"{{{namespace}}}Override")):
        part_name = override.get("PartName", "").lstrip("/").casefold()
        if _should_remove_part(part_name):
            root.remove(override)
    for default in list(root.findall(f"{{{namespace}}}Default")):
        extension = default.get("Extension", "").casefold()
        if extension == "jpeg" and not any(
            name.endswith(".jpeg") for name in retained_names
        ):
            root.remove(default)
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def _sanitize_relationships(data: bytes, relationships_path: str) -> bytes:
    root = etree.fromstring(data)
    namespace = "http://schemas.openxmlformats.org/package/2006/relationships"
    for relationship in list(root.findall(f"{{{namespace}}}Relationship")):
        if relationship.get("TargetMode", "").casefold() == "external":
            continue
        target = relationship.get("Target", "")
        if _should_remove_part(
            _resolve_relationship_target(relationships_path, target).casefold()
        ):
            root.remove(relationship)
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def _resolve_relationship_target(relationships_path: str, target: str) -> str:
    if target.startswith("/"):
        return normpath(target.lstrip("/"))

    path = PurePosixPath(relationships_path)
    owner_directory = path.parent.parent
    return normpath(str(owner_directory / target))
