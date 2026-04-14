from pathlib import Path
import shutil
import unittest
import uuid

from devloop.config import DevloopConfig
from devloop.errors import RetrievalError
from devloop.retrieval import RepositoryRetriever


class RetrievalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parent / "_tmp" / str(uuid.uuid4())
        self.root.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.root.parent, ignore_errors=True)

    def _make_retriever(self) -> RepositoryRetriever:
        config = DevloopConfig(project_root=self.root)
        return RepositoryRetriever(self.root, config)

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


if __name__ == "__main__":
    unittest.main()
