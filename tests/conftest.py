import os
import stat
import sys
import textwrap
from pathlib import Path

import pytest

from openshell_tg import messages
from openshell_tg.store import Store


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    messages.set_language("en")
    yield


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "test.db")


class FakeTelegram:
    def __init__(self):
        self.sent = []
        self.edits = []
        self.answers = []
        self._next = 100

    def send(self, chat_id, text, buttons=None):
        self._next += 1
        self.sent.append((self._next, text, buttons))
        return self._next

    def edit(self, chat_id, message_id, text, buttons=None):
        self.edits.append((message_id, text, buttons))

    def answer(self, callback_id, text=None):
        self.answers.append(text)

    def set_commands(self, commands):
        pass


@pytest.fixture
def tg():
    return FakeTelegram()


@pytest.fixture
def fake_openshell(tmp_path, monkeypatch):
    """A fake `openshell` that logs argv and selected env vars, and prints canned output.

    Canned output: write files to <dir>/out/<subcommand>-<subcommand2>.
    """
    root = tmp_path / "fake"
    (root / "out").mkdir(parents=True)
    script = root / "openshell"
    script.write_text(textwrap.dedent(f"""\
        #!{sys.executable}
        import json, os, sys
        root = {str(root)!r}
        with open(os.path.join(root, "calls.jsonl"), "a") as f:
            f.write(json.dumps({{"argv": sys.argv[1:], "env": {{k: v for k, v in os.environ.items() if k.endswith(("_TOKEN", "_KEY"))}}}}) + "\\n")
        key = "-".join(a for a in sys.argv[1:3] if not a.startswith("-"))
        path = os.path.join(root, "out", key)
        if os.path.exists(path):
            sys.stdout.write(open(path).read())
        err = os.path.join(root, "out", key + ".err")
        if os.path.exists(err):
            sys.stderr.write(open(err).read())
        code = os.path.join(root, "out", key + ".exit")
        sys.exit(int(open(code).read()) if os.path.exists(code) else 0)
        """))
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr("openshell_tg.osh.BINARY", str(script))

    class Fake:
        dir = root

        def output(self, key, text, exit_code=0, stderr=""):
            (root / "out" / key).write_text(text)
            if stderr:
                (root / "out" / f"{key}.err").write_text(stderr)
            if exit_code:
                (root / "out" / f"{key}.exit").write_text(str(exit_code))

        def calls(self):
            import json
            path = root / "calls.jsonl"
            return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []

    return Fake()
