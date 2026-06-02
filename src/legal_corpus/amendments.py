"""Canonical corpus amendment planning and application."""

from __future__ import annotations

import json
import shutil
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from src.anchor_resolver import AnchorNotFoundError, resolve_anchor
from src.mutation_parser import ParsedMutation, parse_notification_offline

from .paths import expected_corpus_relative_path
from .renderer import canonical_rule_id, canonicalize_legacy_reference, render_rule, write_xml
from .source_archive import extract_source_text, read_metadata_yaml
from .validator import validate_corpus


@dataclass
class CorpusReference:
    canonical_id: str
    path: str
    e_id: str | None = None
    element_tag: str | None = None


@dataclass
class AmendmentPlanItem:
    mutation_id: str
    operation: str
    legacy_target: str
    canonical_target: str
    status: str
    target_file: str | None = None
    output_file: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    anchor: str | None = None
    anchor_position: str | None = None
    notes: list[str] = field(default_factory=list)


@dataclass
class AmendmentPlan:
    notification_id: str
    source_dir: str
    corpus_dir: str
    items: list[AmendmentPlanItem]

    @property
    def unresolved_count(self) -> int:
        return sum(1 for item in self.items if item.status not in {"ready", "applied"})

    @property
    def ready_count(self) -> int:
        return sum(1 for item in self.items if item.status == "ready")

    def to_dict(self) -> dict[str, Any]:
        return {
            "notification_id": self.notification_id,
            "source_dir": self.source_dir,
            "corpus_dir": self.corpus_dir,
            "ready_count": self.ready_count,
            "unresolved_count": self.unresolved_count,
            "items": [asdict(item) for item in self.items],
        }


@dataclass
class PromotionResult:
    review_corpus_dir: str
    target_corpus_dir: str
    manifest_path: str
    approved: bool
    committed: bool
    added: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    commit_sha: str | None = None

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def changed_paths(self) -> list[str]:
        return self.added + self.modified + self.removed

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _properties(root: ET.Element) -> dict[str, str]:
    props = {}
    for prop in root.findall(".//property"):
        name = prop.attrib.get("name")
        if name:
            props[name] = prop.attrib.get("value", "")
    return props


def build_corpus_index(corpus_dir: Path) -> dict[str, CorpusReference]:
    """Index document IDs and provision-level IDs from corpus XML."""
    index: dict[str, CorpusReference] = {}
    for path in sorted(corpus_dir.rglob("*.xml")):
        tree = ET.parse(path)
        root = tree.getroot()
        props = _properties(root)
        canonical_id = props.get("canonical_id")
        if canonical_id:
            index[canonical_id] = CorpusReference(canonical_id=canonical_id, path=str(path))

        for element in root.iter():
            refers_to = element.attrib.get("refersTo")
            if refers_to:
                index[refers_to] = CorpusReference(
                    canonical_id=refers_to,
                    path=str(path),
                    e_id=element.attrib.get("eId"),
                    element_tag=element.tag,
                )
    return index


def _load_notification_text(source_dir: Path) -> tuple[str, dict[str, str]]:
    metadata = read_metadata_yaml(source_dir / "metadata.yaml")
    extracted_path = source_dir / "extracted_text.json"
    if extracted_path.exists():
        extracted = json.loads(extracted_path.read_text(encoding="utf-8"))
    else:
        extracted = extract_source_text(source_dir)
    return extracted["text"], metadata


def _notification_id(metadata: dict[str, str], source_dir: Path) -> str:
    return metadata.get("canonical_id") or source_dir.name


def _plan_splice(
    mutation: ParsedMutation,
    canonical_target: str,
    target_ref: CorpusReference,
) -> AmendmentPlanItem:
    item = AmendmentPlanItem(
        mutation_id=mutation.mutation_id,
        operation=mutation.operation,
        legacy_target=mutation.target_node_path,
        canonical_target=canonical_target,
        status="ready",
        target_file=target_ref.path,
        payload=mutation.payload,
        anchor=mutation.anchor,
        anchor_position=mutation.anchor_position,
    )
    if not mutation.anchor:
        item.status = "anchor_missing"
        item.notes.append("SPLICE requires an anchor.")
        return item

    try:
        tree = ET.parse(target_ref.path)
        target_element = _find_element_by_refers_to(tree.getroot(), canonical_target)
        paragraph = _find_content_paragraph(target_element) if target_element is not None else None
        if paragraph is None:
            item.status = "target_missing"
            item.notes.append("Could not find textual paragraph for target.")
            return item
        resolve_anchor(paragraph.text or "", mutation.anchor, canonical_target)
    except AnchorNotFoundError as exc:
        item.status = "anchor_missing"
        item.notes.append(str(exc))
    return item


def _plan_insert_sibling(
    mutation: ParsedMutation,
    canonical_target: str,
    target_ref: CorpusReference,
    corpus_dir: Path,
) -> AmendmentPlanItem:
    new_label = mutation.payload.get("label")
    item = AmendmentPlanItem(
        mutation_id=mutation.mutation_id,
        operation=mutation.operation,
        legacy_target=mutation.target_node_path,
        canonical_target=canonical_target,
        status="ready",
        target_file=target_ref.path,
        payload=mutation.payload,
    )
    if mutation.payload.get("node_type") != "rule" or not new_label:
        item.status = "unsupported"
        item.notes.append("Only INSERT_SIBLING for rule nodes is supported in canonical XML apply.")
        return item
    output_rel = _rule_output_relative_path(new_label)
    item.output_file = str(corpus_dir / output_rel)
    return item


def plan_amendments(source_dir: Path, corpus_dir: Path) -> AmendmentPlan:
    """Parse a notification source archive and resolve mutations against corpus XML."""
    text, metadata = _load_notification_text(source_dir)
    parsed = parse_notification_offline(text)
    index = build_corpus_index(corpus_dir)
    items: list[AmendmentPlanItem] = []

    for mutation in parsed.mutations:
        canonical_target = canonicalize_legacy_reference(mutation.target_node_path)
        target_ref = index.get(canonical_target)
        if not target_ref:
            items.append(
                AmendmentPlanItem(
                    mutation_id=mutation.mutation_id,
                    operation=mutation.operation,
                    legacy_target=mutation.target_node_path,
                    canonical_target=canonical_target,
                    status="target_missing",
                    payload=mutation.payload,
                    anchor=mutation.anchor,
                    anchor_position=mutation.anchor_position,
                    notes=["Target canonical ID was not found in corpus."],
                )
            )
            continue

        if mutation.operation == "SPLICE":
            items.append(_plan_splice(mutation, canonical_target, target_ref))
        elif mutation.operation == "INSERT_SIBLING":
            items.append(_plan_insert_sibling(mutation, canonical_target, target_ref, corpus_dir))
        else:
            items.append(
                AmendmentPlanItem(
                    mutation_id=mutation.mutation_id,
                    operation=mutation.operation,
                    legacy_target=mutation.target_node_path,
                    canonical_target=canonical_target,
                    status="unsupported",
                    target_file=target_ref.path,
                    payload=mutation.payload,
                    anchor=mutation.anchor,
                    anchor_position=mutation.anchor_position,
                    notes=[f"Operation {mutation.operation} is not implemented for canonical XML apply."],
                )
            )

    return AmendmentPlan(
        notification_id=_notification_id(metadata, source_dir),
        source_dir=str(source_dir),
        corpus_dir=str(corpus_dir),
        items=items,
    )


def write_plan(plan: AmendmentPlan, output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(plan.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    return output_path


def _sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative_xml_paths(root: Path) -> set[Path]:
    return {path.relative_to(root) for path in root.rglob("*.xml")}


def _write_promotion_manifest(result: PromotionResult) -> None:
    path = Path(result.manifest_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")


def _git_commit_paths(repo_root: Path, paths: list[Path], message: str) -> str:
    if not paths:
        raise ValueError("No paths to commit")
    relative_paths = [str(path.relative_to(repo_root)) if path.is_absolute() else str(path) for path in paths]
    subprocess.run(["git", "add", *relative_paths], cwd=repo_root, check=True)
    subprocess.run(["git", "commit", "-m", message], cwd=repo_root, check=True)
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def promote_amended_corpus(
    review_corpus_dir: Path,
    target_corpus_dir: Path,
    manifest_path: Path,
    approve: bool = False,
    git_commit: bool = False,
    commit_message: str | None = None,
    repo_root: Path | None = None,
) -> PromotionResult:
    """Validate and optionally promote reviewed corpus XML into the canonical corpus."""
    result = PromotionResult(
        review_corpus_dir=str(review_corpus_dir),
        target_corpus_dir=str(target_corpus_dir),
        manifest_path=str(manifest_path),
        approved=approve,
        committed=False,
    )

    validation = validate_corpus(review_corpus_dir)
    result.warnings.extend(validation.warnings)
    if validation.errors:
        result.errors.extend(validation.errors)
        _write_promotion_manifest(result)
        return result

    review_paths = _relative_xml_paths(review_corpus_dir)
    target_paths = _relative_xml_paths(target_corpus_dir) if target_corpus_dir.exists() else set()

    for relative_path in sorted(review_paths):
        review_file = review_corpus_dir / relative_path
        target_file = target_corpus_dir / relative_path
        if relative_path not in target_paths:
            result.added.append(str(target_file))
        elif _sha256(review_file) != _sha256(target_file):
            result.modified.append(str(target_file))
        else:
            result.unchanged.append(str(target_file))

    for relative_path in sorted(target_paths - review_paths):
        result.removed.append(str(target_corpus_dir / relative_path))

    if approve:
        for relative_path in sorted(review_paths):
            review_file = review_corpus_dir / relative_path
            target_file = target_corpus_dir / relative_path
            target_file.parent.mkdir(parents=True, exist_ok=True)
            if relative_path not in target_paths or _sha256(review_file) != _sha256(target_file):
                shutil.copyfile(review_file, target_file)

        for removed_path in result.removed:
            Path(removed_path).unlink(missing_ok=True)

    if git_commit:
        if not approve:
            result.errors.append("git_commit requires approve=True")
            _write_promotion_manifest(result)
            return result
        result.committed = True
        _write_promotion_manifest(result)
        repo_root = repo_root or Path.cwd()
        commit_paths = [Path(path) for path in result.changed_paths] + [manifest_path]
        result.commit_sha = _git_commit_paths(repo_root, commit_paths, commit_message or "Promote canonical corpus amendments")
    else:
        _write_promotion_manifest(result)

    return result


def _find_element_by_refers_to(root: ET.Element, canonical_id: str) -> ET.Element | None:
    for element in root.iter():
        if element.attrib.get("refersTo") == canonical_id:
            return element
    return None


def _find_content_paragraph(element: ET.Element | None) -> ET.Element | None:
    if element is None:
        return None
    return element.find("./content/p")


def _prepare_insert_text(original: str, insert_text: str, insert_pos: int) -> tuple[str, list[str]]:
    notes = []
    prepared = insert_text
    if prepared and not prepared[0].isspace() and insert_pos > 0 and not original[insert_pos - 1].isspace():
        prepared = " " + prepared
        notes.append("Inserted leading space at splice boundary.")
    if (
        prepared
        and not prepared[-1].isspace()
        and insert_pos < len(original)
        and not original[insert_pos].isspace()
    ):
        prepared = prepared + " "
        notes.append("Inserted trailing space at splice boundary.")
    return prepared, notes


def _apply_splice_to_tree(tree: ET.ElementTree, canonical_target: str, item: AmendmentPlanItem) -> None:
    root = tree.getroot()
    target_element = _find_element_by_refers_to(root, canonical_target)
    paragraph = _find_content_paragraph(target_element)
    if paragraph is None:
        raise ValueError(f"Could not find textual paragraph for {canonical_target}")

    original = paragraph.text or ""
    anchor = item.anchor or ""
    match = resolve_anchor(original, anchor, canonical_target)
    position = item.anchor_position or "after"
    insert_pos = match.position + len(match.matched_text) if position == "after" else match.position
    insert_text, notes = _prepare_insert_text(original, item.payload.get("content", ""), insert_pos)
    paragraph.text = original[:insert_pos] + insert_text + original[insert_pos:]
    item.notes.extend(notes)


def _rule_output_relative_path(label: str) -> Path:
    return expected_corpus_relative_path(canonical_rule_id(label), "rule")


def _target_chapter(target_file: Path) -> dict[str, str]:
    tree = ET.parse(target_file)
    chapter = tree.find(".//chapter")
    if chapter is None:
        return {"number": "", "title": ""}
    num = chapter.findtext("./num") or ""
    heading = chapter.findtext("./heading") or ""
    return {"number": num, "title": heading}


def _apply_insert_sibling(
    item: AmendmentPlanItem,
    output_corpus_dir: Path,
    notification_metadata: dict[str, str],
) -> Path:
    payload = item.payload
    label = payload["label"]
    target_file = Path(item.target_file or "")
    chapter = _target_chapter(target_file)
    metadata = {
        "source_type": "amendment-generated",
        "source_url": notification_metadata.get("source_url", ""),
        "source_sha256": notification_metadata.get("source_sha256", ""),
        "publication_date": notification_metadata.get("publication_date", ""),
        "effective_from": notification_metadata.get("effective_from", ""),
        "issuing_authority": notification_metadata.get("issuing_authority", "/in/authority/cbic"),
        "review_status": "generated",
        "parser_version": "offline-mutation-v1",
        "source_notification": notification_metadata.get("canonical_id", ""),
    }
    rule = {
        "label": label,
        "heading": payload.get("heading", ""),
        "content": payload.get("content", ""),
        "subrules": [],
    }
    output_path = output_corpus_dir / _rule_output_relative_path(label)
    write_xml(render_rule(rule, chapter, metadata), output_path)
    return output_path


def apply_amendments(
    source_dir: Path,
    corpus_dir: Path,
    output_corpus_dir: Path,
    allow_partial: bool = False,
) -> AmendmentPlan:
    """Apply supported amendment mutations into a separate output corpus."""
    if output_corpus_dir.resolve() == corpus_dir.resolve():
        raise ValueError("output_corpus_dir must be separate from corpus_dir")

    plan = plan_amendments(source_dir, corpus_dir)
    if plan.unresolved_count and not allow_partial:
        return plan

    if output_corpus_dir.exists():
        shutil.rmtree(output_corpus_dir)
    shutil.copytree(corpus_dir, output_corpus_dir)

    _, metadata = _load_notification_text(source_dir)
    for item in plan.items:
        if item.status != "ready":
            continue

        if item.operation == "SPLICE":
            source_path = Path(item.target_file or "")
            target_path = output_corpus_dir / source_path.relative_to(corpus_dir)
            tree = ET.parse(target_path)
            _apply_splice_to_tree(tree, item.canonical_target, item)
            write_xml(tree, target_path)
            item.status = "applied"
            item.output_file = str(target_path)
        elif item.operation == "INSERT_SIBLING":
            output_path = _apply_insert_sibling(item, output_corpus_dir, metadata)
            item.status = "applied"
            item.output_file = str(output_path)

    return plan
