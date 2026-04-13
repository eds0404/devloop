import unittest

from devloop.parsers.sbt_test import parse_sbt_test_output


TEST_LOG = """
[info] EnablingInstrumentEventsStreamTest:
[info] - when emission state enabled *** FAILED ***
[info]   scala.UninitializedFieldError: Uninitialized field: EnablingInstrumentsEventsStream.scala: 34
[info]   at com.example.platform.stream.instrument.EnablingInstrumentsEventsStream.schemaRegistryUrl(EnablingInstrumentsEventsStream.scala:34)
[info]   at com.example.platform.stream.instrument.EnablingInstrumentEventsStreamTest.$anonfun$new$3(EnablingInstrumentEventsStreamTest.scala:24)
[info] - when emission state disabled *** FAILED ***
[info]   scala.UninitializedFieldError: Uninitialized field: EnablingInstrumentsEventsStream.scala: 34
[info]   at com.example.platform.stream.instrument.EnablingInstrumentsEventsStream.schemaRegistryUrl(EnablingInstrumentsEventsStream.scala:34)
""".strip()


class SbtTestParserTests(unittest.TestCase):
    def test_parses_failing_tests(self) -> None:
        result = parse_sbt_test_output(TEST_LOG)
        self.assertEqual(result.total_failures, 2)
        self.assertEqual(result.failures[0].suite_name, "EnablingInstrumentEventsStreamTest")
        self.assertEqual(result.failures[0].test_name, "when emission state enabled")
        self.assertIn("Uninitialized field", result.failures[0].message)
        self.assertEqual(result.failures[0].stack_frames[0].line, 34)


if __name__ == "__main__":
    unittest.main()
