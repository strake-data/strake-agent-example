"""Module for loading and managing agent skills from markdown files."""

import re
from dataclasses import dataclass
from pathlib import Path

from .config import logger


@dataclass
class Skill:
    """Represents an agent skill loaded from a file.

    Attributes:
        name: Name of the skill.
        description: A brief description of the skill's purpose.
        body: The full content/instructions for the skill.
    """

    name: str
    description: str
    body: str


class SkillLoader:
    """Loads and provides access to agent skills from a directory."""

    def __init__(self, skills_dir: Path):
        """Initialize the SkillLoader.

        Args:
            skills_dir: Path to the directory containing skill definitions.
        """
        self.skills: dict[str, Skill] = {}
        if not skills_dir.exists():
            logger.warning("Skills directory does not exist: %s", skills_dir)
            return

        for skill_file in sorted(skills_dir.rglob("SKILL.md")):
            try:
                text = skill_file.read_text()
                meta, body = self._parse_frontmatter(text)
                name = meta.get("name", skill_file.parent.name)
                self.skills[name] = Skill(
                    name=name,
                    description=meta.get("description", ""),
                    body=body.strip(),
                )
            except Exception as e:
                logger.error("Failed to load skill from %s: %s", skill_file, e)

    def _parse_frontmatter(self, text: str) -> tuple[dict, str]:
        """Parse frontmatter from a markdown string.

        Args:
            text: The markdown content.

        Returns:
            A tuple containing a dictionary of metadata and the remaining body string.
        """
        match = re.match(r"^---\n(.*?)\n---\n(.*)", text, re.DOTALL)
        if not match:
            logger.warning("Malformed skill file: missing frontmatter.")
            return {}, text

        meta_str, body = match.group(1), match.group(2)
        meta = {}
        for line in meta_str.splitlines():
            if ": " in line:
                k, v = line.split(": ", 1)
                meta[k.strip()] = v.strip()
        return meta, body

    def listing(self) -> str:
        """Generate a listing of available skills for the agent's instructions.

        Returns:
            A string listing available skills.
        """
        if not self.skills:
            return "No skills available."
        lines = ["Available skills (call load_skill(name) to use):"]
        for name, skill in self.skills.items():
            lines.append(f"  - {name}: {skill.description}")
        return "\n".join(lines)

    def load(self, name: str) -> str:
        """Retrieve the full content of a named skill.

        Args:
            name: The name of the skill to load.

        Returns:
            The formatted skill content or an error message.
        """
        skill = self.skills.get(name)
        if not skill:
            available = ", ".join(self.skills.keys()) or "none"
            logger.warning("Requested unknown skill: %s", name)
            return f"Unknown skill '{name}'. Available: {available}"
        return f'<skill name="{name}">\n{skill.body}\n</skill>'


# Global instance
SKILL_LOADER = SkillLoader(Path("skills"))
