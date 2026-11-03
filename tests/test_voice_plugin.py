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

    def test_mic_stays_open_while_speaking_his_words_kept_speaker_stripped(self):
        """11-01 real case: Daniel tested the mic while the Boss spoke; the old mute dropped 'Vamos testar então se
        você recebe tudo'. Now his words stay and only the speaker's (said.txt) are stripped at send."""
        th = self.v.Thoughts()
        th.update("", 0.0)
        th.update("ei Boss liga o OBS", 1.0)                                                   # his, not yet sent
        th.update("ei Boss liga o OBS o Madu agora suas frases chegam inteiras", 2.0, muted=True)  # speaker starts
        self.assertEqual(th.due(2.1), "ei Boss liga o OBS")                                    # his, at once
        self.assertTrue(th.before_speaker)
        th.update("ei Boss liga o OBS o Madu agora suas frases chegam inteiras, pode testar. Vamos testar então se "
                  "você recebe tudo", 4.0, muted=True)
        th.in_speaker = False
        th.update("ei Boss liga o OBS o Madu agora suas frases chegam inteiras, pode testar. Vamos testar então se "
                  "você recebe tudo que eu falar.", 5.0)
        line = th.due(7.0)
        self.assertTrue(th.was_overlap)
        Path(self.state, "said.txt").write_text("Arrumado: agora suas frases chegam inteiras. Pode testar.\n")
        sent = []
        with mock.patch.object(self.v, "send_to_agent", lambda m, agent=None: sent.append(m) or True):
            self.v.deliver(line, "Daniel", [""], overlap=True)
        self.assertEqual(len(sent), 1)
        self.assertIn("Vamos testar então se você recebe tudo que eu falar.", sent[0])
        self.assertNotIn("frases chegam inteiras", sent[0])

    def test_strip_echo_pure_echo_is_dropped(self):
        Path(self.state, "said.txt").write_text("A máquina de criativos já começou: cinco anúncios pra cada produto.\n")
        self.assertEqual(self.v.strip_echo("a máquina de criativos já começou cinco anúncios pra cada produto"), "")
        self.assertEqual(self.v.strip_echo("quanto custa isso"), "quanto custa isso")
        sent = []
        with mock.patch.object(self.v, "send_to_agent", lambda m, agent=None: sent.append(m) or True):
            self.v.deliver("a máquina de criativos já começou cinco anúncios", "Daniel", [""], overlap=True)
        self.assertEqual(sent, [])

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

    def test_envelope_says_a_line_addressed_to_the_boss_is_a_task(self):
        note = self.v.envelope("Ana (studio mic)", "oi", cue='"hey Boss"')
        self.assertIn(f"so do nothing. {self.v.OWNER_RULE} To answer out loud", note)
        self.assertIn(f"otherwise do nothing. {self.v.OWNER_RULE} To answer out loud", self.v.envelope("Ana", "oi"))

    def test_off_keeps_the_whole_line_in_the_log_and_sends_nothing(self):
        sent, logged = [], []
        line = "uma linha com mais de oitenta caracteres para provar que o log guarda a frase inteira ate o fim"
        self.v.OFF.touch()
        with mock.patch.object(self.v, "send_to_agent", lambda m, agent=None: sent.append(m) or True), \
                mock.patch.object(self.v, "log", logged.append):
            self.v.deliver(line, "Ana", [""])
        self.assertEqual(sent, [])
        self.assertEqual(logged, [f"paused, not sent: {line!r}"])
        self.v.OFF.unlink()
        with mock.patch.object(self.v, "send_to_agent", lambda m, agent=None: sent.append(m) or True), \
                mock.patch.object(self.v, "log", logged.append):
            self.v.deliver("de volta", "Ana", [""])
        self.assertIn("de volta", sent[0])

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

    def _speak(self, th, words, start, step=0.3):
        """Words arriving one at a time; a tuple is a pause before that word. Returns the time after the last."""
        now, text = start, th.text
        for w in words:
            if isinstance(w, tuple):
                now += w[1]
                w = w[0]
            text = (text + " " + w).strip()
            now += step
            th.update(text, now)
        return now

    def test_cap_cuts_at_his_longest_pause_and_loses_nothing(self):
        th = self.v.Thoughts()
        th.update("", 0.0)
        said = [f"um{i}" for i in range(30)] + [("depois", 2.0)] + [f"dois{i}" for i in range(70)]
        now = self._speak(th, said, 0.0)
        self.assertGreater(now, self.v.HARD_MS / 1000)
        line = th.due(now)
        self.assertEqual(line, " ".join(f"um{i}" for i in range(30)), "the line ends where he paused")
        self.assertFalse(th.due(now + 0.1))
        rest = th.due(now + 10)
        self.assertTrue(rest.startswith("depois dois0"), rest)
        self.assertEqual(f"{line} {rest}".split(), [w[0] if isinstance(w, tuple) else w for w in said], "every word once, in order")

    def test_cap_with_no_pause_ships_everything_as_before(self):
        th = self.v.Thoughts()
        th.update("", 0.0)
        said = [f"w{i}" for i in range(120)]
        now = self._speak(th, said, 0.0)
        self.assertEqual(th.due(now).split(), said)

    def test_cap_ignores_a_pause_inside_a_word_and_a_stub_at_the_start(self):
        th = self.v.Thoughts()
        th.update("", 0.0)
        now = self._speak(th, ["so", ("this", 5.0)] + [f"w{i}" for i in range(50)], 0.0)
        th.update(th.text + " me", now)            # the model is mid-word: "me" ... "lhor"
        th.update(th.text + "lhor", now + 3.0)
        now = self._speak(th, [f"x{i}" for i in range(40)], now + 3.0)
        line = th.due(now)
        self.assertNotIn(line.split()[-1], ("me",), line)
        self.assertGreater(len(line.split()), 3, "no stub")
        self.assertIn("melhor", line + " " + (th.due(now + 10) or ""))

    def test_rewrite_after_a_cap_cut_sends_nothing_twice(self):
        th = self.v.Thoughts()
        th.update("", 0.0)
        now = self._speak(th, [f"a{i}" for i in range(30)] + [("b0", 2.0)] + [f"b{i}" for i in range(1, 70)], 0.0)
        first = th.due(now)
        th.update(th.text + " final", now + 0.3)
        second = th.due(now + 10)
        self.assertEqual(len(set(first.split()) & set(second.split())), 0, (first, second))

    SWITCH = {"switch": {"off": r"\b(desliga\w*|turn off)\b.{0,20}\b(microfone|mic|voz)\b", "on": r"\b(liga|turn on)\b.{0,20}\b(microfone|mic|voz)\b",
                         "off_said": "Microfone desligado.", "on_said": "Microfone ligado."}}

    def _flip_waits(self, line):
        before = set(self.v.threading.enumerate())
        got = self.v.heard_switch(line, self.SWITCH)
        for t in set(self.v.threading.enumerate()) - before:
            t.join(2)
        return got

    def test_spoken_switch_turns_the_mic_off_and_on_and_goes_to_nobody(self):
        sent, said = [], []
        self.v.send_to_agent, self.v.say = lambda m, *a: sent.append(m) or True, said.append
        self.assertTrue(self._flip_waits("Piratinha, desliga o microfone."))
        self.assertTrue(self.v.OFF.exists())
        self.assertEqual(said, ["Microfone desligado."])
        self.assertIn("mic off", sent[0])
        self.assertTrue(self._flip_waits("liga o microfone"))
        self.assertFalse(self.v.OFF.exists())
        self.assertEqual(said[-1], "Microfone ligado.")
        self.assertEqual(len(sent), 2)

    def test_saying_it_twice_flips_once(self):
        sent = []
        self.v.send_to_agent, self.v.say = lambda m, *a: sent.append(m) or True, lambda t: None
        self._flip_waits("desliga o microfone")
        self.assertTrue(self._flip_waits("desliga o microfone"))
        self.assertEqual(len(sent), 1)

    def test_talk_about_the_mic_is_not_a_switch(self):
        self.v.send_to_agent = lambda *a: self.fail("nothing to tell")
        for line in ("hoje eu vou te explicar como eu desliga o microfone quando a live termina aqui", "desliga o anúncio", "microfone ligado", ""):
            self.assertFalse(self._flip_waits(line), line)
        self.assertFalse(self.v.OFF.exists())
        self.assertFalse(self.v.heard_switch("desliga o microfone", {}), "no phrases set: no switch")

    def test_its_own_confirmation_coming_back_is_not_a_switch(self):
        self.v.send_to_agent = lambda *a: self.fail("nothing to tell")
        self.v.SAID.write_text("desliga o microfone\n")
        self.v.SPEAKING.write_text(f"{time.time()} {time.time()}")
        self.assertFalse(self._flip_waits("desliga o microfone"))

    def test_command_line_toggle_for_a_stream_deck_key(self):
        self.v.send_to_agent, self.v.say = lambda *a: True, lambda t: None
        self.v.CONFIG.write_text("{}")
        with mock.patch("sys.stdout", new=io.StringIO()) as out:
            self.assertEqual(self.v.main(["toggle"]), 0)
            self.assertEqual(self.v.main(["toggle"]), 0)
            self.assertEqual(self.v.main(["off"]), 0)
            self.assertEqual(self.v.main(["off"]), 0)
        self.assertEqual(out.getvalue().split(), ["off", "on", "off", "off"])
        self.assertTrue(self.v.OFF.exists())

    def test_bare_run_is_usage_not_a_second_serve(self):
        with mock.patch("sys.stderr", new=io.StringIO()):
            self.assertEqual(self.v.main([]), 1)


if __name__ == "__main__":
    unittest.main()
