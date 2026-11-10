"""Discovery and rendering helpers for project skills."""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

__all__ = ["Skill", "parse_frontmatter", "discover", "summary", "create"]


def _unquote(value: str) -> str:
    """Remove a matching pair of surrounding quotes from a value."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Parse simple front matter and return ``(metadata, body)``.

    Front matter starts on the first line and ends at the next ``---`` line.
    A ``>`` or ``|`` value consumes the indented lines that follow it and
    joins them into one space-separated value.
    """
    lines = text.splitlines(keepends=True)

    def line_text(index: int) -> str:
        return lines[index].rstrip("\r\n")

    if not lines or line_text(0) != "---":
        return {}, text

    end = None
    for index in range(1, len(lines)):
        if line_text(index) == "---":
            end = index
            break
    if end is None:
        return {}, text

    meta: dict[str, str] = {}
    index = 1
    while index < end:
        line = line_text(index)
        if ":" not in line:
            index += 1
            continue

        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            index += 1
            continue

        if value in (">", "|"):
            index += 1
            parts: list[str] = []
            while index < end:
                indented = line_text(index)
                if not indented or not indented[0].isspace():
                    break
                part = indented.strip()
                if part:
                    parts.append(part)
                index += 1
            value = " ".join(parts)
        else:
            value = _unquote(value)

        meta[key] = value
        index += 1

    return meta, "".join(lines[end + 1 :])


@dataclass
class Skill:
    """A skill loaded from a directory containing ``SKILL.md``."""

    name: str
    description: str
    path: Path

    def body(self) -> str:
        """Return the text of ``SKILL.md`` without its front matter."""
        text = (self.path / "SKILL.md").read_text(encoding="utf-8")
        return parse_frontmatter(text)[1]

    def files(self) -> list[str]:
        """Return up to 100 resource files in the skill directory."""
        found: list[str] = []

        def ignore_error(_error: OSError) -> None:
            return None

        for directory, dirnames, filenames in os.walk(self.path, onerror=ignore_error):
            dirnames[:] = sorted(
                name
                for name in dirnames
                if not name.startswith(".") and name != "__pycache__"
            )
            base = Path(directory)
            for filename in filenames:
                if filename == "SKILL.md" or filename.startswith("."):
                    continue
                relative = (base / filename).relative_to(self.path)
                if any(
                    part.startswith(".") or part == "__pycache__"
                    for part in relative.parts[:-1]
                ):
                    continue
                found.append(relative.as_posix())

        return sorted(found)[:100]

    def render(self) -> str:
        """Render the skill and the files available to an agent."""
        body = self.body()
        rendered = f"# Skill: {self.name}\nDirectory: {self.path}\n\n{body}"
        files = self.files()
        if files:
            rendered += "\n\n## Files in this skill\n"
            rendered += "\n".join(f"- {self.path}/{rel}" for rel in files)
        return rendered


def _fallback_description(body: str) -> str:
    """Return the first useful non-heading line in a skill body."""
    for line in body.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped[:200]
    return ""


def discover(dirs: Iterable[str | Path]) -> dict[str, Skill]:
    """Discover skills in ordered directories; later directories take priority."""
    skills: dict[str, Skill] = {}

    for directory in dirs:
        try:
            base = Path(directory).expanduser()
            if not base.is_dir():
                continue
            children = sorted(base.iterdir(), key=lambda path: path.name)
        except (OSError, RuntimeError):
            continue

        for child in children:
            try:
                if not child.is_dir() or not (child / "SKILL.md").is_file():
                    continue
                text = (child / "SKILL.md").read_text(encoding="utf-8")
                meta, body = parse_frontmatter(text)
            except (OSError, UnicodeError, RuntimeError):
                continue

            name = meta.get("name") or child.name
            description = meta.get("description") or _fallback_description(body)
            skills[name] = Skill(name=name, description=description, path=child)

    return skills


def summary(skills: Mapping[str, Skill], max_desc: int = 160) -> str:
    """Return one summary line per skill, sorted by skill name."""
    lines = [
        f"- {skill.name}: {skill.description[:max_desc]}"
        for skill in sorted(skills.values(), key=lambda skill: skill.name)
    ]
    return "\n".join(lines)


def create(root: str | Path, name: str, description: str) -> Path:
    """Create a starter skill and return its directory."""
    skill_dir = Path(root).expanduser() / name
    skill_file = skill_dir / "SKILL.md"
    if skill_file.exists():
        raise FileExistsError(str(skill_file))

    skill_dir.mkdir(parents=True, exist_ok=True)
    template = f"""---
name: {name}
description: {description}
---

# {name}

## When to use

Describe when this skill should be used.

## Instructions

Provide clear, step-by-step instructions for the agent.

## Examples

Show a short example of applying this skill.
"""
    skill_file.write_text(template, encoding="utf-8")
    return skill_dir
