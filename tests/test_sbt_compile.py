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


class SbtCompileParserTests(unittest.TestCase):
    def test_parses_compile_diagnostics(self) -> None:
        result = parse_sbt_compile_output(COMPILE_LOG)
        self.assertEqual(result.total_errors, 2)
        self.assertEqual(result.file_count, 1)
        self.assertEqual(result.diagnostics[0].line, 14)
        self.assertEqual(result.diagnostics[1].details, ["found   : Any", "required: String"])
        self.assertEqual(result.diagnostics[1].caret_line, "^")


if __name__ == "__main__":
    unittest.main()

