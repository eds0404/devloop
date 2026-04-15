"""CLI entry point for the devloop MVP."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
from pathlib import Path
import re
import sys

from devloop import __version__
from devloop.clipboard import get_clipboard_text as _system_get_clipboard_text
from devloop.clipboard import set_clipboard_text as _system_set_clipboard_text
from devloop.config import DevloopConfig, default_config_text, load_config
from devloop.detector import ClipboardKind, DetectionResult, detect_clipboard_content
from devloop.errors import DevloopError, PatchApplyError, PatchInfrastructureError, ProtocolError, SessionError
from devloop.git_tools import discover_repo_root, get_head_commit, get_paths_diff, summarize_paths_status
from devloop.parsers.sbt_compile import parse_sbt_compile_output
from devloop.parsers.sbt_test import parse_sbt_test_output
from devloop.patch_apply import apply_patch_payload
from devloop.prompt_builder import PromptSection, build_bootstrap_prompt, build_context_prompt
from devloop.protocol import ProtocolCommand, extract_command_block, parse_protocol_response
from devloop.retrieval import QueryResult, RepositoryRetriever
from devloop.runlog import RunLogRecorder
from devloop.session import CURRENT_PROTOCOL_REVISION, SessionState, SessionStore

_ACTIVE_RUN_LOG: RunLogRecorder | None = None


def main(argv: list[str] | None = None) -> int:
    _configure_stdout()
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    run_log = _create_run_log_recorder(args, argv)
    exit_code = 1

    with _stdout_tee_context(run_log):
        session_store: SessionStore | None = None
        session: SessionState | None = None

        try:
            if args.version:
                print(__version__)
                exit_code = 0
                return exit_code
            if args.print_default_config:
                print(default_config_text())
                exit_code = 0
                return exit_code
            if not args.config:
                parser.error("--config is required unless --print-default-config or --version is used")

            config = load_config(Path(args.config))
            repo_root = discover_repo_root(config.project_root)
            session_store = SessionStore(repo_root, config.state_dir_mode)
            session, recovered_session = _load_session_for_run(
                session_store,
                force_bootstrap=args.force_bootstrap,
                reset_session=args.reset_session,
            )
            protocol_reset = _refresh_session_protocol_revision(session)
            session.touch()

            if args.force_bootstrap:
                if recovered_session:
                    print(
                        _human_text(
                            config.human_language,
                            f"ÐŸÐ¾Ð²Ñ€ÐµÐ¶Ð´ÐµÐ½Ð½Ð°Ñ session file Ð±Ñ‹Ð»Ð° ÑÐ±Ñ€Ð¾ÑˆÐµÐ½Ð°: {session_store.session_path}",
                            f"A broken session file was reset: {session_store.session_path}",
                        )
                    )
                exit_code = _handle_first_run(config, repo_root, session_store, session, forced=True)
                return exit_code

            if protocol_reset:
                print(
                    _human_text(
                        config.human_language,
                        "Ð›Ð¾ÐºÐ°Ð»ÑŒÐ½Ð°Ñ ÑÐµÑÑÐ¸Ñ Ð¿ÐµÑ€ÐµÐ²ÐµÐ´ÐµÐ½Ð° Ð½Ð° Ð½Ð¾Ð²ÑƒÑŽ Ñ€ÐµÐ²Ð¸Ð·Ð¸ÑŽ Ð¿Ñ€Ð¾Ñ‚Ð¾ÐºÐ¾Ð»Ð°. ÐÑƒÐ¶ÐµÐ½ Ð½Ð¾Ð²Ñ‹Ð¹ bootstrap prompt.",
                        "The local session was upgraded to a new protocol revision. A fresh bootstrap prompt is required.",
                    )
                )
                exit_code = _handle_first_run(config, repo_root, session_store, session, forced=True)
                return exit_code

            if not session.initialized:
                exit_code = _handle_first_run(config, repo_root, session_store, session)
                return exit_code

            clipboard_text_raw = get_clipboard_text()
            clipboard_text = clipboard_text_raw.strip()
            if not clipboard_text:
                raise DevloopError(
                    _human_text(
                        config.human_language,
                        "Ð‘ÑƒÑ„ÐµÑ€ Ð¾Ð±Ð¼ÐµÐ½Ð° Ð¿ÑƒÑÑ‚. Ð¡ÐºÐ¾Ð¿Ð¸Ñ€ÑƒÐ¹ Ð¾Ñ‚Ð²ÐµÑ‚ ChatGPT Ð¸Ð»Ð¸ Ð»Ð¾Ð³ Ð¸ Ð·Ð°Ð¿ÑƒÑÑ‚Ð¸ ÐºÐ¾Ð¼Ð°Ð½Ð´Ñƒ ÑÐ½Ð¾Ð²Ð°.",
                        "Clipboard is empty. Copy a ChatGPT response or a log and run the command again.",
                    )
                )

            detection, forced_mode = _resolve_detection(clipboard_text, args.force_mode, session)
            _print_mode_message(detection.kind, config.human_language, forced=forced_mode)
            _record_detection(detection, forced_mode)

            retriever = RepositoryRetriever(repo_root, config)

            if detection.kind == ClipboardKind.LLM_RESPONSE:
                _handle_llm_response(clipboard_text, config, retriever, session_store, session)
            elif detection.kind == ClipboardKind.SBT_COMPILE:
                _handle_compile_log(clipboard_text, config, retriever, session_store, session)
            elif detection.kind == ClipboardKind.SBT_TEST:
                _handle_test_log(clipboard_text, config, retriever, session_store, session)
            else:
                _handle_raw_clipboard(clipboard_text, config, retriever, session_store, session)

            exit_code = 0
            return exit_code
        except DevloopError as exc:
            print(_human_text(config.human_language if 'config' in locals() else "ru", f"ÐžÑˆÐ¸Ð±ÐºÐ°: {exc}", f"Error: {exc}"))
            exit_code = 1
            return exit_code
        finally:
            if session_store and session:
                try:
                    session.touch()
                    session_store.save(session)
                except DevloopError:
                    pass
            if run_log:
                run_log.finalize(exit_code)


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


def _create_run_log_recorder(args: argparse.Namespace, argv: list[str] | None) -> RunLogRecorder | None:
    config_value = getattr(args, "config", None)
    if not config_value:
        return None
    cli_args = list(argv) if argv is not None else sys.argv[1:]
    return RunLogRecorder(Path(config_value), cli_args)


@contextlib.contextmanager
def _stdout_tee_context(run_log: RunLogRecorder | None):
    global _ACTIVE_RUN_LOG
    previous_run_log = _ACTIVE_RUN_LOG
    _ACTIVE_RUN_LOG = run_log
    if run_log is None:
        try:
            yield
        finally:
            _ACTIVE_RUN_LOG = previous_run_log
        return

    tee = _ConsoleTee(sys.stdout, run_log)
    with contextlib.redirect_stdout(tee):
        try:
            yield
        finally:
            _ACTIVE_RUN_LOG = previous_run_log


def get_clipboard_text() -> str:
    text = _system_get_clipboard_text()
    if _ACTIVE_RUN_LOG is not None:
        _ACTIVE_RUN_LOG.record_clipboard_before(text)
    return text


def set_clipboard_text(text: str) -> None:
    _system_set_clipboard_text(text)
    if _ACTIVE_RUN_LOG is not None:
        _ACTIVE_RUN_LOG.record_clipboard_after(text)


def _record_detection(detection: DetectionResult, forced: bool) -> None:
    if _ACTIVE_RUN_LOG is None:
        return
    lines = [
        f"Clipboard kind: {detection.kind.value}",
        f"Forced mode: {'yes' if forced else 'no'}",
        f"Score: {detection.score}",
        "Detection reasons:",
    ]
    if detection.reasons:
        lines.extend(f"  - {reason}" for reason in detection.reasons)
    else:
        lines.append("  - <none>")
    _ACTIVE_RUN_LOG.add_section("CLIPBOARD CLASSIFICATION", "\n".join(lines))


def _record_llm_command_context(envelope, session: SessionState) -> None:
    if _ACTIVE_RUN_LOG is None:
        return
    command = envelope.command
    _ACTIVE_RUN_LOG.add_section("LLM COMMAND BLOCK", envelope.raw_block)
    lines = [
        f"Session id: {session.session_id}",
        f"Protocol parse mode: {envelope.parse_mode}",
        f"Command: {command.command}",
        f"Task summary (EN): {command.task_summary_en}",
        f"Current goal (EN): {command.current_goal_en}",
        f"Summary for human: {command.summary_human}",
        f"Next step for human: {command.next_step_human}",
        f"Payload keys: {', '.join(sorted(command.payload.keys())) or '<none>'}",
    ]
    _ACTIVE_RUN_LOG.add_section("LLM COMMAND SUMMARY", "\n".join(lines))


def _record_llm_protocol_failure(clipboard_text: str, session: SessionState | None, exc: ProtocolError) -> None:
    if _ACTIVE_RUN_LOG is None:
        return
    raw_block = ""
    try:
        raw_block = extract_command_block(clipboard_text)
    except ProtocolError:
        pass
    if raw_block:
        _ACTIVE_RUN_LOG.add_section("LLM COMMAND BLOCK", raw_block)
    lines = [
        f"Session id: {session.session_id if session is not None else '<unknown>'}",
        "Protocol parse mode: failed_before_command",
        "Patch decision: rejected",
        "Failure stage: protocol_parse",
        f"Failure reason: {exc}",
    ]
    if raw_block and "APPLY_PATCH" in raw_block.upper():
        _ACTIVE_RUN_LOG.add_section("PATCH VALIDATION", "\n".join(lines))


def _record_patch_attempt_start(
    command: ProtocolCommand,
    session: SessionState,
    repo_root: Path,
    parse_mode: str,
) -> tuple[str, list[Path], str, str]:
    normalized_payload = _render_patch_payload_for_prompt(command.payload)
    patch_id = hashlib.sha256(normalized_payload.encode("utf-8")).hexdigest()[:12]
    affected_paths = _extract_patch_repo_paths(command.payload)
    repo_head_before = _safe_get_head_commit(repo_root)
    status_before = _safe_status_summary(repo_root, affected_paths)

    if _ACTIVE_RUN_LOG is not None:
        summary_lines = [
            f"Session id: {session.session_id}",
            f"Protocol parse mode: {parse_mode}",
            f"Patch id: {patch_id}",
            f"Patch format: {command.payload.get('patch_format', '')}",
            f"Task summary (EN): {command.task_summary_en}",
            f"Current goal (EN): {command.current_goal_en}",
            "Affected file entries:",
        ]
        files = command.payload.get("files")
        if isinstance(files, list) and files:
            for index, file_entry in enumerate(files, start=1):
                if not isinstance(file_entry, dict):
                    continue
                op = str(file_entry.get("operation", "replace"))
                path = str(file_entry.get("path", ""))
                summary_lines.append(f"  [{index}] {op} {path}")
                expected_sha256 = file_entry.get("expected_sha256")
                if isinstance(expected_sha256, str) and expected_sha256.strip():
                    summary_lines.append(f"      expected_sha256: {expected_sha256.strip()}")
                replacements = file_entry.get("replacements")
                if isinstance(replacements, list):
                    for repl_index, replacement in enumerate(replacements, start=1):
                        if not isinstance(replacement, dict):
                            continue
                        summary_lines.append(
                            f"      replacement[{repl_index}] expected_matches={replacement.get('expected_matches', 1)}"
                        )
        else:
            summary_lines.append("  <none>")
        _ACTIVE_RUN_LOG.add_section("PATCH COMMAND SUMMARY", "\n".join(summary_lines))
        _ACTIVE_RUN_LOG.add_section("EFFECTIVE PATCH PAYLOAD", normalized_payload)
        _ACTIVE_RUN_LOG.add_section("NORMALIZED PATCH PAYLOAD", normalized_payload)
        _ACTIVE_RUN_LOG.add_section(
            "TARGET REPO STATE BEFORE PATCH",
            "\n".join(
                [
                    f"Target repo HEAD before patch: {repo_head_before}",
                    "Target repo status before patch:",
                    status_before,
                ]
            ),
        )
    return patch_id, affected_paths, repo_head_before, status_before


def _record_patch_failure(
    *,
    command: ProtocolCommand,
    repo_root: Path,
    affected_paths: list[Path],
    repo_head_before: str,
    exc: PatchApplyError,
    parse_mode: str,
    source_windows: str = "",
    repair_prompt_generated: bool = False,
) -> None:
    if _ACTIVE_RUN_LOG is None:
        return
    repo_head_after = _safe_get_head_commit(repo_root)
    status_after = _safe_status_summary(repo_root, affected_paths)
    lines = [
        "Patch decision: rejected",
        f"Protocol parse mode: {parse_mode}",
        f"Failure stage: {getattr(exc, 'stage', 'unknown')}",
        f"Failure reason: {exc}",
        f"Why repair prompt was generated: {exc}" if repair_prompt_generated else "Why repair prompt was generated: <not generated>",
        "Affected paths:",
    ]
    if affected_paths:
        lines.extend(f"  - {path.as_posix()}" for path in affected_paths)
    else:
        lines.append("  - <none>")
    details = getattr(exc, "details", {}) if isinstance(getattr(exc, "details", {}), dict) else {}
    if details:
        lines.append("Failure details:")
        for key, value in sorted(details.items()):
            lines.append(f"  {key}: {value}")
    _ACTIVE_RUN_LOG.add_section("PATCH VALIDATION", "\n".join(lines))
    _ACTIVE_RUN_LOG.add_section(
        "PATCH APPLY RESULT",
        "\n".join(
            [
                f"Target repo HEAD before patch: {repo_head_before}",
                f"Target repo HEAD after patch: {repo_head_after}",
                "Post-failure git status:",
                status_after,
            ]
        ),
    )
    resulting_diff = _safe_get_paths_diff(repo_root, affected_paths)
    if resulting_diff:
        _ACTIVE_RUN_LOG.add_section("PATCH RESULTING DIFF", resulting_diff)
    if source_windows:
        _ACTIVE_RUN_LOG.add_section("PATCH FAILURE CONTEXT", source_windows)


def _record_patch_success(
    *,
    command: ProtocolCommand,
    repo_root: Path,
    affected_paths: list[Path],
    repo_head_before: str,
    result,
    parse_mode: str,
) -> None:
    if _ACTIVE_RUN_LOG is None:
        return
    repo_head_after = _safe_get_head_commit(repo_root)
    status_after = result.git_status_summary.strip() or _safe_status_summary(repo_root, affected_paths)
    fallbacks = list(result.fallbacks_used)
    if parse_mode != "v2_strict":
        fallbacks.append(f"protocol_parse_mode={parse_mode}")
    if result.warning:
        fallbacks.append(result.warning)

    validation_lines = [
        "Patch decision: accepted",
        f"Protocol parse mode: {parse_mode}",
        f"Failure stage: <none>",
        f"Failure reason: <none>",
        "Affected paths:",
    ]
    validation_lines.extend(f"  - {path}" for path in result.affected_files)
    validation_lines.append("Fallbacks used:")
    if fallbacks:
        validation_lines.extend(f"  - {fallback}" for fallback in fallbacks)
    else:
        validation_lines.append("  - <none>")
    _ACTIVE_RUN_LOG.add_section("PATCH VALIDATION", "\n".join(validation_lines))
    _ACTIVE_RUN_LOG.add_section(
        "PATCH APPLY RESULT",
        "\n".join(
            [
                f"Target repo HEAD before patch: {repo_head_before}",
                f"Target repo HEAD after patch: {repo_head_after}",
                "Post-apply git status:",
                status_after,
                "",
                _format_patch_file_results(result.file_results),
            ]
        ).strip(),
    )
    resulting_diff = _safe_get_paths_diff(repo_root, affected_paths)
    if resulting_diff:
        _ACTIVE_RUN_LOG.add_section("PATCH RESULTING DIFF", resulting_diff)


def _extract_patch_repo_paths(payload: dict[str, object]) -> list[Path]:
    files = payload.get("files")
    if not isinstance(files, list):
        return []
    paths: list[Path] = []
    for file_entry in files:
        if not isinstance(file_entry, dict):
            continue
        path_text = str(file_entry.get("path", "")).replace("\\", "/").strip()
        if not path_text:
            continue
        paths.append(Path(*[part for part in path_text.split("/") if part]))
    return paths


def _safe_get_head_commit(repo_root: Path) -> str:
    try:
        return get_head_commit(repo_root)
    except Exception as exc:  # noqa: BLE001
        return f"<unknown: {exc}>"


def _safe_status_summary(repo_root: Path, paths: list[Path]) -> str:
    if not paths:
        return "<no affected paths>"
    try:
        summary = summarize_paths_status(repo_root, paths).strip()
        return summary or "<clean>"
    except Exception as exc:  # noqa: BLE001
        return f"<failed to read status: {exc}>"


def _safe_get_paths_diff(repo_root: Path, paths: list[Path]) -> str:
    if not paths:
        return ""
    try:
        return get_paths_diff(repo_root, paths)
    except Exception as exc:  # noqa: BLE001
        return f"<failed to read diff: {exc}>"


def _format_patch_file_results(file_results) -> str:
    lines = ["Per-file results:"]
    if not file_results:
        lines.append("  <none>")
        return "\n".join(lines)
    for file_result in file_results:
        lines.append(f"  Path: {file_result.path}")
        lines.append(f"    operation: {file_result.operation}")
        lines.append(f"    expected_sha256: {file_result.expected_sha256 or '<none>'}")
        lines.append(f"    before_sha256: {file_result.before_sha256 or '<absent>'}")
        lines.append(f"    after_sha256: {file_result.after_sha256 or '<absent>'}")
        if file_result.replacement_results:
            lines.append("    replacement_results:")
            for index, replacement in enumerate(file_result.replacement_results, start=1):
                rendered_lines = ", ".join(str(line) for line in replacement.matched_line_numbers) or "<none>"
                lines.append(
                    f"      [{index}] expected={replacement.expected_matches}, found={replacement.found_matches}, lines={rendered_lines}"
                )
        else:
            lines.append("    replacement_results: <none>")
    return "\n".join(lines)


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
    session.protocol_revision = CURRENT_PROTOCOL_REVISION
    session.followup_prompt_count = 0
    session.last_generated_prompt = prompt
    session.last_truncation_report = ""
    session_store.save(session)
    if forced:
        print(
            _human_text(
                config.human_language,
                "Ð ÐµÐ¶Ð¸Ð¼ bootstrap Ð²ÐºÐ»ÑŽÑ‡ÐµÐ½ Ð¿Ñ€Ð¸Ð½ÑƒÐ´Ð¸Ñ‚ÐµÐ»ÑŒÐ½Ð¾.",
                "Bootstrap mode was forced.",
            )
        )
    print(_human_text(config.human_language, "ÐŸÐµÑ€Ð²Ñ‹Ð¹ Ð·Ð°Ð¿ÑƒÑÐº Ð´Ð»Ñ ÑÑ‚Ð¾Ð³Ð¾ Ñ€ÐµÐ¿Ð¾Ð·Ð¸Ñ‚Ð¾Ñ€Ð¸Ñ.", "First run for this repository."))
    print(
        _human_text(
            config.human_language,
            "Ð’ Ð±ÑƒÑ„ÐµÑ€ Ð¾Ð±Ð¼ÐµÐ½Ð° Ð¿Ð¾Ð¼ÐµÑ‰ÐµÐ½ bootstrap prompt Ð´Ð»Ñ ChatGPT.",
            "A bootstrap prompt for ChatGPT was copied to the clipboard.",
        )
    )
    _print_next_step(
        config.human_language,
        _human_text(
            config.human_language,
            "Ð²ÑÑ‚Ð°Ð²ÑŒ ÑÑ‚Ð¾Ñ‚ prompt Ð² ChatGPT Ð²Ð¼ÐµÑÑ‚Ðµ Ñ Ð¸ÑÑ…Ð¾Ð´Ð½Ñ‹Ð¼ Ð¾Ð¿Ð¸ÑÐ°Ð½Ð¸ÐµÐ¼ Ð·Ð°Ð´Ð°Ñ‡Ð¸ Ð¸ Ð·Ð°Ñ‚ÐµÐ¼ ÑÐºÐ¾Ð¿Ð¸Ñ€ÑƒÐ¹ Ð¿Ð¾Ð»Ð½Ñ‹Ð¹ Ð¾Ñ‚Ð²ÐµÑ‚ ChatGPT Ð² Ð±ÑƒÑ„ÐµÑ€ Ð¾Ð±Ð¼ÐµÐ½Ð°.",
            "paste this prompt into ChatGPT together with the original task description, then copy the full ChatGPT reply to the clipboard.",
        ),
    )
    return 0


def _refresh_session_protocol_revision(session: SessionState) -> bool:
    if session.protocol_revision == CURRENT_PROTOCOL_REVISION:
        return False
    session.protocol_revision = CURRENT_PROTOCOL_REVISION
    session.initialized = False
    session.followup_prompt_count = 0
    session.last_generated_prompt = ""
    session.last_truncation_report = ""
    session.last_parsed_llm_response = {}
    return True


def _handle_llm_response(
    clipboard_text: str,
    config: DevloopConfig,
    retriever: RepositoryRetriever,
    session_store: SessionStore,
    session: SessionState,
) -> None:
    try:
        envelope = parse_protocol_response(clipboard_text)
    except ProtocolError as exc:
        _record_llm_protocol_failure(clipboard_text, session, exc)
        raise

    _record_llm_command_context(envelope, session)
    command = envelope.command
    session.last_parsed_llm_response = command.to_session_summary()
    session.last_known_task_summary = command.task_summary_en
    session.last_known_current_goal = command.current_goal_en
    session.add_history_entry(f"{command.command}: {command.current_goal_en}")

    if command.command == "COLLECT_CONTEXT":
        _handle_collect_context(command, config, retriever, session_store, session)
        return
    if command.command == "APPLY_PATCH":
        _handle_apply_patch(command, config, retriever, session_store, session, envelope.parse_mode)
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
            "Ð’ Ð±ÑƒÑ„ÐµÑ€ Ð¾Ð±Ð¼ÐµÐ½Ð° Ð¿Ð¾Ð¼ÐµÑ‰ÐµÐ½ Ð½Ð¾Ð²Ñ‹Ð¹ prompt Ð´Ð»Ñ ChatGPT.",
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
    parse_mode: str,
) -> None:
    repo_root = Path(session.repo_root)
    _patch_id, affected_paths, repo_head_before, _status_before = _record_patch_attempt_start(
        command,
        session,
        repo_root,
        parse_mode,
    )
    try:
        result = apply_patch_payload(
            repo_root=repo_root,
            state_dir=session_store.state_dir,
            payload=command.payload,
            allow_apply_on_dirty_files=config.allow_apply_on_dirty_files,
        )
    except PatchInfrastructureError as exc:
        _record_patch_failure(
            command=command,
            repo_root=repo_root,
            affected_paths=affected_paths,
            repo_head_before=repo_head_before,
            exc=exc,
            parse_mode=parse_mode,
        )
        session.add_history_entry(f"PATCH_LOCAL_ERROR: {exc}")
        session_store.save(session)
        print(
            _human_text(
                config.human_language,
                "ÐŸÐ°Ñ‚Ñ‡ Ð½Ðµ Ð¿Ñ€Ð¸Ð¼ÐµÐ½ÐµÐ½ Ð¸Ð·-Ð·Ð° Ð»Ð¾ÐºÐ°Ð»ÑŒÐ½Ð¾Ð¹ Ð¾ÑˆÐ¸Ð±ÐºÐ¸ Git Ð¸Ð»Ð¸ Ñ„Ð°Ð¹Ð»Ð¾Ð²Ð¾Ð¹ ÑÐ¸ÑÑ‚ÐµÐ¼Ñ‹.",
                "Patch was not applied because of a local Git or filesystem error.",
            )
        )
        print(str(exc))
        _print_next_step(
            config.human_language,
            _human_text(
                config.human_language,
                "Ð¿Ñ€Ð¾Ð²ÐµÑ€ÑŒ, Ñ‡Ñ‚Ð¾ Ð´Ñ€ÑƒÐ³Ð¾Ð¹ git-Ð¿Ñ€Ð¾Ñ†ÐµÑÑ Ð½Ðµ Ð´ÐµÑ€Ð¶Ð¸Ñ‚ .git/index.lock Ð¸ Ñ‡Ñ‚Ð¾ Ñ€ÐµÐ¿Ð¾Ð·Ð¸Ñ‚Ð¾Ñ€Ð¸Ð¹ Ð´Ð¾ÑÑ‚ÑƒÐ¿ÐµÐ½ Ð½Ð° Ð·Ð°Ð¿Ð¸ÑÑŒ, Ð·Ð°Ñ‚ÐµÐ¼ ÑÐ½Ð¾Ð²Ð° Ð·Ð°Ð¿ÑƒÑÑ‚Ð¸ Ñ‚Ñƒ Ð¶Ðµ ÐºÐ¾Ð¼Ð°Ð½Ð´Ñƒ.",
                "check that no other git process holds .git/index.lock and that the repository is writable, then run the same command again.",
            ),
        )
        return
    except PatchApplyError as exc:
        source_windows = _build_patch_repair_source_windows(retriever, command.payload)
        _record_patch_failure(
            command=command,
            repo_root=repo_root,
            affected_paths=affected_paths,
            repo_head_before=repo_head_before,
            exc=exc,
            parse_mode=parse_mode,
            source_windows=source_windows,
            repair_prompt_generated=True,
        )
        repair_sections = [
            PromptSection("Patch apply failure", str(exc), required=True),
            PromptSection(
                "Repair rules",
                (
                    "Return exactly one machine-readable command block.\n"
                    "Prefer APPLY_PATCH if you can correct the patch now.\n"
                    "Use DEVLOOP_COMMAND_V2.\n"
                    "Use PATCH_FORMAT: SEARCH_REPLACE_BLOCKS_V1.\n"
                    "Use one file section per path and exact SEARCH/REPLACE blocks.\n"
                    "Allowed file operations are OP: REPLACE, OP: CREATE_FILE, and OP: DELETE_FILE.\n"
                    "Use COLLECT_CONTEXT only if the current repository context is insufficient.\n"
                    "Use ASK_HUMAN only if a manual run or manual answer is required.\n"
                    "For replace operations, provide exact current text in each SEARCH block and set MATCH_COUNT explicitly.\n"
                    "For create operations, provide CONTENT and omit SEARCH/REPLACE blocks.\n"
                    "For delete operations, omit SEARCH/REPLACE blocks and CONTENT."
                ),
                required=True,
            ),
            PromptSection(
                "Rejected patch payload",
                _render_patch_payload_for_prompt(command.payload),
                compact_body=_compact_body(_render_patch_payload_for_prompt(command.payload), 80),
            ),
        ]
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
        print(_human_text(config.human_language, "Patch Ð½Ðµ Ð¿Ñ€Ð¸Ð¼ÐµÐ½ÐµÐ½ Ð°Ð²Ñ‚Ð¾Ð¼Ð°Ñ‚Ð¸Ñ‡ÐµÑÐºÐ¸.", "Patch was not applied automatically."))
        print(
            _human_text(
                config.human_language,
                "Ð’ Ð±ÑƒÑ„ÐµÑ€ Ð¾Ð±Ð¼ÐµÐ½Ð° Ð¿Ð¾Ð¼ÐµÑ‰ÐµÐ½ repair prompt Ð´Ð»Ñ ChatGPT.",
                "A repair prompt for ChatGPT was copied to the clipboard.",
            )
        )
        _print_next_step(
            config.human_language,
            _human_text(
                config.human_language,
                "Ð²ÑÑ‚Ð°Ð²ÑŒ repair prompt Ð² ChatGPT Ð¸ Ð¿Ð¾Ð»ÑƒÑ‡Ð¸ Ð¸ÑÐ¿Ñ€Ð°Ð²Ð»ÐµÐ½Ð½Ñ‹Ð¹ Ð¾Ñ‚Ð²ÐµÑ‚ Ñ Ð¾Ð´Ð½Ð¾Ð¹ machine-readable ÐºÐ¾Ð¼Ð°Ð½Ð´Ð¾Ð¹.",
                "paste the repair prompt into ChatGPT and get a corrected reply with exactly one machine-readable command.",
            ),
        )
        return

    summary = ", ".join(result.affected_files)
    session.last_applied_patch_summary = summary
    session.add_history_entry(f"APPLY_PATCH: {summary}")
    _record_patch_success(
        command=command,
        repo_root=repo_root,
        affected_paths=affected_paths,
        repo_head_before=repo_head_before,
        result=result,
        parse_mode=parse_mode,
    )
    print(_human_text(config.human_language, "Patch Ð¿Ñ€Ð¾Ð²ÐµÑ€ÐµÐ½ Ð¸ Ð¿Ñ€Ð¸Ð¼ÐµÐ½ÐµÐ½.", "Patch was validated and applied."))
    if result.git_status_summary:
        print(_human_text(config.human_language, "Ð˜Ð·Ð¼ÐµÐ½ÐµÐ½Ð½Ñ‹Ðµ Ð¿ÑƒÑ‚Ð¸:", "Changed paths:"))
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
    if parsed.succeeded and parsed.total_errors == 0:
        current_goal = "Compilation completed without blocking errors. Decide the next narrow manual step."
        status_message = _human_text(
            config.human_language,
            f"ÃÂÃÂ°ÃÂ¹ÃÂ´ÃÂµÃÂ½ sbt compile log: successful run, ÃÂ¾Ã‘Ë†ÃÂ¸ÃÂ±ÃÂ¾ÃÂº {parsed.total_errors}, warning lines {parsed.raw_warning_lines}.",
            f"Detected sbt compile log: successful run, errors {parsed.total_errors}, warning lines {parsed.raw_warning_lines}.",
        )
    else:
        current_goal = session.last_known_current_goal or "Analyze the compile diagnostics and propose the smallest safe next step."
        status_message = _human_text(
            config.human_language,
            f"ÃÂÃÂ°ÃÂ¹ÃÂ´ÃÂµÃÂ½ sbt compile log: ÃÂ¾Ã‘Ë†ÃÂ¸ÃÂ±ÃÂ¾ÃÂº {parsed.total_errors}, Ã‘â€žÃÂ°ÃÂ¹ÃÂ»ÃÂ¾ÃÂ² {parsed.file_count}.",
            f"Detected sbt compile log: errors {parsed.total_errors}, files {parsed.file_count}.",
        )
    prompt_result = _build_followup_prompt(
        session=session,
        task_summary=session.last_known_task_summary or "Diagnose the current Scala compile failure.",
        current_goal=current_goal,
        source_label="Clipboard sbt compile output",
        human_language_name=config.human_language_name,
        sections=_query_results_to_sections(query_results),
        max_chars=config.max_prompt_chars,
    )
    set_clipboard_text(prompt_result.text)
    session.last_generated_prompt = prompt_result.text
    session.last_truncation_report = prompt_result.truncation_report
    session_store.save(session)
    print(status_message)
    print(
        _human_text(
            config.human_language,
            "Ãâ€™ ÃÂ±Ã‘Æ’Ã‘â€žÃÂµÃ‘â‚¬ ÃÂ¾ÃÂ±ÃÂ¼ÃÂµÃÂ½ÃÂ° ÃÂ¿ÃÂ¾ÃÂ¼ÃÂµÃ‘â€°ÃÂµÃÂ½ ÃÂ½ÃÂ¾ÃÂ²Ã‘â€¹ÃÂ¹ prompt ÃÂ´ÃÂ»Ã‘Â ChatGPT.",
            "A new prompt for ChatGPT was copied to the clipboard.",
        )
    )
    _print_next_step(
        config.human_language,
        _human_text(
            config.human_language,
            "ÃÂ²Ã‘ÂÃ‘â€šÃÂ°ÃÂ²Ã‘Å’ Ã‘ÂÃ‘â€šÃÂ¾Ã‘â€š prompt ÃÂ² ChatGPT, ÃÂ¿ÃÂ¾ÃÂ»Ã‘Æ’Ã‘â€¡ÃÂ¸ ÃÂ¾Ã‘â€šÃÂ²ÃÂµÃ‘â€š, Ã‘ÂÃÂºÃÂ¾ÃÂ¿ÃÂ¸Ã‘â‚¬Ã‘Æ’ÃÂ¹ ÃÂµÃÂ³ÃÂ¾ ÃÂ² ÃÂ±Ã‘Æ’Ã‘â€žÃÂµÃ‘â‚¬ ÃÂ¸ Ã‘ÂÃÂ½ÃÂ¾ÃÂ²ÃÂ° ÃÂ·ÃÂ°ÃÂ¿Ã‘Æ’Ã‘ÂÃ‘â€šÃÂ¸ Ã‘â€šÃ‘Æ’ ÃÂ¶ÃÂµ ÃÂºÃÂ¾ÃÂ¼ÃÂ°ÃÂ½ÃÂ´Ã‘Æ’.",
            "paste this prompt into ChatGPT, get the reply, copy it to the clipboard, and run the same command again.",
        ),
    )
    return
    print(
        _human_text(
            config.human_language,
            f"ÐÐ°Ð¹Ð´ÐµÐ½ sbt compile log: Ð¾ÑˆÐ¸Ð±Ð¾Ðº {parsed.total_errors}, Ñ„Ð°Ð¹Ð»Ð¾Ð² {parsed.file_count}.",
            f"Detected sbt compile log: errors {parsed.total_errors}, files {parsed.file_count}.",
        )
    )
    print(
        _human_text(
            config.human_language,
            "Ð’ Ð±ÑƒÑ„ÐµÑ€ Ð¾Ð±Ð¼ÐµÐ½Ð° Ð¿Ð¾Ð¼ÐµÑ‰ÐµÐ½ Ð½Ð¾Ð²Ñ‹Ð¹ prompt Ð´Ð»Ñ ChatGPT.",
            "A new prompt for ChatGPT was copied to the clipboard.",
        )
    )
    _print_next_step(
        config.human_language,
        _human_text(
            config.human_language,
            "Ð²ÑÑ‚Ð°Ð²ÑŒ ÑÑ‚Ð¾Ñ‚ prompt Ð² ChatGPT, Ð¿Ð¾Ð»ÑƒÑ‡Ð¸ Ð¾Ñ‚Ð²ÐµÑ‚, ÑÐºÐ¾Ð¿Ð¸Ñ€ÑƒÐ¹ ÐµÐ³Ð¾ Ð² Ð±ÑƒÑ„ÐµÑ€ Ð¸ ÑÐ½Ð¾Ð²Ð° Ð·Ð°Ð¿ÑƒÑÑ‚Ð¸ Ñ‚Ñƒ Ð¶Ðµ ÐºÐ¾Ð¼Ð°Ð½Ð´Ñƒ.",
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
            f"ÐÐ°Ð¹Ð´ÐµÐ½ sbt test log: Ð¿Ð°Ð´ÐµÐ½Ð¸Ð¹ {parsed.total_failures}.",
            f"Detected sbt test log: failures {parsed.total_failures}.",
        )
    )
    print(
        _human_text(
            config.human_language,
            "Ð’ Ð±ÑƒÑ„ÐµÑ€ Ð¾Ð±Ð¼ÐµÐ½Ð° Ð¿Ð¾Ð¼ÐµÑ‰ÐµÐ½ Ð½Ð¾Ð²Ñ‹Ð¹ prompt Ð´Ð»Ñ ChatGPT.",
            "A new prompt for ChatGPT was copied to the clipboard.",
        )
    )
    _print_next_step(
        config.human_language,
        _human_text(
            config.human_language,
            "Ð²ÑÑ‚Ð°Ð²ÑŒ ÑÑ‚Ð¾Ñ‚ prompt Ð² ChatGPT, Ð¿Ð¾Ð»ÑƒÑ‡Ð¸ Ð¾Ñ‚Ð²ÐµÑ‚, ÑÐºÐ¾Ð¿Ð¸Ñ€ÑƒÐ¹ ÐµÐ³Ð¾ Ð² Ð±ÑƒÑ„ÐµÑ€ Ð¸ ÑÐ½Ð¾Ð²Ð° Ð·Ð°Ð¿ÑƒÑÑ‚Ð¸ Ñ‚Ñƒ Ð¶Ðµ ÐºÐ¾Ð¼Ð°Ð½Ð´Ñƒ.",
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
            "Ð Ð°ÑÐ¿Ð¾Ð·Ð½Ð°Ð½ Ð¾Ð±Ñ‹Ñ‡Ð½Ñ‹Ð¹ Ñ‚ÐµÐºÑÑ‚ Ð¸Ð»Ð¸ Ð»Ð¾Ð³ Ð±ÐµÐ· ÑÐ¿ÐµÑ†Ð¸Ð°Ð»ÑŒÐ½Ð¾Ð³Ð¾ Ñ„Ð¾Ñ€Ð¼Ð°Ñ‚Ð°.",
            "Detected plain text or a log without a special format.",
        )
    )
    print(
        _human_text(
            config.human_language,
            "Ð’ Ð±ÑƒÑ„ÐµÑ€ Ð¾Ð±Ð¼ÐµÐ½Ð° Ð¿Ð¾Ð¼ÐµÑ‰ÐµÐ½ Ð½Ð¾Ð²Ñ‹Ð¹ prompt Ð´Ð»Ñ ChatGPT.",
            "A new prompt for ChatGPT was copied to the clipboard.",
        )
    )
    _print_next_step(
        config.human_language,
        _human_text(
            config.human_language,
            "Ð²ÑÑ‚Ð°Ð²ÑŒ ÑÑ‚Ð¾Ñ‚ prompt Ð² ChatGPT, Ð¿Ð¾Ð»ÑƒÑ‡Ð¸ Ð¾Ñ‚Ð²ÐµÑ‚, ÑÐºÐ¾Ð¿Ð¸Ñ€ÑƒÐ¹ ÐµÐ³Ð¾ Ð² Ð±ÑƒÑ„ÐµÑ€ Ð¸ ÑÐ½Ð¾Ð²Ð° Ð·Ð°Ð¿ÑƒÑÑ‚Ð¸ Ñ‚Ñƒ Ð¶Ðµ ÐºÐ¾Ð¼Ð°Ð½Ð´Ñƒ.",
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


def _render_patch_payload_for_prompt(payload: dict[str, object]) -> str:
    if str(payload.get("patch_format", "")) != "search_replace_v1":
        return str(payload)
    files = payload.get("files")
    if not isinstance(files, list) or not files:
        return "PATCH_FORMAT: SEARCH_REPLACE_BLOCKS_V1"

    rendered: list[str] = ["PATCH_FORMAT: SEARCH_REPLACE_BLOCKS_V1"]
    for file_entry in files:
        if not isinstance(file_entry, dict):
            continue
        rendered.append("*** BEGIN FILE ***")
        rendered.append(f"PATH: {str(file_entry.get('path', '')).replace('\\', '/')}")
        operation = str(file_entry.get("operation", "replace")).strip().lower()
        if operation == "create":
            rendered.append("OP: CREATE_FILE")
        elif operation == "delete":
            rendered.append("OP: DELETE_FILE")
        else:
            rendered.append("OP: REPLACE")
        expected_sha256 = file_entry.get("expected_sha256")
        if isinstance(expected_sha256, str) and expected_sha256.strip():
            rendered.append(f"EXPECTED_SHA256: sha256:{expected_sha256.strip()}")

        if operation == "create":
            content = file_entry.get("content")
            if isinstance(content, str):
                rendered.append("@@@CONTENT@@@")
                rendered.append(content)
                rendered.append("@@@END@@@")
        elif operation == "replace":
            replacements = file_entry.get("replacements")
            if isinstance(replacements, list):
                for replacement in replacements:
                    if not isinstance(replacement, dict):
                        continue
                    expected_matches = replacement.get("expected_matches", 1)
                    rendered.append(f"MATCH_COUNT: {expected_matches}")
                    rendered.append("@@@SEARCH@@@")
                    rendered.append(str(replacement.get("search", "")))
                    rendered.append("@@@REPLACE@@@")
                    rendered.append(str(replacement.get("replace", "")))
                    rendered.append("@@@END@@@")
        rendered.append("*** END FILE ***")
    return "\n".join(rendered).strip()


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


def _resolve_detection(
    text: str,
    force_mode: str,
    session: SessionState | None = None,
) -> tuple[DetectionResult, bool]:
    if force_mode == "auto":
        detection = detect_clipboard_content(text)
        return _maybe_reclassify_compile_success(text, detection, session), False
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


def _maybe_reclassify_compile_success(
    text: str,
    detection: DetectionResult,
    session: SessionState | None,
) -> DetectionResult:
    if detection.kind != ClipboardKind.RAW_TEXT:
        return detection
    if session is None:
        return detection
    if not _looks_like_generic_sbt_success(text):
        return detection
    if not _session_expects_compile(session):
        return detection
    return DetectionResult(
        kind=ClipboardKind.SBT_COMPILE,
        score=4,
        reasons=detection.reasons
        + ["Reclassified generic sbt success output as compile using session context"],
    )


def _looks_like_generic_sbt_success(text: str) -> bool:
    if "[success] Total time:" not in text:
        return False
    markers = 0
    patterns = (
        r"(?im)^\[info\]\s+welcome to sbt\b",
        r"(?im)^\[info\]\s+loading settings for project\b",
        r"(?im)^\[info\]\s+loading project definition\b",
        r"(?im)^\[info\]\s+set current project to\b",
        r"(?im)^\[info\]\s+Reapplying settings\b",
    )
    for pattern in patterns:
        if re.search(pattern, text):
            markers += 1
    return markers >= 2


def _session_expects_compile(session: SessionState) -> bool:
    last_response = session.last_parsed_llm_response if isinstance(session.last_parsed_llm_response, dict) else {}
    haystacks = [
        str(last_response.get("next_step_human", "")),
        str(last_response.get("current_goal_en", "")),
        str(last_response.get("task_summary_en", "")),
        str(session.last_known_current_goal),
        str(session.last_known_task_summary),
    ]
    combined = "\n".join(haystacks).lower()
    return "compile" in combined or "ÐºÐ¾Ð¼Ð¿Ð¸Ð»" in combined or "ÑÐ±Ð¾Ñ€Ðº" in combined


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
            "Ð Ð°ÑÐ¿Ð¾Ð·Ð½Ð°Ð½ Ð¾Ñ‚Ð²ÐµÑ‚ LLM Ñ machine-readable ÐºÐ¾Ð¼Ð°Ð½Ð´Ð¾Ð¹.",
            "Detected an LLM response with a machine-readable command.",
        ),
        ClipboardKind.SBT_COMPILE: _human_text(
            human_language,
            "Ð Ð°ÑÐ¿Ð¾Ð·Ð½Ð°Ð½ sbt compile output.",
            "Detected sbt compile output.",
        ),
        ClipboardKind.SBT_TEST: _human_text(
            human_language,
            "Ð Ð°ÑÐ¿Ð¾Ð·Ð½Ð°Ð½ sbt test output.",
            "Detected sbt test output.",
        ),
        ClipboardKind.RAW_TEXT: _human_text(
            human_language,
            "Ð Ð°ÑÐ¿Ð¾Ð·Ð½Ð°Ð½ Ð¾Ð±Ñ‹Ñ‡Ð½Ñ‹Ð¹ Ñ‚ÐµÐºÑÑ‚ Ð¸Ð· Ð±ÑƒÑ„ÐµÑ€Ð° Ð¾Ð±Ð¼ÐµÐ½Ð°.",
            "Detected plain text from the clipboard.",
        ),
    }
    if forced:
        prefix = _human_text(human_language, "ÐŸÑ€Ð¸Ð½ÑƒÐ´Ð¸Ñ‚ÐµÐ»ÑŒÐ½Ð¾ Ð²Ñ‹Ð±Ñ€Ð°Ð½ Ñ€ÐµÐ¶Ð¸Ð¼:", "Forced mode selected:")
        print(f"{prefix} {messages[kind]}")
        return
    print(messages[kind])


def _configure_stdout() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")


class _ConsoleTee(io.TextIOBase):
    def __init__(self, stream, run_log: RunLogRecorder) -> None:
        self._stream = stream
        self._run_log = run_log

    def write(self, text: str) -> int:
        self._run_log.append_console(text)
        return self._stream.write(text)

    def flush(self) -> None:
        self._stream.flush()

    @property
    def encoding(self) -> str | None:
        return getattr(self._stream, "encoding", None)


def _print_next_step(human_language: str, message: str) -> None:
    prefix = "Ð”Ð°Ð»ÑŒÑˆÐµ" if human_language == "ru" else "Next"
    print(f"{prefix}: {message}")


def _human_text(human_language: str, ru_text: str, en_text: str) -> str:
    return ru_text if human_language == "ru" else en_text
