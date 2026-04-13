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

    def test_resolve_repo_path_stays_inside_repo(self) -> None:
        scala_file = self.root / "src" / "main" / "scala" / "Example.scala"
        scala_file.parent.mkdir(parents=True, exist_ok=True)
        scala_file.write_text("object Example {}\n", encoding="utf-8")

        config = DevloopConfig(project_root=self.root)
        retriever = RepositoryRetriever(self.root, config)
        resolved = retriever.resolve_repo_path("src/main/scala/Example.scala")
        self.assertEqual(resolved, scala_file.resolve())

    def test_resolve_repo_path_rejects_escape(self) -> None:
        config = DevloopConfig(project_root=self.root)
        retriever = RepositoryRetriever(self.root, config)
        with self.assertRaises(RetrievalError):
            retriever.resolve_repo_path("../outside.txt")


if __name__ == "__main__":
    unittest.main()
