"""`mp tcc`: one line per macOS App Data request, and the day's counts (card 1c417d9f90).

Pins that tccd's log lines are correlated by msgID into one request, that only App Data requests
count, that a prompt is told apart from a silent answer, and that the parent chain stops at the
agent that started the process.
"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import sandbox


ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "mypeople" / "runtime" / "bin"


def ev(ts, msg):
    return json.dumps({"timestamp": "2026-10-23 %s.000000-0300" % ts, "eventMessage": msg}) + "\n"


def request(mid, ts, pid, service="kTCCServiceSystemPolicyAppData", prompt=True, value=2):
    lines = [ev(ts, "AUTHREQ_CTX: msgID=%s, function=TCCAccessRequest, service=%s, preflight=no" % (mid, service)),
             ev(ts, "AUTHREQ_ATTRIBUTION: msgID=%s, attribution={responsible={TCCDProcess: identifier=co.plow.mypeople, "
                    "pid=%d, binary_path=/bin/cp}, accessing={TCCDProcess: identifier=cp, pid=%d, auid=501, "
                    "binary_path=/bin/cp}, requesting={TCCDProcess: identifier=com.apple.sandboxd, pid=1}, }," % (mid, pid, pid))]
    if prompt:
        lines.append(ev(ts, "AUTHREQ_PROMPTING: msgID=%s, service=%s, subject=Sub:{co.plow.mypeople}" % (mid, service)))
    return lines, ev(ts, "AUTHREQ_RESULT: msgID=%s, authValue=%d, authReason=2, authVersion=1," % (mid, value))


class TccTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.mp = sandbox.load(cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_requests_lines_and_counts(self):
        mp = self.mp
        a, a_end = request("8347.2", "14:04:24", 100)
        b, b_end = request("8347.4", "14:04:25", 200, prompt=False, value=0)
        other, other_end = request("370.9", "14:04:25", 300, service="kTCCServiceAddressBook")
        # Interleaved, plus the header `log stream` prints first.
        stream = ["Filtering the log data using ...\n"] + a + b + other + [other_end, b_end, a_end]
        snaps = {100: {"chain": "cp <- zsh <- claude", "agent": "test-node/main:eng-1",
                       "container": "group.com.example.shared"},
                 200: {}}  # exited before it was read
        reqs = list(mp.tcc_requests(stream, snaps.__getitem__))
        self.assertEqual([r["pid"] for r in reqs], [200, 100])
        lines = [mp.tcc_line(r) for r in reqs]
        self.assertEqual(lines[1].split("\t")[1:],
                         ["test-node/main:eng-1", "group.com.example.shared", "prompt",
                          "allowed", "cp <- zsh <- claude\n"])
        self.assertEqual(lines[0].split("\t")[1:], ["-", "?", "no-prompt", "denied", "cp (exited)\n"])
        self.assertTrue(lines[1].startswith("2026-10-23 14:04:24 "))
        self.assertEqual(mp.tcc_counts(lines + lines[1:], "2026-10-23"),
                         [(("test-node/main:eng-1", "group.com.example.shared"), [2, 2]),
                          (("-", "?"), [0, 1])])
        self.assertEqual(mp.tcc_counts(lines, "2026-10-22"), [])

    def test_snapshot_chain_stops_at_the_agent(self):
        mp = self.mp
        env = " PATH=/bin AGENT_ID=test-node/main:eng-1"
        args = {10: "cp -R /Users/x/Library/Group Containers/group.example.shared/db /tmp/y",
                11: "/bin/zsh -c eval 'cp -R ~/Library/Group\\ Containers/group.example.shared/db /tmp/y'",
                12: "claude --model x", 13: "-zsh", 14: "tmux"}
        tables = {"pid=,ppid=": {10: "11", 11: "12", 12: "13", 13: "14", 14: "1"},
                  "args": args,
                  "env": {p: a + (env if p <= 12 else "") for p, a in args.items()}}

        def fake_ps(*a):
            return tables["env" if "-E" in a else "args" if "pid=,args=" in a else "pid=,ppid="]
        with mock.patch.object(mp, "_ps", fake_ps):
            got = mp.tcc_snapshot(10)
        self.assertEqual(got, {"chain": "cp <- zsh <- claude", "agent": "test-node/main:eng-1",
                               "container": "group.example.shared"})
        self.assertEqual(mp.TCC_CONTAINER.search(args[11]).group(1), "group.example.shared")


if __name__ == "__main__":
    unittest.main()
