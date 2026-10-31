"""The voice plugin offline: captions in, one message per thought to the Boss, its own voice dropped."""
import io
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
import sandbox

PLUGIN = Path(__file__).resolve().parents[1] / "mypeople/runtime/plugins/voice/voice.py"


class Voice(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp()
        self.v = sandbox.load_file(PLUGIN, "voice_plugin", {"VOICE_STATE_DIR": self.state, "BOSS_AGENT": "h/main:Boss"})
        self.v.log = lambda msg: None

    def test_one_message_per_thought_backlog_skipped(self):
        th = self.v.Thoughts()
        th.update("old talk before the plugin started", 0.0)
        th.update("old talk before the plugin started conta uma", 1.0)
        th.update("old talk before the plugin started conta uma piada de pirata", 1.8)   # a mid-thought pause
        self.assertIsNone(th.due(2.5))
        self.assertEqual(th.due(3.4), "conta uma piada de pirata")
        self.assertIsNone(th.due(9.0))   # sent once

    def test_trimmed_buffer_never_resends(self):
        th = self.v.Thoughts()
        th.update("", 0.0)
        th.update("primeira frase que eu falei aqui na live", 1.0)
        self.assertEqual(th.due(3.0), "primeira frase que eu falei aqui na live")
        th.update("eu falei aqui na live e agora a segunda", 4.0)   # the caption server trimmed its front
        self.assertEqual(th.due(6.0), "e agora a segunda")

    def test_hard_cap_ships_a_thought_that_never_pauses(self):
        th = self.v.Thoughts()
        th.update("", 0.0)
        for i in range(1, 22):
            th.update("palavra " * i, float(i))
        self.assertTrue(th.due(21.0))

    def test_own_voice_dropped_his_words_kept(self):
        """Real 10-31 lines: the echo of a spoken reply drops; two of his lines that shared only filler words pass."""
        Path(self.state, "said.txt").write_text(
            "Ainda não. Hoje eu não consigo mandar mensagem em grupo pelo seu WhatsApp. Dá pra configurar, e você "
            "aprova o texto antes de eu enviar.\nEu tava conferindo se já tenho acesso ao seu WhatsApp.\n")
        self.assertTrue(self.v.is_echo("pelo seu WhatsApp configurar e você aprova um texto antes de eu enviar."))
        self.assertFalse(self.v.is_echo("Cara, qual que é esse agente que eu tô conversando? É melhor rotear do boss ao i"))
        self.assertFalse(self.v.is_echo("ONDER, tolhando histórico para mandar do jeito que já foi feito."))
        old = time.time() - 120
        os.utime(Path(self.state, "said.txt"), (old, old))
        self.assertFalse(self.v.is_echo("pelo seu WhatsApp configurar e você aprova um texto antes de eu enviar."))

    def test_envelope_carries_the_answer_command(self):
        sent = []
        with mock.patch.object(self.v, "send_to_agent", lambda m, agent=None: sent.append(m) or True):
            self.v.deliver("MP, conta uma piada", "Ana (studio mic)", [""])
        self.assertTrue(sent[0].startswith("[voice] from Ana (studio mic): MP, conta uma piada\n"))
        self.assertIn(f"{self.v.RUN} say \"your reply\"", sent[0])
        self.assertIn("answer only when they are talking to you", sent[0])

    def test_hung_send_returns_instead_of_raising(self):
        """10-31: one mp send that hung 30 s raised and silenced the old bridge for good."""
        with mock.patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired("mp", 30)):
            self.assertFalse(self.v.send_to_agent("hi"))

    def test_say_remembers_the_line_then_speaks_it(self):
        Path(self.state, "config.json").write_text('{"sse": "h:1", "say": ["speak", "--voice", "x"]}')
        with mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
            for n in range(4):
                self.assertEqual(self.v.say(f"linha {n}"), 0)
        run.assert_called_with(["speak", "--voice", "x", "linha 3"])
        self.assertEqual(Path(self.state, "said.txt").read_text().splitlines(), ["linha 1", "linha 2", "linha 3"])

    def test_bare_run_is_usage_not_a_second_serve(self):
        with mock.patch("sys.stderr", new=io.StringIO()):
            self.assertEqual(self.v.main([]), 1)


if __name__ == "__main__":
    unittest.main()
