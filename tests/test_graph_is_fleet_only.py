"""The Board is where tasks live; the Graph shows the fleet. And the Boss wears the Plow mark.

The CEO (2026-10-05): the board and the graph "should be different views", and the graph had a
lot of board baked in (a priority bar, a task card under every agent, a backlog box, a full task
editor). He also asked what the serif "B" Boss avatar was doing in a $4B company's app. These
checks fail if either comes back.
"""
import ast
from pathlib import Path
import re
import unittest

BIN = Path(__file__).resolve().parents[1] / "mypeople" / "runtime" / "bin"
GRAPH = (BIN / "terminal-graph.html").read_text()
BOARD = (BIN / "todos.html").read_text()
SERVER = (BIN / "todo-server.py").read_text()


class GraphIsFleetOnly(unittest.TestCase):
    def test_no_board_features_on_the_graph(self):
        for board_thing in ('id="taskbar"', "backlog", "task-node", "taskModal", "Add a priority",
                            "taskSearch", "/todo/board", "/todo/update", "/todo/comment", "/todo/status"):
            with self.subTest(board_thing=board_thing):
                self.assertNotIn(board_thing, GRAPH)

    def test_an_old_graph_task_link_opens_the_task_on_the_board(self):
        self.assertIn('location.replace("/?task="+encodeURIComponent(t))', GRAPH)
        self.assertIn('searchParams.get("task")', BOARD)   # the board opens /?task=ID

    def test_the_graph_endpoint_never_loads_the_board(self):
        fn = next(n for n in ast.walk(ast.parse(SERVER))
                  if isinstance(n, ast.FunctionDef) and n.name == "_terminal_graph")
        body = ast.get_source_segment(SERVER, fn)
        self.assertNotIn("load_board", body)   # the whole board, every 2s, per open graph
        self.assertNotIn('"tasks"', body)


class BossWearsThePlowMark(unittest.TestCase):
    def test_no_letter_avatar(self):
        self.assertNotIn('textContent="B"', BOARD)
        self.assertNotRegex(BOARD, r'class="boss-av"[^>]*>\s*B\s*<')

    def test_the_hero_avatar_is_the_plow_icon_and_the_others_copy_it(self):
        hero = re.search(r'<div class="boss-av"[^>]*>(.*?)</div>', BOARD, re.S).group(1)
        self.assertIn('<svg viewBox="0 0 500 500"><path fill="currentColor"', hero)
        self.assertIn("color:var(--grove)", BOARD)   # Grove on Volt, an approved logo variant
        self.assertEqual(2, BOARD.count('innerHTML=$("#bossHero .boss-av").innerHTML'))


if __name__ == "__main__":
    unittest.main()
