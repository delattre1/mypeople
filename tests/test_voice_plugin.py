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
        self.assertIsNone(th.due(3.4))   # no sentence end yet: 1.6 s is a pause, not the end (11-01)
        self.assertEqual(th.due(5.4), "conta uma piada de pirata")
        self.assertIsNone(th.due(12.0))   # sent once

    def test_trimmed_buffer_never_resends(self):
        th = self.v.Thoughts()
        th.update("", 0.0)
        th.update("primeira frase que eu falei aqui na live.", 1.0)
        self.assertEqual(th.due(3.0), "primeira frase que eu falei aqui na live.")
        th.update("eu falei aqui na live. e agora a segunda", 4.0)   # the caption server trimmed its front
        self.assertEqual(th.due(8.0), "e agora a segunda")

    def test_hard_cap_ships_a_thought_that_never_pauses(self):
        th = self.v.Thoughts()
        th.update("", 0.0)
        for i in range(1, 32):
            th.update("palavra " * i, float(i))
        self.assertTrue(th.due(31.0))

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

    def test_garbled_echo_dropped(self):
        """10-31: this echo matched 10 of 21 word pairs and reached the Boss as his speech."""
        Path(self.state, "said.txt").write_text("Beleza, a gente liga o WebSocket do OBS pra você agora. E no AbacatePay: "
                                                "posso eu mesmo ativar o 2FA e guardar o segredo no cofre dos agentes?\n")
        self.assertTrue(self.v.is_echo("pra você agora e numa batate pari possui o mesmo ativar o dois FA e guardar "
                                       "o segredo no órgão dos agentes"))

    def test_what_arrives_while_speaking_is_skipped_his_earlier_words_kept(self):
        th = self.v.Thoughts()
        th.update("", 0.0)
        th.update("ei Boss liga o OBS", 1.0)                                           # his, not yet sent
        th.update("ei Boss liga o OBS beleza a gente liga o WebSocket", 2.0, muted=True)  # the speaker's echo
        th.update("ei Boss liga o OBS beleza a gente liga o WebSocket do OBS agora", 3.0, muted=True)
        self.assertEqual(th.due(4.0), "ei Boss liga o OBS")
        th.update("ei Boss liga o OBS beleza a gente liga o WebSocket do OBS agora valeu", 8.0)
        self.assertEqual(th.due(12.0), "valeu")

    def test_long_utterance_with_a_pause_ships_whole_never_mid_word(self):
        """11-01 real lines: a 1.5 s pause shipped 'me', then 'lhor entendi ...' (= 'melhor'), and a comma the
        model added later shipped as its own line. Daniel: send the whole utterance."""
        th = self.v.Thoughts()
        th.update("", 0.0)
        th.update("eu ligo faço a ligação se eu ficar me", 1.0)          # he pauses mid-word
        self.assertIsNone(th.due(3.0))
        th.update("eu ligo faço a ligação se eu ficar melhor entendi o que você quiser comprar.", 3.2)
        self.assertEqual(th.due(5.0), "eu ligo faço a ligação se eu ficar melhor entendi o que você quiser comprar.")
        th.update("eu ligo faço a ligação, se eu ficar melhor, entendi o que você quiser comprar.", 6.0)  # commas only
        self.assertIsNone(th.due(12.0))
        th = self.v.Thoughts()
        th.update("", 0.0)
        th.update("se eu ficar me", 1.0)
        self.assertEqual(th.due(5.0), "se eu ficar me")                   # a long pause does ship it
        th.update("se eu ficar melhor entendi", 6.0)
        self.assertEqual(th.due(10.0), "melhor entendi")                  # the next line starts with the whole word

    def test_words_before_the_speaker_ship_at_once_and_skip_the_echo_filter(self):
        """11-01: held words used to ship glued to the speaker's echo and were dropped with it; and his line was
        dropped as 'echo' because the Boss's reply repeated his words."""
        th = self.v.Thoughts()
        th.update("", 0.0)
        th.update("a gente precisa de um agente de criativos", 1.0)
        th.update("a gente precisa de um agente de criativos combinado vou montar o agente de criativos", 2.0, muted=True)
        self.assertEqual(th.due(2.1), "a gente precisa de um agente de criativos")
        self.assertTrue(th.before_speaker)
        Path(self.state, "said.txt").write_text("Combinado, vou montar o agente de criativos que a gente precisa.\n")
        sent = []
        with mock.patch.object(self.v, "send_to_agent", lambda m, agent=None: sent.append(m) or True):
            self.v.deliver("a gente precisa de um agente de criativos", "Daniel", [""], before_speaker=True)
        self.assertEqual(len(sent), 1)

    def test_speaking_window(self):
        Path(self.state, "speaking").write_text("100.0 ")                    # still playing
        self.assertTrue(self.v.speaking(150.0))
        self.assertFalse(self.v.speaking(200.0))                             # a `say` that died stops muting
        Path(self.state, "speaking").write_text("100.0 110.0")
        self.assertTrue(self.v.speaking(112.0))
        self.assertFalse(self.v.speaking(113.0))
        self.assertFalse(self.v.speaking(99.0))

    def test_envelope_carries_the_answer_command(self):
        sent = []
        with mock.patch.object(self.v, "send_to_agent", lambda m, agent=None: sent.append(m) or True):
            self.v.deliver("MP, conta uma piada", "Ana (studio mic)", [""])
        self.assertTrue(sent[0].startswith("[voice] from Ana (studio mic): MP, conta uma piada\n"))
        self.assertIn(f"{self.v.RUN} say \"your reply\"", sent[0])
        self.assertIn("Answer only when they are talking to you", sent[0])

    def test_cue_tells_the_boss_how_it_is_addressed(self):
        note = self.v.envelope("Ana (studio mic)", "oi", cue='"hey Boss"')
        self.assertIn('They address you by saying "hey Boss"', note)
        self.assertIn("a line without that is them talking to someone else, so do nothing", note)

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
        start, end = map(float, Path(self.state, "speaking").read_text().split())
        self.assertLessEqual(start, end)
        self.assertEqual(Path(self.state, "said.txt").read_text().splitlines(), ["linha 1", "linha 2", "linha 3"])

    def test_bare_run_is_usage_not_a_second_serve(self):
        with mock.patch("sys.stderr", new=io.StringIO()):
            self.assertEqual(self.v.main([]), 1)


if __name__ == "__main__":
    unittest.main()
