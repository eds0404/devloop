"""Prompt composition with logical-section truncation."""

from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from string import Template


@dataclass(slots=True)
class PromptSection:
    title: str
    body: str
    required: bool = False
    compact_body: str | None = None


@dataclass(slots=True)
class PromptBuildResult:
    text: str
    truncated: bool
    truncation_report: str
    included_titles: list[str]
    omitted_titles: list[str]
    shortened_titles: list[str]


def load_template_text(name: str) -> str:
    return resources.files("devloop.templates").joinpath(name).read_text(encoding="utf-8")


def build_bootstrap_prompt(repo_name: str, human_language_name: str) -> str:
    template = Template(load_template_text("protocol_prompt.txt"))
    return template.substitute(
        repo_name=repo_name,
        human_language_name=human_language_name,
        protocol_reference_section=render_section(
            "Full protocol reference",
            load_protocol_reference_text(human_language_name),
        ),
    )


def build_context_prompt(
    *,
    task_summary: str,
    current_goal: str,
    source_label: str,
    human_language_name: str,
    sections: list[PromptSection],
    max_chars: int,
    include_protocol_reference: bool = True,
) -> PromptBuildResult:
    template = Template(load_template_text("context_prompt.txt"))
    preamble = template.substitute(
        task_summary=task_summary or "No task summary has been established yet.",
        current_goal=current_goal or "Continue with the next smallest useful step.",
        source_label=source_label,
        human_language_name=human_language_name,
        protocol_reference_section=build_protocol_reference_section(
            human_language_name,
            include_protocol_reference,
        ),
        context_sections="${context_sections}",
    )
    preamble = preamble.replace("${context_sections}", "").rstrip()

    included_titles: list[str] = []
    omitted_titles: list[str] = []
    shortened_titles: list[str] = []
    rendered_sections: list[str] = [preamble]

    for section in sections:
        normal = render_section(section.title, section.body)
        compact = render_section(section.title, section.compact_body) if section.compact_body else None
        if _fits(rendered_sections + [normal], max_chars):
            rendered_sections.append(normal)
            included_titles.append(section.title)
            continue
        if compact and _fits(rendered_sections + [compact], max_chars):
            rendered_sections.append(compact)
            included_titles.append(section.title)
            shortened_titles.append(section.title)
            continue
        if section.required:
            fallback = compact or normal
            available = max(0, max_chars - _current_length(rendered_sections))
            rendered_sections.append(fallback[:available])
            included_titles.append(section.title)
            shortened_titles.append(section.title)
            continue
        omitted_titles.append(section.title)

    report = build_truncation_report(omitted_titles, shortened_titles)
    if report:
        report_section = render_section("Truncation report", report)
        while not _fits(rendered_sections + [report_section], max_chars):
            removed = _pop_last_optional_section(rendered_sections, included_titles, sections)
            if not removed:
                break
            omitted_titles.append(removed)
            report = build_truncation_report(omitted_titles, shortened_titles)
            report_section = render_section("Truncation report", report)
        if report and _fits(rendered_sections + [report_section], max_chars):
            rendered_sections.append(report_section)
        elif report:
            compact_report = render_section("Truncation report", "Context was truncated.")
            base_text = "\n\n".join(part for part in rendered_sections if part.strip())
            reserve = len(compact_report) + 2
            base_text = base_text[: max(0, max_chars - reserve)].rstrip()
            rendered_sections = [part for part in [base_text, compact_report] if part]

    final_text = "\n\n".join(part for part in rendered_sections if part.strip())
    return PromptBuildResult(
        text=final_text[:max_chars],
        truncated=bool(omitted_titles or shortened_titles),
        truncation_report=report,
        included_titles=included_titles,
        omitted_titles=omitted_titles,
        shortened_titles=shortened_titles,
    )


def render_section(title: str, body: str | None) -> str:
    text = (body or "").strip()
    if not text:
        return ""
    if not title:
        return text
    return f"## {title}\n{text}"


def load_protocol_rules_text(human_language_name: str) -> str:
    template = Template(load_template_text("protocol_rules.txt"))
    return template.substitute(human_language_name=human_language_name)


def load_protocol_reference_text(human_language_name: str) -> str:
    template = Template(load_template_text("protocol_reference.txt"))
    return template.substitute(human_language_name=human_language_name)


def build_protocol_reference_section(human_language_name: str, include_full_reference: bool) -> str:
    if include_full_reference:
        return render_section("Full protocol reference", load_protocol_reference_text(human_language_name))
    return render_section(
        "Protocol reminder",
        (
            "The full protocol reference is intentionally omitted in this prompt to save space. "
            "Return exactly one DEVLOOP_COMMAND_V2 block between the standard markers. "
            "Do not use YAML. Do not add prose outside the block. "
            "Use only COLLECT_CONTEXT, APPLY_PATCH, ASK_HUMAN, or DONE. "
            "For APPLY_PATCH, use PATCH_FORMAT: SEARCH_REPLACE_BLOCKS_V1 and the same file section markers as in the previous prompts."
        ),
    )


def build_truncation_report(omitted_titles: list[str], shortened_titles: list[str]) -> str:
    lines: list[str] = []
    if shortened_titles:
        lines.append(f"Shortened sections: {', '.join(shortened_titles)}")
    if omitted_titles:
        lines.append(f"Omitted sections: {', '.join(omitted_titles)}")
    return "\n".join(lines)


def _fits(parts: list[str], max_chars: int) -> bool:
    return _current_length(parts) <= max_chars


def _current_length(parts: list[str]) -> int:
    return len("\n\n".join(part for part in parts if part.strip()))


def _pop_last_optional_section(
    rendered_sections: list[str],
    included_titles: list[str],
    defined_sections: list[PromptSection],
) -> str | None:
    defined_by_title = {section.title: section for section in defined_sections}
    for index in range(len(included_titles) - 1, -1, -1):
        title = included_titles[index]
        section = defined_by_title.get(title)
        if section and section.required:
            continue
        included_titles.pop(index)
        rendered_sections.pop(index + 1)
        return title
    return None
