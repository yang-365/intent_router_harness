"""Filesystem-based skill registry with progressive body loading.

The registry scans skill directories at startup, parsing ONLY frontmatter
metadata (name, description, intent_codes, required_slots, reference manifest).
Skill bodies and reference bodies are loaded on-demand when the middleware
decides they are needed for the current task.

All business logic lives in skill files — the registry is business-agnostic.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SkillReferenceMeta:
    """Metadata for one reference file exposed by a skill."""

    id: str
    relative_path: str
    purpose: str = ""


@dataclass(frozen=True, slots=True)
class SkillMeta:
    """Metadata parsed from SKILL.md frontmatter — always in memory."""

    name: str
    description: str
    skill_dir: Path
    intent_codes: tuple[str, ...] = ()
    required_slots: tuple[str, ...] = ()
    references: tuple[SkillReferenceMeta, ...] = ()

    @property
    def skill_path(self) -> Path:
        return self.skill_dir / "SKILL.md"


@dataclass
class LoadedSkillContent:
    """Lazily loaded content for a single skill session context."""

    skill_body: str | None = None
    reference_bodies: dict[str, str] = field(default_factory=dict)


class SkillRegistry:
    """Business-agnostic skill index with progressive loading.

    Startup: scan skill roots → parse frontmatter only → build intent index.
    Runtime: load skill body / reference body on demand per session context.
    """

    def __init__(self) -> None:
        self._skills: dict[str, SkillMeta] = {}
        self._intent_index: dict[str, str] = {}

    def __len__(self) -> int:
        return len(self._skills)

    @classmethod
    def from_roots(cls, roots: list[str | Path]) -> SkillRegistry:
        """Scan skill directories, parsing only frontmatter metadata.

        Args:
            roots: List of directories to scan for skill subdirectories.

        Returns:
            Populated SkillRegistry with metadata index.
        """
        registry = cls()
        for root in roots:
            root_path = Path(root).expanduser()
            if not root_path.is_dir():
                logger.warning("skipping missing skill root path=%s", root_path)
                continue
            for skill_dir in sorted(
                item for item in root_path.iterdir() if item.is_dir()
            ):
                skill_path = skill_dir / "SKILL.md"
                if not skill_path.is_file():
                    continue
                meta = _parse_skill_metadata(skill_path)
                if meta is None:
                    continue
                _validate_skill_meta(meta)
                registry._skills[meta.name] = meta
                for intent_code in meta.intent_codes:
                    if intent_code in registry._intent_index:
                        existing = registry._intent_index[intent_code]
                        if existing != meta.name:
                            raise ValueError(
                                f"intent_code {intent_code!r} claimed by both "
                                f"{existing!r} and {meta.name!r}"
                            )
                    registry._intent_index[intent_code] = meta.name
                logger.info(
                    "registered skill name=%s intents=%s slots=%s refs=%d",
                    meta.name,
                    list(meta.intent_codes),
                    list(meta.required_slots),
                    len(meta.references),
                )
        logger.info(
            "skill registry ready count=%d skills=%s",
            len(registry),
            sorted(registry._skills),
        )
        return registry

    def names(self) -> list[str]:
        """Return registered skill names in sorted order."""
        return sorted(self._skills)

    def get_meta(self, name: str) -> SkillMeta | None:
        """Return metadata for a skill by name."""
        return self._skills.get(name)

    def find_by_intent(self, intent_code: str) -> SkillMeta | None:
        """Resolve intent_code → skill metadata."""
        name = self._intent_index.get(intent_code)
        if name is None:
            return None
        return self._skills.get(name)

    def all_metadata_summary(self) -> str:
        """Build a concise summary of ALL skills for intent recognition.

        This summary is always injected into the system prompt so the
        agent can match user intent to a skill — without loading bodies.
        """
        if not self._skills:
            return ""
        lines = ["## Available Skills (Intent Recognition)"]
        for name in sorted(self._skills):
            meta = self._skills[name]
            lines.append(
                f"- **{meta.name}**: {meta.description}\n"
                f"  intent_codes: {list(meta.intent_codes)}\n"
                f"  required_slots: {list(meta.required_slots)}"
            )
        return "\n".join(lines)

    def load_skill_body(self, name: str) -> str | None:
        """Load the full SKILL.md body (intent boundary logic).

        Args:
            name: Skill name to load.

        Returns:
            Markdown body (without frontmatter) or None if not found.
        """
        meta = self._skills.get(name)
        if meta is None:
            return None
        skill_path = meta.skill_path
        if not skill_path.is_file():
            logger.warning("skill file missing name=%s path=%s", name, skill_path)
            return None
        content = skill_path.read_text(encoding="utf-8")
        _, body = _split_frontmatter(content)
        return body.strip()

    def load_reference(self, skill_name: str, ref_id: str) -> str | None:
        """Load a single reference body by skill name and reference ID.

        Args:
            skill_name: Skill name.
            ref_id: Reference ID declared in frontmatter.

        Returns:
            Reference markdown body or None if not found.
        """
        meta = self._skills.get(skill_name)
        if meta is None:
            return None
        ref_meta = next((r for r in meta.references if r.id == ref_id), None)
        if ref_meta is None:
            return None
        ref_path = (meta.skill_dir / ref_meta.relative_path).resolve()
        if not ref_path.is_file():
            logger.warning(
                "reference file missing skill=%s ref=%s path=%s",
                skill_name,
                ref_id,
                ref_path,
            )
            return None
        content = ref_path.read_text(encoding="utf-8")
        _, body = _split_frontmatter(content)
        return body.strip()

    def virtual_file_listing(self, skill_name: str) -> dict[str, str]:
        """Build the virtual file paths for a loaded skill.

        Args:
            skill_name: Skill to build paths for.

        Returns:
            Mapping of virtual path → purpose description.
        """
        meta = self._skills.get(skill_name)
        if meta is None:
            return {}
        listing: dict[str, str] = {}
        dir_name = meta.skill_dir.name
        listing[f"/skills/{dir_name}/SKILL.md"] = meta.description
        for ref in meta.references:
            listing[f"/skills/{dir_name}/{ref.relative_path}"] = ref.purpose
        return listing


# ---------------------------------------------------------------------------
# Internal parsing helpers — business-agnostic
# ---------------------------------------------------------------------------


def _parse_skill_metadata(skill_path: Path) -> SkillMeta | None:
    """Parse only frontmatter from a SKILL.md file."""
    try:
        content = skill_path.read_text(encoding="utf-8")
    except OSError:
        logger.warning("cannot read skill file path=%s", skill_path)
        return None
    metadata, _ = _split_frontmatter(content)
    name = str(metadata.get("name") or skill_path.parent.name).strip()
    description = str(metadata.get("description") or "").strip()
    if not description:
        logger.warning("skill missing description name=%s path=%s", name, skill_path)
    references = _parse_reference_manifest(metadata.get("references"), skill_path)
    return SkillMeta(
        name=name,
        description=description,
        skill_dir=skill_path.parent.resolve(),
        intent_codes=tuple(_string_list(metadata.get("intent_codes"))),
        required_slots=tuple(_string_list(metadata.get("required_slots"))),
        references=tuple(references),
    )


def _validate_skill_meta(meta: SkillMeta) -> None:
    """Enforce one intent per skill."""
    if len(meta.intent_codes) > 1:
        raise ValueError(
            f"skill {meta.name!r} declares multiple intent_codes "
            f"{list(meta.intent_codes)}; one skill must map to one intent"
        )


def _parse_reference_manifest(
    raw: Any, skill_path: Path
) -> list[SkillReferenceMeta]:
    """Parse reference declarations from frontmatter."""
    if raw is None or raw == "":
        return []
    if not isinstance(raw, list):
        logger.warning(
            "ignoring malformed references in skill path=%s", skill_path
        )
        return []
    result: list[SkillReferenceMeta] = []
    seen: set[str] = set()
    for index, item in enumerate(raw, start=1):
        if isinstance(item, dict):
            ref_id = str(item.get("id") or "").strip()
            rel_path = str(item.get("path") or "").strip()
            purpose = str(item.get("purpose") or "").strip()
        elif isinstance(item, str):
            ref_id = f"ref_{index:03d}"
            rel_path = item.strip()
            purpose = ""
        else:
            continue
        if not ref_id or not rel_path:
            continue
        if ref_id in seen:
            logger.warning(
                "duplicate reference id=%s in skill path=%s", ref_id, skill_path
            )
            continue
        seen.add(ref_id)
        abs_path = (skill_path.parent / rel_path).resolve()
        if not abs_path.is_relative_to(skill_path.parent.resolve()):
            logger.warning(
                "reference escapes skill directory ref=%s path=%s", ref_id, abs_path
            )
            continue
        result.append(SkillReferenceMeta(id=ref_id, relative_path=rel_path, purpose=purpose))
    return result


def _split_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """Split YAML-like frontmatter from markdown content."""
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, content
    end_index: int | None = None
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end_index = index
            break
    if end_index is None:
        return {}, content
    metadata = _parse_simple_frontmatter(lines[1:end_index])
    body = "\n".join(lines[end_index + 1 :])
    return metadata, body


def _parse_simple_frontmatter(lines: list[str]) -> dict[str, Any]:
    """Parse simple key: value frontmatter without YAML library."""
    parsed: dict[str, Any] = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, raw_value = line.split(":", 1)
        parsed[key.strip()] = _parse_scalar_or_list(raw_value.strip())
    return parsed


def _parse_scalar_or_list(value: str) -> Any:
    """Parse a frontmatter value as scalar, list, or boolean."""
    if not value:
        return ""
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    if value.startswith("[") or value.startswith('"') or value.startswith("'"):
        try:
            decoded = json.loads(value.replace("'", '"'))
        except json.JSONDecodeError:
            return value.strip("'\"")
        return decoded
    if "," in value:
        return [item.strip() for item in value.split(",") if item.strip()]
    return value.strip("'\"")


def _string_list(value: Any) -> list[str]:
    """Coerce a value to a list of non-empty strings."""
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()]
