from pathlib import Path
import unittest

from devloop.config import DevloopConfig, default_config_text


class ConfigTests(unittest.TestCase):
    def test_normalizes_language_aliases(self) -> None:
        config = DevloopConfig(
            project_root=Path(__file__).resolve().parents[1],
            prompt_language="English",
            human_language="Russian",
        )
        self.assertEqual(config.prompt_language, "en")
        self.assertEqual(config.human_language, "ru")

    def test_supports_english_human_language(self) -> None:
        config = DevloopConfig(
            project_root=Path(__file__).resolve().parents[1],
            prompt_language="en",
            human_language="en",
        )
        self.assertEqual(config.human_language_name, "English")

    def test_default_config_uses_short_codes(self) -> None:
        text = default_config_text()
        self.assertIn("prompt_language: en", text)
        self.assertIn("human_language: ru", text)
        self.assertIn("include_project_summary_in_prompts: false", text)

    def test_project_summary_is_disabled_by_default(self) -> None:
        config = DevloopConfig(project_root=Path(__file__).resolve().parents[1])
        self.assertFalse(config.include_project_summary_in_prompts)


if __name__ == "__main__":
    unittest.main()
