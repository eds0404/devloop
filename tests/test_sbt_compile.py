import unittest

from devloop.parsers.sbt_compile import parse_sbt_compile_output


COMPILE_LOG = """
[warn] something irrelevant
[error] C:\\repo\\core\\src\\main\\scala\\com\\acme\\OfficialDayEndFetcher.scala:14:8: not found: object play
[error] import play.api.libs.json._
[error]        ^
[error] C:\\repo\\core\\src\\main\\scala\\com\\acme\\OfficialDayEndFetcher.scala:296:51: type mismatch;
[error]  found   : Any
[error]  required: String
[error]               parent ! TrackCriminal(i.id, i.ric, knownReason)
[error]                                                   ^
[error] (core / Compile / compileIncremental) Compilation failed
""".strip()

SUCCESS_LOG = """
[info] scalafmt: Formatting 1 Scala sources (C:\\repo\\core)...
[info] compiling 6 Scala sources to C:\\repo\\core\\target\\scala-2.13\\classes ...
[warn] C:\\repo\\core\\src\\main\\scala\\com\\acme\\Parser.scala:10:5: discarded non-Unit value
[warn]   parser.run()
[warn]   ^
[info] done compiling
[success] Total time: 12 s, completed Apr 13, 2026, 4:09:45 PM
""".strip()


class SbtCompileParserTests(unittest.TestCase):
    def test_parses_compile_diagnostics(self) -> None:
        result = parse_sbt_compile_output(COMPILE_LOG)
        self.assertEqual(result.total_errors, 2)
        self.assertEqual(result.file_count, 1)
        self.assertEqual(result.diagnostics[0].line, 14)
        self.assertEqual(result.diagnostics[1].details, ["found   : Any", "required: String"])
        self.assertEqual(result.diagnostics[1].caret_line, "^")
        self.assertFalse(result.succeeded)
        self.assertEqual(result.raw_warning_lines, 1)

    def test_tracks_successful_compile_and_warning_lines(self) -> None:
        result = parse_sbt_compile_output(SUCCESS_LOG)
        self.assertTrue(result.succeeded)
        self.assertEqual(result.total_errors, 0)
        self.assertEqual(result.file_count, 0)
        self.assertEqual(result.raw_warning_lines, 3)


if __name__ == "__main__":
    unittest.main()

