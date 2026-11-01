"""Load versioned prompt files with an explicit substitution contract."""

from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
from string import Template
from typing import Mapping

PROMPTS = Path(__file__).parent / "prompts"


@dataclass(frozen=True)
class Prompt:
    name: str
    version: str
    sha256: str
    variables: frozenset[str]
    body: str

    def render(self, **values: str) -> str:
        if values.keys() != self.variables:
            missing = sorted(self.variables - values.keys())
            extra = sorted(values.keys() - self.variables)
            raise ValueError(f"Prompt {self.name}/{self.version}: missing variables {missing}; unexpected variables {extra}")
        return Template(self.body).substitute(values)

    def metadata(self) -> Mapping[str, str]:
        return {"name": self.name, "version": self.version, "sha256": self.sha256}


def load_prompt(name: str, version: str = "v1", *, root: Path = PROMPTS) -> Prompt:
    if not re.fullmatch(r"[a-z][a-z0-9_-]*", name) or not re.fullmatch(r"v[1-9][0-9]*", version):
        raise ValueError("Invalid prompt name or version")
    path = root / name / f"{version}.md"
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"Prompt file does not exist: {path}") from exc
    text = raw.decode("utf-8")
    header, separator, body = text.partition("\n\n")
    lines = header.splitlines()
    if not separator or len(lines) != 3 or lines[:2] != [f"Name: {name}", f"Version: {version}"] or not lines[2].startswith("Variables: "):
        raise ValueError(f"Invalid prompt header or version: {path}")
    names = lines[2].removeprefix("Variables: ").split(", ")
    template = Template(body)
    if (not body.strip() or not template.is_valid() or len(set(names)) != len(names)
            or any(not re.fullmatch(r"[a-z][a-z0-9_]*", value) for value in names)
            or set(template.get_identifiers()) != set(names)):
        raise ValueError(f"Invalid prompt variables or body: {path}")
    return Prompt(name, version, hashlib.sha256(raw).hexdigest(), frozenset(names), body)
