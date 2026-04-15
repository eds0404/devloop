# devloop

`devloop` is a Windows-first local human-in-the-loop development assistant for Scala/sbt repositories. It is not an autonomous agent. The human uses ChatGPT manually, and the Windows clipboard is the transport channel between ChatGPT and the local tool.

## Workflow

1. Describe the problem in ChatGPT.
2. Run:

```powershell
python -m devloop --config C:\path\to\devloop.yaml
```

3. On the first run for a repository, `devloop` puts an English bootstrap prompt into the clipboard.
4. Paste that bootstrap prompt into ChatGPT together with the original task.
5. ChatGPT replies with:
   - a short explanation in the configured human language,
   - exactly one machine-readable command block.
6. Copy the full ChatGPT reply to the clipboard.
7. Run the same `python -m devloop --config ...` command again.
8. `devloop` auto-detects the clipboard content and does one of the following:
   - parses and executes a validated `COLLECT_CONTEXT` command,
   - validates and applies a safe Git patch from `APPLY_PATCH`,
   - shows manual instructions for `ASK_HUMAN`,
   - stores state and exits for `DONE`,
   - parses sbt compile output and prepares a compact prompt,
   - parses sbt test output and prepares a compact prompt,
   - wraps generic clipboard text into a compact prompt.

The normal loop always uses one repeated command. There are no operational modes such as `--ingest-test` or `--from-clipboard`.
When auto-detection needs help, you can optionally use `--force-bootstrap` or `--force-mode`.

## Installation

```powershell
py -3.11 -m pip install -e .
```

## Configuration

Print a starter config:

```powershell
python -m devloop --print-default-config > devloop.yaml
```

Optional troubleshooting flags:

- `--force-bootstrap` generates only the bootstrap/protocol prompt and ignores clipboard contents.
- `--force-mode llm|compile|test|raw` overrides clipboard auto-detection for the current run.
- `--reset-session` recreates the local session metadata for the current repository.

Run log:

- Every run with `--config ...` appends one flat-text entry to `.devloop.log` next to the config file.
- Each entry contains the current `devloop` Git HEAD, CLI arguments, clipboard input, raw config file text, console output, and the clipboard text written by `devloop`.
- For `APPLY_PATCH`, the same log also records the extracted machine block, protocol parse mode, normalized patch payload, patch id, target repository state before and after the attempt, per-file sha256 values, replacement match results, resulting diff, and exact failure stage when a patch is rejected.
- The log is append-only and intended for diagnosis of protocol, parser, and clipboard issues.

Example config:

```yaml
project_root: C:\path\to\scala-project
max_prompt_chars: 120000
max_files: 12
max_snippet_lines: 180
max_search_results: 20
max_error_groups: 20
max_test_failures: 10
include_globs:
  - "**/*"
exclude_globs:
  - ".git/**"
  - "target/**"
  - ".bloop/**"
  - ".metals/**"
  - ".idea/**"
  - "project/target/**"
snippet_context_before: 25
snippet_context_after: 40
project_packages: []
include_project_summary_in_prompts: false
allow_apply_on_dirty_files: false
state_dir_mode: localappdata
prompt_language: en
human_language: ru
```

By default, `devloop` does not append a project tree summary to every generated prompt. Set `include_project_summary_in_prompts: true` only if you explicitly want that extra context in compile/test/raw follow-up prompts.

Session metadata is stored outside the repository under `%LOCALAPPDATA%\devloop\repos\<hash>\session.yaml`.
If the session file becomes corrupted, use `--reset-session`, or run `--force-bootstrap` to regenerate the protocol prompt and automatically recover from a broken session file.
An explicit `project_tree` request from the LLM still returns the full configured project tree summary even when `include_project_summary_in_prompts` is `false`.

## Clipboard-Based Loop

`devloop` inspects clipboard content in this order:

1. LLM response containing `<<<DEVLOOP_COMMAND_START>>>` and `<<<DEVLOOP_COMMAND_END>>>`
2. sbt compile output
3. sbt test output
4. generic raw text/log content

All prompts generated for ChatGPT are in English.
Console output shown to the human follows `human_language` and currently supports `ru` and `en`.
The first bootstrap prompt always includes the full protocol/capability reference. Follow-up prompts include the same full reference every other prompt to save space, with a short reminder on the alternating prompts.

## Supported Commands

### `COLLECT_CONTEXT`

Asks the local tool to gather deterministic repository context and build a compact English prompt for ChatGPT.

Supported query types:

- `project_tree`
- `file_search`
- `path_search`
- `text_search`
- `regex_search`
- `read_file`
- `read_snippet`
- `read_around_match`
- `related_files`
- `related_tests`

### `APPLY_PATCH`

Applies a validated structured `search_replace_v1` patch inside the repository root. The tool performs exact text replacement for existing files and can also create or delete files through explicit file operations. After a successful apply, `devloop` updates the Git index for affected paths.

### `ASK_HUMAN`

Prints instructions for a manual step such as `sbt compile`, `sbt test`, a focused test run, or manual verification in the configured human language.

### `DONE`

Stores session state and exits cleanly.

## LLM Command Block Format

ChatGPT must return exactly one machine-readable block:

```text
<<<DEVLOOP_COMMAND_START>>>
DEVLOOP_COMMAND_V2
VERSION: 1
COMMAND: COLLECT_CONTEXT
SUMMARY_HUMAN: I am collecting only the necessary context.
NEXT_STEP_HUMAN: Paste the new prompt into ChatGPT.
TASK_SUMMARY_EN: Fix the failing Scala compile issue.
CURRENT_GOAL_EN: Inspect the parser implementation around the reported compile error.
PROMPT_GOAL: Diagnose the compile failure and propose a minimal patch.
*** BEGIN QUERY ***
TYPE: read_snippet
FILE: src/main/scala/com/acme/Parser.scala
START_LINE: 80
END_LINE: 160
*** END QUERY ***
<<<DEVLOOP_COMMAND_END>>>
```

## Safety Restrictions

- Strict command allowlist: `COLLECT_CONTEXT`, `APPLY_PATCH`, `ASK_HUMAN`, `DONE`
- No arbitrary shell execution from the LLM
- No network access from the tool
- Repository-root sandbox for file reads and patch writes
- Structured patch validation before apply
- Refusal on unsafe or ambiguous paths
- Refusal on dirty affected files by default
- No local backup copies inside the project tree
- Manual compile and test runs only

## Patch Workflow

`APPLY_PATCH` uses one structured format, `search_replace_v1`, with Git-aware staging:

1. Validate `payload.patch_format == search_replace_v1`.
2. Validate every repository-relative path and reject `.git` internals or path escapes.
3. Reject duplicate file entries in the same patch.
4. For `replace` operations, require exact `search` text and explicit `expected_matches`.
5. For `create` operations, require explicit `content`.
6. For `delete` operations, require an explicit delete file operation instead of an implicit diff.
7. Check for dirty affected files unless `allow_apply_on_dirty_files: true`.
8. Write only the validated target files and stage them with Git.

## Scala/sbt Log Parsing

Compile parser behavior:

- groups errors by file/location,
- captures message, snippet lines, and carets when present,
- drops warning noise,
- keeps a compact error-focused summary.

Test parser behavior:

- extracts failing suites/tests,
- captures a short message,
- captures relevant stack frames,
- keeps only compact failure context for the next prompt.

## Running Tests

The unit tests use the standard library `unittest` module:

```powershell
py -3.11 -m unittest discover -s tests -v
```

## Assumptions and Notes

- Target platform is Windows 10/11.
- Python 3.11+ is required.
- Git is expected to be installed. The code also checks common Windows Git install paths if `git.exe` is not visible in `PATH`.
- Clipboard access uses PowerShell `Get-Clipboard` / `Set-Clipboard`, which is more reliable in Git Bash and other mixed Windows shell environments.
- YAML parsing and dumping are handled by a bundled lightweight compatibility layer, so the MVP has no external runtime dependencies.
- `prompt_language` uses short codes and is currently fixed to `en`.
- `human_language` uses short codes and currently supports `ru` and `en`.
- Session metadata outside the repository is treated as an explicit exception to the no-write-outside-repo rule. Repository file changes still happen only through validated patch application.
- `APPLY_PATCH` now uses a single structured patch format, `search_replace_v1`, instead of Git unified diff. This makes the LLM contract narrower and substantially more reliable for clipboard-driven workflows.
- The MVP intentionally does not implement RAG, embeddings, LSP integration, SemanticDB, or autonomous command execution.
