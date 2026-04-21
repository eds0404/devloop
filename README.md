# devloop

`devloop` is a Windows-first local human-in-the-loop assistant for Scala/sbt repositories.
It is a clipboard-driven loop between a human, ChatGPT, and a local repository.

## Quick Start

Install in editable mode:

```powershell
py -3.11 -m pip install -e .
```

Create a starter config:

```powershell
python -m devloop --print-default-config > devloop.yaml
```

Run the loop:

```powershell
python -m devloop --config C:\path\to\devloop.yaml
```

Normal flow:

1. On the first run for a repository, `devloop` puts a bootstrap prompt into the clipboard.
2. Paste that prompt into ChatGPT together with the task.
3. Copy the full ChatGPT reply back to the clipboard.
4. Run the same `python -m devloop --config ...` command again.
5. `devloop` auto-detects clipboard content and either:
   - executes an LLM command,
   - turns sbt compile output into a new prompt,
   - turns sbt test output into a new prompt,
   - wraps plain text into a new prompt.

Troubleshooting flags:

- `--force-bootstrap` regenerates only the bootstrap prompt.
- `--force-mode llm|compile|test|raw` overrides clipboard auto-detection for one run.
- `--reset-session` recreates the local session metadata for the current target repository.
- `--version` prints the package version.

## Configuration

Example:

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

Key fields:

- `project_root`: required target Git repository.
- `include_project_summary_in_prompts`: off by default. Only affects automatically generated follow-up prompts. An explicit `project_tree` request from the LLM still returns the full summary.
- `allow_apply_on_dirty_files`: off by default. When `false`, `APPLY_PATCH` refuses to touch dirty affected files.
- `prompt_language`: currently fixed to `en`.
- `human_language`: currently `ru` or `en`.
- `state_dir_mode`: currently only `localappdata`.

Session metadata is stored outside the repository under `%LOCALAPPDATA%\devloop\repos\<hash>\session.yaml`.

## Run Log

Each run with `--config ...` appends one flat-text entry to `.devloop.log` next to the config file.

Base fields in every entry:

- current `devloop` Git HEAD
- CLI arguments
- clipboard text before processing
- raw config file text
- console output
- clipboard text written by `devloop`

Extra fields for `APPLY_PATCH` runs:

- extracted machine block
- protocol parse mode: `v2_strict` or `v2_relaxed`
- normalized patch payload
- patch id
- target repository HEAD and status before and after the attempt
- exact failure stage and failure reason
- source windows used for repair prompts
- per-file sha256 values before and after apply
- per-replacement expected match count, found match count, and matched line numbers
- resulting diff for affected paths
- fallback usage, if any

The log is append-only and uses plain text sections with explicit begin/end markers.

If `devloop.yaml` lives inside the target repository, add `.devloop.log` to that repository's `.gitignore`.

## Clipboard Detection

`devloop` inspects clipboard content in this order:

1. an LLM response with `<<<DEVLOOP_COMMAND_START>>>` and `<<<DEVLOOP_COMMAND_END>>>`
2. sbt compile output
3. sbt test output
4. plain text

All prompts generated for ChatGPT are English prompts.
Console output follows `human_language`.

The bootstrap prompt always contains the full protocol reference.
Follow-up prompts alternate between a full protocol reference and a shorter reminder.

## LLM Protocol

Only one external protocol is supported:

- line-based `DEVLOOP_COMMAND_V2`
- exactly one command block per reply
- YAML command blocks are not accepted

Required block markers:

```text
<<<DEVLOOP_COMMAND_START>>>
...
<<<DEVLOOP_COMMAND_END>>>
```

Required top-level fields inside the block:

- `VERSION`
- `COMMAND`
- `SUMMARY_HUMAN`
- `NEXT_STEP_HUMAN`
- `TASK_SUMMARY_EN`
- `CURRENT_GOAL_EN`

Supported commands:

- `COLLECT_CONTEXT`
- `APPLY_PATCH`
- `ASK_HUMAN`
- `DONE`

Response discipline:

- Put human-facing text into `SUMMARY_HUMAN` and `NEXT_STEP_HUMAN`.
- Do not add prose outside the command block.
- Do not emit a second draft or a repeated copy of the block.

### `COLLECT_CONTEXT`

Use this to ask `devloop` for deterministic repository context.

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

`project_tree` may optionally include `PATH: <directory>` to request one subtree.
Project-tree and search-style retrieval operate on Git-visible files (`tracked + untracked, excluding ignored`), not arbitrary build output on disk.

### `APPLY_PATCH`

Use this to request exact file edits.

External protocol format:

- `PATCH_FORMAT: SEARCH_REPLACE_BLOCKS_V1`
- one `*** BEGIN FILE ***` / `*** END FILE ***` section per file
- operations:
  - `OP: REPLACE`
  - `OP: CREATE_FILE`
  - `OP: DELETE_FILE`

For `REPLACE`:

- `PATH` is required
- `MATCH_COUNT` is required for each replacement block
- `@@@SEARCH@@@`
- `@@@REPLACE@@@`
- `@@@END@@@`

For `CREATE_FILE`:

- `PATH` is required
- `@@@CONTENT@@@`
- `@@@END@@@`

For `DELETE_FILE`:

- `PATH` is required
- optional `EXPECTED_SHA256`

Minimal example:

```text
<<<DEVLOOP_COMMAND_START>>>
DEVLOOP_COMMAND_V2
VERSION: 1
COMMAND: APPLY_PATCH
SUMMARY_HUMAN: Apply the exact patch.
NEXT_STEP_HUMAN: Run compile.
TASK_SUMMARY_EN: Replace the old import.
CURRENT_GOAL_EN: Apply one exact replacement.
PATCH_FORMAT: SEARCH_REPLACE_BLOCKS_V1
*** BEGIN FILE ***
PATH: src/main/scala/com/acme/Parser.scala
OP: REPLACE
MATCH_COUNT: 1
@@@SEARCH@@@
import play.api.libs.json.Json
@@@REPLACE@@@
import io.circe.syntax._
@@@END@@@
*** END FILE ***
<<<DEVLOOP_COMMAND_END>>>
```

### `ASK_HUMAN`

Use this when a manual step is required, for example:

- `sbt compile`
- `sbt test`
- a focused sbt command
- manual verification
- a domain clarification from the human

### `DONE`

Use this when no further local tool action is needed.

## Safety Model

- LLM output is limited to `COLLECT_CONTEXT`, `APPLY_PATCH`, `ASK_HUMAN`, and `DONE`.
- Patch paths must stay repository-relative and may not touch `.git`.
- Duplicate file entries in one patch are rejected.
- Replace operations use exact search text and exact match counts.
- Dirty affected files are rejected by default.
- Build and test commands are run manually by the human, not by the LLM.

## sbt Parsing

Compile parsing focuses on:

- grouped diagnostics by file and location
- source snippets and carets when present
- warning counts
- compact prompts for the next LLM turn

Test parsing focuses on:

- failed suites and test cases
- short failure messages
- relevant stack frames

## Running Tests

```powershell
py -3.11 -m unittest discover -s tests -v
```

## Environment

- Windows 10 or 11
- Python 3.11+
- Git available in `PATH` or in common Windows install locations
- PowerShell clipboard access via `Get-Clipboard` and `Set-Clipboard`
