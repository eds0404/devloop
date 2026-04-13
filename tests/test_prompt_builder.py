import unittest

from devloop.prompt_builder import PromptSection, build_context_prompt


class PromptBuilderTests(unittest.TestCase):
    def test_includes_protocol_rules_in_context_prompt(self) -> None:
        result = build_context_prompt(
            task_summary="Task",
            current_goal="Goal",
            source_label="unit test",
            human_language_name="Russian",
            sections=[PromptSection("Important", "Body", required=True)],
            max_chars=4000,
        )
        self.assertIn("Machine protocol is mandatory for every reply.", result.text)
        self.assertIn("<<<DEVLOOP_COMMAND_START>>>", result.text)
        self.assertIn("Every field nested under `payload:` must be indented by two spaces.", result.text)
        self.assertIn("read_around_match", result.text)

    def test_can_replace_full_protocol_reference_with_short_reminder(self) -> None:
        result = build_context_prompt(
            task_summary="Task",
            current_goal="Goal",
            source_label="unit test",
            human_language_name="Russian",
            sections=[PromptSection("Important", "Body", required=True)],
            max_chars=4000,
            include_protocol_reference=False,
        )
        self.assertIn("The full protocol reference is intentionally omitted", result.text)
        self.assertNotIn("read_around_match", result.text)

    def test_reports_truncation_at_section_boundaries(self) -> None:
        sections = [
            PromptSection("Important", "A" * 40, required=True),
            PromptSection("Optional One", "\n".join(["line"] * 30), compact_body="compact one"),
            PromptSection("Optional Two", "\n".join(["line"] * 30), compact_body="compact two"),
        ]
        result = build_context_prompt(
            task_summary="Task",
            current_goal="Goal",
            source_label="unit test",
            human_language_name="Russian",
            sections=sections,
            max_chars=260,
        )
        self.assertTrue(result.truncated)
        self.assertIn("Truncation report", result.text)
        self.assertTrue(result.omitted_titles or result.shortened_titles)


if __name__ == "__main__":
    unittest.main()
