from pathlib import Path
import shutil
import subprocess
import unittest
import uuid

from devloop.config import DevloopConfig
from devloop.errors import RetrievalError
from devloop.parsers.sbt_compile import CompileParseResult
from devloop.retrieval import RepositoryRetriever


class RetrievalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parent / "_tmp" / str(uuid.uuid4())
        self.root.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _make_retriever(self) -> RepositoryRetriever:
        config = DevloopConfig(project_root=self.root)
        return RepositoryRetriever(self.root, config)

    def _init_git_repo(self) -> None:
        subprocess.run(["git", "init"], cwd=self.root, check=True, capture_output=True, text=True)
        subprocess.run(["git", "config", "user.email", "devloop@example.com"], cwd=self.root, check=True, capture_output=True, text=True)
        subprocess.run(["git", "config", "user.name", "devloop"], cwd=self.root, check=True, capture_output=True, text=True)

    def test_resolve_repo_path_stays_inside_repo(self) -> None:
        scala_file = self.root / "src" / "main" / "scala" / "Example.scala"
        scala_file.parent.mkdir(parents=True, exist_ok=True)
        scala_file.write_text("object Example {}\n", encoding="utf-8")

        retriever = self._make_retriever()
        resolved = retriever.resolve_repo_path("src/main/scala/Example.scala")
        self.assertEqual(resolved, scala_file.resolve())

    def test_resolve_repo_path_rejects_escape(self) -> None:
        retriever = self._make_retriever()
        with self.assertRaises(RetrievalError):
            retriever.resolve_repo_path("../outside.txt")

    def test_execute_queries_read_around_match_returns_context(self) -> None:
        scala_file = self.root / "src" / "main" / "scala" / "Example.scala"
        scala_file.parent.mkdir(parents=True, exist_ok=True)
        scala_file.write_text(
            "object Example {\n"
            "  val before = 1\n"
            "  val marker = 2\n"
            "  val after = 3\n"
            "}\n",
            encoding="utf-8",
        )

        retriever = self._make_retriever()
        results = retriever.execute_queries(
            [
                {
                    "type": "read_around_match",
                    "query": "marker",
                    "glob": "**/*.scala",
                    "before": 1,
                    "after": 1,
                    "limit": 1,
                }
            ]
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].query_type, "read_around_match")
        self.assertIn("File: src/main/scala/Example.scala", results[0].body)
        self.assertIn("val before = 1", results[0].body)
        self.assertIn("val marker = 2", results[0].body)
        self.assertIn("val after = 3", results[0].body)

    def test_execute_queries_rejects_invalid_regex(self) -> None:
        scala_file = self.root / "src" / "main" / "scala" / "Example.scala"
        scala_file.parent.mkdir(parents=True, exist_ok=True)
        scala_file.write_text("object Example {}\n", encoding="utf-8")

        retriever = self._make_retriever()
        with self.assertRaises(RetrievalError) as context:
            retriever.execute_queries([{"type": "regex_search", "query": "("}])
        self.assertIn("Invalid regex query", str(context.exception))

    def test_project_tree_summary_honors_default_excludes(self) -> None:
        included = self.root / "src" / "main" / "scala" / "App.scala"
        included.parent.mkdir(parents=True, exist_ok=True)
        included.write_text("object App {}\n", encoding="utf-8")
        excluded = self.root / "target" / "generated.txt"
        excluded.parent.mkdir(parents=True, exist_ok=True)
        excluded.write_text("generated\n", encoding="utf-8")

        retriever = self._make_retriever()
        summary = retriever.project_tree_summary()
        self.assertIn("src/main/scala/App.scala", summary)
        self.assertNotIn("target/generated.txt", summary)

    def test_project_tree_summary_does_not_truncate_to_max_files(self) -> None:
        config = DevloopConfig(project_root=self.root, max_files=2)
        for index in range(5):
            path = self.root / "src" / "main" / "scala" / f"File{index}.scala"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"object File{index} {{}}\n", encoding="utf-8")

        retriever = RepositoryRetriever(self.root, config)
        summary = retriever.project_tree_summary()

        self.assertIn("src/main/scala/File0.scala", summary)
        self.assertIn("src/main/scala/File4.scala", summary)
        self.assertNotIn("omitted", summary)

    def test_project_tree_summary_supports_subtree_path(self) -> None:
        first = self.root / "core" / "src" / "main" / "scala" / "Core.scala"
        first.parent.mkdir(parents=True, exist_ok=True)
        first.write_text("object Core {}\n", encoding="utf-8")
        second = self.root / "registry-writer" / "src" / "main" / "scala" / "Main.scala"
        second.parent.mkdir(parents=True, exist_ok=True)
        second.write_text("object Main {}\n", encoding="utf-8")

        retriever = self._make_retriever()
        summary = retriever.project_tree_summary("core/src")

        self.assertIn("core/src/main/scala/Core.scala", summary)
        self.assertNotIn("registry-writer/src/main/scala/Main.scala", summary)

    def test_project_tree_summary_uses_git_visible_files_not_ignored_output(self) -> None:
        self._init_git_repo()
        tracked = self.root / "src" / "main" / "scala" / "App.scala"
        tracked.parent.mkdir(parents=True, exist_ok=True)
        tracked.write_text("object App {}\n", encoding="utf-8")
        ignored = self.root / "target" / "generated.txt"
        ignored.parent.mkdir(parents=True, exist_ok=True)
        ignored.write_text("generated\n", encoding="utf-8")
        (self.root / ".gitignore").write_text("target/\n", encoding="utf-8")
        subprocess.run(["git", "add", ".gitignore", "src/main/scala/App.scala"], cwd=self.root, check=True, capture_output=True, text=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=self.root, check=True, capture_output=True, text=True)

        retriever = self._make_retriever()
        summary = retriever.project_tree_summary()

        self.assertIn("src/main/scala/App.scala", summary)
        self.assertNotIn("target/generated.txt", summary)

    def test_build_compile_query_results_reports_success_without_errors(self) -> None:
        retriever = self._make_retriever()
        parsed = CompileParseResult(
            diagnostics=[],
            total_errors=0,
            file_count=0,
            raw_error_lines=0,
            raw_warning_lines=2,
            succeeded=True,
        )

        results = retriever.build_compile_query_results(parsed)
        self.assertEqual(results[0].query_type, "compile_summary")
        self.assertIn("Compile succeeded: yes", results[0].body)
        self.assertIn("Raw [warn] lines seen: 2", results[0].body)
        self.assertEqual(results[1].query_type, "compile_details")
        self.assertIn("completed successfully", results[1].body)


if __name__ == "__main__":
    unittest.main()
