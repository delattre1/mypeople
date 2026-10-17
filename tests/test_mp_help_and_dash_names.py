"""`mp <verb> --help` prints usage and does nothing; no agent name starts with '-' (card d7679b9123).

mp took the first argument literally: `mp spawn --help` started a real Claude agent named --help,
`mp kill --help` retired a roster entry of that name, and `mp ensure-boss --help` ran ensure-boss.
"""
import io
from pathlib import Path
import unittest
from unittest import mock
import sandbox


ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "mypeople" / "runtime" / "bin"


class HelpAndDashNameTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mp = sandbox.load_file(BIN / "mp", "mp_cli_help")

    def test_help_on_every_verb_prints_usage_and_runs_nothing(self):
        mp = self.mp
        ran = []
        handlers = [n for n in dir(mp) if n.startswith("do_")]
        with mock.patch.multiple(mp, **{n: (lambda name: lambda argv: ran.append(name))(n) for n in handlers}):
            for verb in mp.USAGE:
                for flag in ("--help", "-h"):
                    out = io.StringIO()
                    with mock.patch.object(mp.sys, "argv", ["mp", verb, flag]), \
                            mock.patch.object(mp.sys, "stdout", out):
                        self.assertEqual(0, mp.main(), (verb, flag))
                    self.assertTrue(out.getvalue().startswith("usage: mp " + verb), (verb, out.getvalue()))
        self.assertEqual([], ran)

    def test_an_agent_name_never_starts_with_a_dash(self):
        for bad in ("--help", "-h", "main:--help", "-x/main:eng-1", "node/main:-eng"):
            with self.assertRaises(SystemExit, msg=bad):
                self.mp.full_aid(bad)
        self.assertEqual("node/main:eng-967", self.mp.full_aid("node/main:eng-967"))
        self.assertEqual("daniels-MacBook-Pro-2/main:eng-967", self.mp.full_aid("daniels-MacBook-Pro-2/main:eng-967"))


if __name__ == "__main__":
    unittest.main()
