"""CLI entry point for the devloop MVP."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

from devloop import __version__
from devloop import yaml_compat as yaml
from devloop.clipboard import get_clipboard_text, set_clipboard_text
from devloop.config import DevloopConfig, default_config_text, load_config
from devloop.detector import ClipboardKind, DetectionResult, detect_clipboard_content
from devloop.errors import DevloopError, PatchApplyError, SessionError
from devloop.git_tools import discover_repo_root
from devloop.parsers.sbt_compile import parse_sbt_compile_output
from devloop.parsers.sbt_test import parse_sbt_test_output
from devloop.patch_apply import apply_patch_payload
from devloop.prompt_builder import PromptSection, build_bootstrap_prompt, build_context_prompt
from devloop.protocol import ProtocolCommand, parse_protocol_response
from devloop.retrieval import QueryResult, RepositoryRetriever
from devloop.session import SessionState, SessionStore


def main(argv: list[str] | None = None) -> int:
    _configure_stdout()
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    if args.version:
        print(__version__)
        return 0
    if args.print_default_config:
        print(default_config_text())
        return 0
    if not args.config:
        parser.error("--config is required unless --print-default-config or --version is used")

    session_store: SessionStore | None = None
    session: SessionState | None = None

    try:
        config = load_config(Path(args.config))
        repo_root = discover_repo_root(config.project_root)
        session_store = SessionStore(repo_root, config.state_dir_mode)
        session, recovered_session = _load_session_for_run(
            session_store,
            force_bootstrap=args.force_bootstrap,
            reset_session=args.reset_session,
        )
        session.touch()

        if args.force_bootstrap:
            if recovered_session:
                print(
                    _human_text(
                        config.human_language,
                        f"Поврежденная session file была сброшена: {session_store.session_path}",
                        f"A broken session file was reset: {session_store.session_path}",
                    )
                )
            return _handle_first_run(config, repo_root, session_store, session, forced=True)

        if not session.initialized:
            return _handle_first_run(config, repo_root, session_store, session)

        clipboard_text = get_clipboard_text().strip()
        if not clipboard_text:
            raise DevloopError(
                _human_text(
                    config.human_language,
                    "Буфер обмена пуст. Скопируй ответ ChatGPT или лог и запусти команду снова.",
                    "Clipboard is empty. Copy a ChatGPT response or a log and run the command again.",
                )
            )

        detection, forced_mode = _resolve_detection(clipboard_text, args.force_mode)
        _print_mode_message(detection.kind, config.human_language, forced=forced_mode)

        retriever = RepositoryRetriever(repo_root, config)

        if detection.kind == ClipboardKind.LLM_RESPONSE:
            _handle_llm_response(clipboard_text, config, retriever, session_store, session)
        elif detection.kind == ClipboardKind.SBT_COMPILE:
            _handle_compile_log(clipboard_text, config, retriever, session_store, session)
        elif detection.kind == ClipboardKind.SBT_TEST:
            _handle_test_log(clipboard_text, config, retriever, session_store, session)
        else:
            _handle_raw_clipboard(clipboard_text, config, retriever, session_store, session)

        return 0
    except DevloopError as exc:
        print(_human_text(config.human_language if 'config' in locals() else "ru", f"Ошибка: {exc}", f"Error: {exc}"))
        return 1
    finally:
        if session_store and session:
            try:
                session.touch()
                session_store.save(session)
            except DevloopError:
                pass


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="devloop",
        description="Local human-in-the-loop development assistant for Scala/sbt projects.",
    )
    parser.add_argument("--config", help="Path to devloop YAML config file.")
    parser.add_argument(
        "--force-bootstrap",
        action="store_true",
        help="Generate only the bootstrap/protocol prompt and skip clipboard inspection.",
    )
    parser.add_argument(
        "--force-mode",
        choices=["auto", "llm", "compile", "test", "raw"],
        default="auto",
        help="Override clipboard auto-detection for troubleshooting.",
    )
    parser.add_argument(
        "--reset-session",
        action="store_true",
        help="Reset the local session metadata for the current repository before continuing.",
    )
    parser.add_argument("--print-default-config", action="store_true", help="Print default YAML config and exit.")
    parser.add_argument("--version", action="store_true", help="Print version and exit.")
    return parser


def _handle_first_run(
    config: DevloopConfig,
    repo_root: Path,
    session_store: SessionStore,
    session: SessionState,
    forced: bool = False,
) -> int:
    prompt = build_bootstrap_prompt(repo_root.name, config.human_language_name)
    set_clipboard_text(prompt)
    session.initialized = True
    session.followup_prompt_count = 0
    session.last_generated_prompt = prompt
    session.last_truncation_report = ""
    session_store.save(session)
    if forced:
        print(
            _human_text(
                config.human_language,
                "Режим bootstrap включен принудительно.",
                "Bootstrap mode was forced.",
            )
        )
    print(_human_text(config.human_language, "Первый запуск для этого репозитория.", "First run for this repository."))
    print(
        _human_text(
            config.human_language,
            "В буфер обмена помещен bootstrap prompt для ChatGPT.",
            "A bootstrap prompt for ChatGPT was copied to the clipboard.",
        )
    )
    _print_next_step(
        config.human_language,
        _human_text(
            config.human_language,
            "вставь этот prompt в ChatGPT вместе с исходным описанием задачи и затем скопируй полный ответ ChatGPT в буфер обмена.",
            "paste this prompt into ChatGPT together with the original task description, then copy the full ChatGPT reply to the clipboard.",
        ),
    )
    return 0


def _handle_llm_response(
    clipboard_text: str,
    config: DevloopConfig,
    retriever: RepositoryRetriever,
    session_store: SessionStore,
    session: SessionState,
) -> None:
    envelope = parse_protocol_response(clipboard_text)
    command = envelope.command
    session.last_parsed_llm_response = command.to_session_summary()
    session.last_known_task_summary = command.task_summary_en
    session.last_known_current_goal = command.current_goal_en
    session.add_history_entry(f"{command.command}: {command.current_goal_en}")

    if command.command == "COLLECT_CONTEXT":
        _handle_collect_context(command, config, retriever, session_store, session)
        return
    if command.command == "APPLY_PATCH":
        _handle_apply_patch(command, config, retriever, session_store, session)
        return
    if command.command == "ASK_HUMAN":
        print(command.summary_human)
        _print_next_step(config.human_language, command.next_step_human)
        return
    if command.command == "DONE":
        print(command.summary_human)
        _print_next_step(config.human_language, command.next_step_human)
        return
    raise DevloopError(f"Unknown protocol command: {command.command}")


def _handle_collect_context(
    command: ProtocolCommand,
    config: DevloopConfig,
    retriever: RepositoryRetriever,
    session_store: SessionStore,
    session: SessionState,
) -> None:
    query_results = retriever.execute_queries(command.payload["queries"])
    sections = _query_results_to_sections(query_results)
    prompt_goal = command.payload.get("prompt_goal")
    current_goal = str(prompt_goal) if isinstance(prompt_goal, str) and prompt_goal.strip() else command.current_goal_en
    prompt_result = _build_followup_prompt(
        session=session,
        task_summary=command.task_summary_en,
        current_goal=current_goal,
        source_label="LLM-requested local repository context",
        human_language_name=config.human_language_name,
        sections=sections,
        max_chars=config.max_prompt_chars,
    )
    set_clipboard_text(prompt_result.text)
    session.last_generated_prompt = prompt_result.text
    session.last_truncation_report = prompt_result.truncation_report
    session_store.save(session)
    print(command.summary_human)
    print(
        _human_text(
            config.human_language,
            "В буфер обмена помещен новый prompt для ChatGPT.",
            "A new prompt for ChatGPT was copied to the clipboard.",
        )
    )
    _print_next_step(config.human_language, command.next_step_human)


def _handle_apply_patch(
    command: ProtocolCommand,
    config: DevloopConfig,
    retriever: RepositoryRetriever,
    session_store: SessionStore,
    session: SessionState,
) -> None:
    try:
        result = apply_patch_payload(
            repo_root=Path(session.repo_root),
            state_dir=session_store.state_dir,
            payload=command.payload,
            allow_apply_on_dirty_files=config.allow_apply_on_dirty_files,
        )
    except PatchApplyError as exc:
        repair_sections = [
            PromptSection("Patch apply failure", str(exc), required=True),
            PromptSection(
                "Repair rules",
                (
                    "Return exactly one machine-readable command block.\n"
                    "Prefer APPLY_PATCH if you can correct the patch now.\n"
                    "Use patch_format=search_replace_v1.\n"
                    "Use payload.files with explicit per-file operations.\n"
                    "Allowed file operations are replace, create, and delete.\n"
                    "Use COLLECT_CONTEXT only if the current repository context is insufficient.\n"
                    "Use ASK_HUMAN only if a manual run or manual answer is required.\n"
                    "For replace operations, provide exact current text in each search block and set expected_matches explicitly.\n"
                    "For create operations, provide content and omit replacements.\n"
                    "For delete operations, omit replacements and content."
                ),
                required=True,
            ),
            PromptSection(
                "Rejected patch payload",
                yaml.safe_dump(command.payload, sort_keys=False, allow_unicode=True).strip(),
                compact_body=_compact_body(
                    yaml.safe_dump(command.payload, sort_keys=False, allow_unicode=True).strip(),
                    80,
                ),
            ),
        ]
        source_windows = _build_patch_repair_source_windows(retriever, command.payload)
        if source_windows:
            repair_sections.append(
                PromptSection(
                    "Current source windows",
                    source_windows,
                    compact_body=_compact_body(source_windows, 140),
                )
            )
        prompt_result = _build_followup_prompt(
            session=session,
            task_summary=command.task_summary_en,
            current_goal="Repair the rejected patch or request the smallest missing context needed to fix it.",
            source_label="Local patch validation failure",
            human_language_name=config.human_language_name,
            sections=repair_sections,
            max_chars=config.max_prompt_chars,
        )
        set_clipboard_text(prompt_result.text)
        session.last_generated_prompt = prompt_result.text
        session.last_truncation_report = prompt_result.truncation_report
        session.add_history_entry(f"PATCH_REPAIR: {exc}")
        session_store.save(session)
        print(_human_text(config.human_language, "Patch не применен автоматически.", "Patch was not applied automatically."))
        print(
            _human_text(
                config.human_language,
                "В буфер обмена помещен repair prompt для ChatGPT.",
                "A repair prompt for ChatGPT was copied to the clipboard.",
            )
        )
        _print_next_step(
            config.human_language,
            _human_text(
                config.human_language,
                "вставь repair prompt в ChatGPT и получи исправленный ответ с одной machine-readable командой.",
                "paste the repair prompt into ChatGPT and get a corrected reply with exactly one machine-readable command.",
            ),
        )
        return

    summary = ", ".join(result.affected_files)
    session.last_applied_patch_summary = summary
    session.add_history_entry(f"APPLY_PATCH: {summary}")
    print(_human_text(config.human_language, "Patch проверен и применен.", "Patch was validated and applied."))
    if result.git_status_summary:
        print(_human_text(config.human_language, "Измененные пути:", "Changed paths:"))
        print(result.git_status_summary)
    print(command.summary_human)
    _print_next_step(config.human_language, command.next_step_human)


def _handle_compile_log(
    clipboard_text: str,
    config: DevloopConfig,
    retriever: RepositoryRetriever,
    session_store: SessionStore,
    session: SessionState,
) -> None:
    parsed = parse_sbt_compile_output(clipboard_text, config.max_error_groups)
    query_results = retriever.build_compile_query_results(parsed)
    _maybe_add_project_tree_summary(query_results, retriever, config)
    prompt_result = _build_followup_prompt(
        session=session,
        task_summary=session.last_known_task_summary or "Diagnose the current Scala compile failure.",
        current_goal=session.last_known_current_goal or "Analyze the compile diagnostics and propose the smallest safe next step.",
        source_label="Clipboard sbt compile output",
        human_language_name=config.human_language_name,
        sections=_query_results_to_sections(query_results),
        max_chars=config.max_prompt_chars,
    )
    set_clipboard_text(prompt_result.text)
    session.last_generated_prompt = prompt_result.text
    session.last_truncation_report = prompt_result.truncation_report
    session_store.save(session)
    print(
        _human_text(
            config.human_language,
            f"Найден sbt compile log: ошибок {parsed.total_errors}, файлов {parsed.file_count}.",
            f"Detected sbt compile log: errors {parsed.total_errors}, files {parsed.file_count}.",
        )
    )
    print(
        _human_text(
            config.human_language,
            "В буфер обмена помещен новый prompt для ChatGPT.",
            "A new prompt for ChatGPT was copied to the clipboard.",
        )
    )
    _print_next_step(
        config.human_language,
        _human_text(
            config.human_language,
            "вставь этот prompt в ChatGPT, получи ответ, скопируй его в буфер и снова запусти ту же команду.",
            "paste this prompt into ChatGPT, get the reply, copy it to the clipboard, and run the same command again.",
        ),
    )


def _handle_test_log(
    clipboard_text: str,
    config: DevloopConfig,
    retriever: RepositoryRetriever,
    session_store: SessionStore,
    session: SessionState,
) -> None:
    parsed = parse_sbt_test_output(clipboard_text, config.max_test_failures)
    query_results = retriever.build_test_query_results(parsed)
    _maybe_add_project_tree_summary(query_results, retriever, config)
    prompt_result = _build_followup_prompt(
        session=session,
        task_summary=session.last_known_task_summary or "Diagnose the current Scala test failure.",
        current_goal=session.last_known_current_goal or "Analyze the failing tests and propose the smallest safe next step.",
        source_label="Clipboard sbt test output",
        human_language_name=config.human_language_name,
        sections=_query_results_to_sections(query_results),
        max_chars=config.max_prompt_chars,
    )
    set_clipboard_text(prompt_result.text)
    session.last_generated_prompt = prompt_result.text
    session.last_truncation_report = prompt_result.truncation_report
    session_store.save(session)
    print(
        _human_text(
            config.human_language,
            f"Найден sbt test log: падений {parsed.total_failures}.",
            f"Detected sbt test log: failures {parsed.total_failures}.",
        )
    )
    print(
        _human_text(
            config.human_language,
            "В буфер обмена помещен новый prompt для ChatGPT.",
            "A new prompt for ChatGPT was copied to the clipboard.",
        )
    )
    _print_next_step(
        config.human_language,
        _human_text(
            config.human_language,
            "вставь этот prompt в ChatGPT, получи ответ, скопируй его в буфер и снова запусти ту же команду.",
            "paste this prompt into ChatGPT, get the reply, copy it to the clipboard, and run the same command again.",
        ),
    )


def _handle_raw_clipboard(
    clipboard_text: str,
    config: DevloopConfig,
    retriever: RepositoryRetriever,
    session_store: SessionStore,
    session: SessionState,
) -> None:
    query_results = [retriever.build_raw_clipboard_query_result(clipboard_text)]
    _maybe_add_project_tree_summary(query_results, retriever, config)
    prompt_result = _build_followup_prompt(
        session=session,
        task_summary=session.last_known_task_summary or "Analyze the clipboard content and propose the next safe step.",
        current_goal=session.last_known_current_goal or "Use the raw clipboard content to determine the next useful action in the devloop workflow.",
        source_label="Raw clipboard text or log",
        human_language_name=config.human_language_name,
        sections=_query_results_to_sections(query_results),
        max_chars=config.max_prompt_chars,
    )
    set_clipboard_text(prompt_result.text)
    session.last_generated_prompt = prompt_result.text
    session.last_truncation_report = prompt_result.truncation_report
    session_store.save(session)
    print(
        _human_text(
            config.human_language,
            "Распознан обычный текст или лог без специального формата.",
            "Detected plain text or a log without a special format.",
        )
    )
    print(
        _human_text(
            config.human_language,
            "В буфер обмена помещен новый prompt для ChatGPT.",
            "A new prompt for ChatGPT was copied to the clipboard.",
        )
    )
    _print_next_step(
        config.human_language,
        _human_text(
            config.human_language,
            "вставь этот prompt в ChatGPT, получи ответ, скопируй его в буфер и снова запусти ту же команду.",
            "paste this prompt into ChatGPT, get the reply, copy it to the clipboard, and run the same command again.",
        ),
    )


def _query_results_to_sections(query_results: list[QueryResult]) -> list[PromptSection]:
    sections: list[PromptSection] = []
    for index, result in enumerate(query_results):
        compact = _compact_body(result.body, 24)
        sections.append(
            PromptSection(
                title=result.title,
                body=result.body,
                required=index == 0,
                compact_body=compact if compact != result.body else None,
            )
        )
    return sections


def _compact_body(body: str, max_lines: int) -> str:
    lines = body.splitlines()
    if len(lines) <= max_lines:
        return body
    kept = lines[:max_lines]
    kept.append(f"... omitted {len(lines) - max_lines} more lines")
    return "\n".join(kept)


def _build_followup_prompt(
    *,
    session: SessionState,
    task_summary: str,
    current_goal: str,
    source_label: str,
    human_language_name: str,
    sections: list[PromptSection],
    max_chars: int,
):
    prompt_result = build_context_prompt(
        task_summary=task_summary,
        current_goal=current_goal,
        source_label=source_label,
        human_language_name=human_language_name,
        sections=sections,
        max_chars=max_chars,
        include_protocol_reference=_should_include_full_protocol_reference(session),
    )
    session.note_followup_prompt_generated()
    return prompt_result


def _build_patch_repair_source_windows(retriever: RepositoryRetriever, payload: dict[str, object]) -> str:
    if str(payload.get("patch_format", "")) != "search_replace_v1":
        return ""
    files = payload.get("files")
    if not isinstance(files, list) or not files:
        return ""

    windows: list[str] = []
    for file_entry in files[: retriever.config.max_files]:
        if not isinstance(file_entry, dict):
            continue
        repo_path = str(file_entry.get("path", "")).replace("\\", "/").strip()
        if not repo_path:
            continue
        operation = str(file_entry.get("operation", "replace"))
        header_lines = [f"File: {repo_path}", f"Operation: {operation}"]
        try:
            resolved = retriever.resolve_repo_path(repo_path)
        except DevloopError:
            if operation == "create":
                content = file_entry.get("content")
                if isinstance(content, str):
                    header_lines.append("Current status: file does not exist in repository.")
                    header_lines.append("Requested content:")
                    header_lines.append(_compact_body(content, 40))
                    windows.append("\n".join(header_lines))
            continue

        try:
            file_text = retriever.read_text_file(resolved)
        except DevloopError:
            continue
        normalized_text = _normalize_newlines(file_text)

        if operation == "create":
            header_lines.append("Current status: file already exists.")
            header_lines.append("Current file excerpt:")
            header_lines.append(_compact_body(file_text, 40))
            windows.append("\n".join(header_lines))
            continue

        if operation == "delete":
            header_lines.append("Current file excerpt:")
            header_lines.append(_compact_body(file_text, 40))
            windows.append("\n".join(header_lines))
            continue

        replacements = file_entry.get("replacements")
        if not isinstance(replacements, list):
            continue
        for index, replacement in enumerate(replacements[:3], start=1):
            if not isinstance(replacement, dict):
                continue
            search_text = replacement.get("search")
            replace_text = replacement.get("replace")
            expected_matches = replacement.get("expected_matches", 1)
            if not isinstance(search_text, str) or not isinstance(replace_text, str):
                continue
            block_lines = list(header_lines)
            block_lines.append(f"Requested replacement: {index}")
            block_lines.append(f"expected_matches: {expected_matches}")
            block_lines.append("Search text:")
            block_lines.append(_compact_body(search_text, 24))
            block_lines.append("Replace text:")
            block_lines.append(_compact_body(replace_text, 24))
            match_line = _find_first_match_line(normalized_text, search_text)
            block_lines.append("Current file window:")
            if match_line is None:
                block_lines.append(_compact_body(file_text, 40))
            else:
                start_line = max(1, match_line - retriever.config.snippet_context_before)
                end_line = match_line + retriever.config.snippet_context_after
                try:
                    block_lines.append(retriever.read_snippet(resolved, start_line, end_line))
                except DevloopError:
                    block_lines.append(_compact_body(file_text, 40))
            windows.append("\n".join(block_lines))
    return "\n\n".join(windows)


def _find_first_match_line(file_text: str, search_text: str) -> int | None:
    normalized_search = _normalize_newlines(search_text)
    match_index = file_text.find(normalized_search)
    if match_index < 0:
        return None
    return file_text.count("\n", 0, match_index) + 1


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _maybe_add_project_tree_summary(
    query_results: list[QueryResult],
    retriever: RepositoryRetriever,
    config: DevloopConfig,
) -> None:
    if not config.include_project_summary_in_prompts:
        return
    query_results.append(QueryResult("project_tree", "Project tree summary", retriever.project_tree_summary()))


def _should_include_full_protocol_reference(session: SessionState) -> bool:
    return session.followup_prompt_count % 2 == 0


def _resolve_detection(text: str, force_mode: str) -> tuple[DetectionResult, bool]:
    if force_mode == "auto":
        return detect_clipboard_content(text), False
    forced_kinds = {
        "llm": ClipboardKind.LLM_RESPONSE,
        "compile": ClipboardKind.SBT_COMPILE,
        "test": ClipboardKind.SBT_TEST,
        "raw": ClipboardKind.RAW_TEXT,
    }
    return (
        DetectionResult(
            kind=forced_kinds[force_mode],
            score=100,
            reasons=[f"Forced mode override: {force_mode}"],
        ),
        True,
    )


def _load_session_for_run(
    session_store: SessionStore,
    *,
    force_bootstrap: bool,
    reset_session: bool,
) -> tuple[SessionState, bool]:
    if reset_session:
        return session_store.reset(), True
    if force_bootstrap:
        try:
            return session_store.load_or_create(), False
        except SessionError:
            return session_store.reset(), True
    return session_store.load_or_create(), False


def _print_mode_message(kind: ClipboardKind, human_language: str, forced: bool = False) -> None:
    messages = {
        ClipboardKind.LLM_RESPONSE: _human_text(
            human_language,
            "Распознан ответ LLM с machine-readable командой.",
            "Detected an LLM response with a machine-readable command.",
        ),
        ClipboardKind.SBT_COMPILE: _human_text(
            human_language,
            "Распознан sbt compile output.",
            "Detected sbt compile output.",
        ),
        ClipboardKind.SBT_TEST: _human_text(
            human_language,
            "Распознан sbt test output.",
            "Detected sbt test output.",
        ),
        ClipboardKind.RAW_TEXT: _human_text(
            human_language,
            "Распознан обычный текст из буфера обмена.",
            "Detected plain text from the clipboard.",
        ),
    }
    if forced:
        prefix = _human_text(human_language, "Принудительно выбран режим:", "Forced mode selected:")
        print(f"{prefix} {messages[kind]}")
        return
    print(messages[kind])


def _configure_stdout() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")


def _print_next_step(human_language: str, message: str) -> None:
    prefix = "Дальше" if human_language == "ru" else "Next"
    print(f"{prefix}: {message}")


def _human_text(human_language: str, ru_text: str, en_text: str) -> str:
    return ru_text if human_language == "ru" else en_text
