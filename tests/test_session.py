import os
from pathlib import Path
import shutil
import unittest
import uuid

from devloop.cli import _build_arg_parser, _load_session_for_run
from devloop.errors import SessionError
from devloop.session import SessionStore
from devloop import yaml_compat as yaml


class SessionTests(unittest.TestCase):
    def _make_test_root(self) -> Path:
        root = Path(__file__).resolve().parent / "_tmp" / str(uuid.uuid4())
        root.mkdir(parents=True, exist_ok=True)
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        return root

    def test_load_or_create_reports_session_path_on_parse_error(self) -> None:
        temp_dir = self._make_test_root()
        repo_root = temp_dir / "repo"
        repo_root.mkdir()
        localappdata = temp_dir / "localappdata"
        localappdata.mkdir()
        previous = os.environ.get("LOCALAPPDATA")
        os.environ["LOCALAPPDATA"] = str(localappdata)
        try:
            store = SessionStore(repo_root, "localappdata")
            store.session_path.write_text("last_generated_prompt: [", encoding="utf-8")
            with self.assertRaises(SessionError) as context:
                store.load_or_create()
            self.assertIn(str(store.session_path), str(context.exception))
        finally:
            if previous is None:
                os.environ.pop("LOCALAPPDATA", None)
            else:
                os.environ["LOCALAPPDATA"] = previous

    def test_force_bootstrap_recovers_from_broken_session(self) -> None:
        temp_dir = self._make_test_root()
        repo_root = temp_dir / "repo"
        repo_root.mkdir()
        localappdata = temp_dir / "localappdata"
        localappdata.mkdir()
        previous = os.environ.get("LOCALAPPDATA")
        os.environ["LOCALAPPDATA"] = str(localappdata)
        try:
            store = SessionStore(repo_root, "localappdata")
            store.session_path.write_text("last_generated_prompt: [", encoding="utf-8")
            session, recovered = _load_session_for_run(
                store,
                force_bootstrap=True,
                reset_session=False,
            )
            self.assertTrue(recovered)
            self.assertEqual(session.repo_root, str(repo_root.resolve()))
            self.assertFalse(session.initialized)
        finally:
            if previous is None:
                os.environ.pop("LOCALAPPDATA", None)
            else:
                os.environ["LOCALAPPDATA"] = previous

    def test_parser_accepts_reset_session_flag(self) -> None:
        args = _build_arg_parser().parse_args(["--config", "C:\\repo\\devloop.yaml", "--reset-session"])
        self.assertTrue(args.reset_session)

    def test_yaml_parser_accepts_block_empty_dict_and_list(self) -> None:
        text = (
            "last_parsed_llm_response:\n"
            "  {}\n"
            "command_history_summary:\n"
            "  []\n"
        )
        parsed = yaml.safe_load(text)
        self.assertEqual(parsed["last_parsed_llm_response"], {})
        self.assertEqual(parsed["command_history_summary"], [])


if __name__ == "__main__":
    unittest.main()
