"""Configuration loading and validation."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from devloop.errors import ConfigError
from devloop import yaml_compat as yaml

DEFAULT_INCLUDE_GLOBS = ["**/*"]
DEFAULT_EXCLUDE_GLOBS = [
    ".git/**",
    "target/**",
    ".bloop/**",
    ".metals/**",
    ".idea/**",
    "project/target/**",
]
LANGUAGE_ALIASES = {
    "en": "en",
    "english": "en",
    "ru": "ru",
    "russian": "ru",
}
LANGUAGE_NAMES = {
    "en": "English",
    "ru": "Russian",
}


@dataclass(slots=True)
class DevloopConfig:
    project_root: Path
    max_prompt_chars: int = 120000
    max_files: int = 12
    max_snippet_lines: int = 180
    max_search_results: int = 20
    max_error_groups: int = 20
    max_test_failures: int = 10
    include_globs: list[str] = field(default_factory=lambda: list(DEFAULT_INCLUDE_GLOBS))
    exclude_globs: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDE_GLOBS))
    snippet_context_before: int = 25
    snippet_context_after: int = 40
    project_packages: list[str] = field(default_factory=list)
    include_project_summary_in_prompts: bool = False
    allow_apply_on_dirty_files: bool = False
    state_dir_mode: str = "localappdata"
    prompt_language: str = "en"
    human_language: str = "ru"

    def __post_init__(self) -> None:
        self.project_root = self.project_root.expanduser()
        self.prompt_language = normalize_language_code(self.prompt_language, "prompt_language")
        self.human_language = normalize_language_code(self.human_language, "human_language")
        if not self.project_root.exists():
            raise ConfigError(f"Configured project_root does not exist: {self.project_root}")
        if not self.project_root.is_dir():
            raise ConfigError(f"Configured project_root is not a directory: {self.project_root}")
        if self.max_prompt_chars <= 0:
            raise ConfigError("max_prompt_chars must be positive")
        if self.max_files <= 0:
            raise ConfigError("max_files must be positive")
        if self.max_snippet_lines <= 0:
            raise ConfigError("max_snippet_lines must be positive")
        if self.max_search_results <= 0:
            raise ConfigError("max_search_results must be positive")
        if self.max_error_groups <= 0:
            raise ConfigError("max_error_groups must be positive")
        if self.max_test_failures <= 0:
            raise ConfigError("max_test_failures must be positive")
        if self.snippet_context_before < 0 or self.snippet_context_after < 0:
            raise ConfigError("snippet context values must be non-negative")
        if self.state_dir_mode not in {"localappdata"}:
            raise ConfigError("Only state_dir_mode=localappdata is supported in the MVP")
        if self.prompt_language != "en":
            raise ConfigError("prompt_language must be en in the MVP")
        if self.human_language not in {"ru", "en"}:
            raise ConfigError("human_language must be ru or en in the MVP")

    def to_serializable_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["project_root"] = str(self.project_root)
        return data

    @property
    def human_language_name(self) -> str:
        return LANGUAGE_NAMES[self.human_language]


def load_config(path: Path) -> DevloopConfig:
    """Load and validate a YAML config file."""
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Failed to parse YAML config: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("Config file must contain a mapping at the top level")

    data = dict(raw)
    if "project_root" not in data:
        raise ConfigError("Config file must define project_root")

    return DevloopConfig(
        project_root=Path(data["project_root"]),
        max_prompt_chars=int(data.get("max_prompt_chars", 120000)),
        max_files=int(data.get("max_files", 12)),
        max_snippet_lines=int(data.get("max_snippet_lines", 180)),
        max_search_results=int(data.get("max_search_results", 20)),
        max_error_groups=int(data.get("max_error_groups", 20)),
        max_test_failures=int(data.get("max_test_failures", 10)),
        include_globs=_read_string_list(data.get("include_globs"), DEFAULT_INCLUDE_GLOBS),
        exclude_globs=_read_string_list(data.get("exclude_globs"), DEFAULT_EXCLUDE_GLOBS),
        snippet_context_before=int(data.get("snippet_context_before", 25)),
        snippet_context_after=int(data.get("snippet_context_after", 40)),
        project_packages=_read_string_list(data.get("project_packages"), []),
        include_project_summary_in_prompts=bool(data.get("include_project_summary_in_prompts", False)),
        allow_apply_on_dirty_files=bool(data.get("allow_apply_on_dirty_files", False)),
        state_dir_mode=str(data.get("state_dir_mode", "localappdata")),
        prompt_language=str(data.get("prompt_language", "en")),
        human_language=str(data.get("human_language", "ru")),
    )


def default_config_text() -> str:
    """Return a ready-to-edit default YAML config."""
    example = {
        "project_root": r"C:\path\to\scala-project",
        "max_prompt_chars": 120000,
        "max_files": 12,
        "max_snippet_lines": 180,
        "max_search_results": 20,
        "max_error_groups": 20,
        "max_test_failures": 10,
        "include_globs": list(DEFAULT_INCLUDE_GLOBS),
        "exclude_globs": list(DEFAULT_EXCLUDE_GLOBS),
        "snippet_context_before": 25,
        "snippet_context_after": 40,
        "project_packages": [],
        "include_project_summary_in_prompts": False,
        "allow_apply_on_dirty_files": False,
        "state_dir_mode": "localappdata",
        "prompt_language": "en",
        "human_language": "ru",
    }
    return yaml.safe_dump(example, sort_keys=False, allow_unicode=True)


def _read_string_list(value: Any, default: list[str]) -> list[str]:
    if value is None:
        return list(default)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError("Expected a list of strings in config")
    return list(value)


def normalize_language_code(value: str, field_name: str) -> str:
    normalized = str(value).strip().lower()
    if normalized not in LANGUAGE_ALIASES:
        raise ConfigError(
            f"{field_name} must use a supported language code or alias: en, ru, English, Russian"
        )
    return LANGUAGE_ALIASES[normalized]
