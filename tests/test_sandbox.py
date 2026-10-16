import subprocess

import pytest

from openshell_tg import osh
from openshell_tg import sandbox as sb


def git(folder, *args):
    subprocess.run(["git", "-C", str(folder), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    folder = tmp_path / "proj"
    folder.mkdir()
    git(folder, "init", "-q")
    (folder / "app.py").write_text("print(1)\n")
    (folder / ".gitignore").write_text("ignored.env\n")
    git(folder, "add", ".")
    git(folder, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    return folder


def test_secret_files_detected(repo):
    (repo / ".env").write_text("A=1")
    (repo / "server.pem").write_text("x")
    (repo / ".env.example").write_text("A=")
    (repo / "ignored.env").write_text("x")
    assert sb.secret_files(repo) == [".env", "server.pem"]


def test_gitignored_secrets_are_fine(repo):
    (repo / ".gitignore").write_text(".env\n")
    (repo / ".env").write_text("A=1")
    assert sb.secret_files(repo) == []


def test_history_script_runs(repo, tmp_path):
    """The restore script, run locally against a fake /sandbox layout, keeps WIP on branch work."""
    (repo / "app.py").write_text("print(2)\n")  # uncommitted change
    work = tmp_path / "sandbox" / "workspace"
    work.mkdir(parents=True)
    for name in ("app.py", ".gitignore"):
        (work / name).write_text((repo / name).read_text())
    git(repo, "bundle", "create", "-q", str(work / ".otg-in.bundle"), "--branches", "HEAD")
    sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    script = sb._restore_history_script(sha, "T (agent)", "t@t")
    script = script.replace(sb.WORKDIR, str(work))
    subprocess.run(["sh", "-c", script], check=True)
    out = lambda *a: subprocess.run(["git", "-C", str(work), *a], capture_output=True, text=True).stdout.strip()
    assert out("branch", "--show-current") == "work"
    assert out("rev-parse", "HEAD") == sha
    assert out("status", "--porcelain") == "M app.py"
    assert (work / ".git" / "otg-restored").exists()
    assert not (work / ".otg-in.bundle").exists()


def test_create_refuses_secret_files(repo, monkeypatch):
    from openshell_tg import project
    (repo / "id_ed25519").write_text("key")
    project.create(repo, "codex", "subscription", github=False)
    proj = project.load(repo)
    called = []
    monkeypatch.setattr("openshell_tg.osh.create", lambda *a, **k: called.append(a))
    with pytest.raises(sb.SandboxError, match="id_ed25519"):
        sb.create(proj, [], print, lambda q: True)
    assert not called


def test_broken_sandbox_is_recreated_after_confirmation(monkeypatch, tmp_path):
    from openshell_tg import osh
    calls = []
    proj = type("P", (), {"sandbox": "otg-x", "folder": tmp_path})()
    monkeypatch.setattr(sb, "state", lambda name: "Error")
    monkeypatch.setattr(sb, "_wait_while_busy", lambda name, say: "Error")

    def fail_start(name):
        raise osh.OpenShellError(["sandbox", "start"], 1, "current phase: Error")

    monkeypatch.setattr(osh, "start", fail_start)
    monkeypatch.setattr(osh, "delete", lambda name: calls.append("delete"))
    monkeypatch.setattr(sb, "create", lambda *a, **k: calls.append("create"))
    assert sb.ensure_running(proj, [], lambda t: None, lambda q: True) is True
    assert calls == ["delete", "create"]


def test_missing_marker_reruns_restore_instead_of_failing(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from openshell_tg import osh
    proj = SimpleNamespace(sandbox="otg-x", folder=tmp_path)
    markers = iter([1, 0, 0])  # marker missing; present after restore; terminal probe ok
    restored = []
    monkeypatch.setattr(sb, "_wait_while_busy", lambda name, say: "Ready")
    monkeypatch.setattr(sb, "_restore", lambda p: restored.append(1))
    monkeypatch.setattr(osh, "exec_", lambda *a, **k: SimpleNamespace(returncode=next(markers)))
    monkeypatch.setattr(osh, "attached_providers", lambda name: set())
    assert sb.ensure_running(proj, [], lambda t: None, lambda q: True) is False
    assert restored == [1]


def test_blocked_terminal_is_explained_before_attach(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from openshell_tg import osh
    import pytest
    proj = SimpleNamespace(sandbox="otg-x", folder=tmp_path, policy=tmp_path / "openshell" / "policy.yaml")
    monkeypatch.setattr(sb, "_wait_while_busy", lambda name, say: "Ready")
    results = iter([0, 1])  # marker present; tmux probe fails
    monkeypatch.setattr(osh, "exec_", lambda *a, **k: SimpleNamespace(returncode=next(results)))
    with pytest.raises(sb.SandboxError) as err:
        sb.ensure_running(proj, [], lambda t: None, lambda q: True)
    assert "/dev/ptmx" in str(err.value) and "policy.yaml" in str(err.value)


def test_policy_templates_allow_terminal_devices():
    import yaml
    from pathlib import Path
    import openshell_tg
    for f in (Path(openshell_tg.__file__).parent / "templates").glob("policy*.yaml"):
        fs = yaml.safe_load(f.read_text())["filesystem_policy"]
        assert {"/dev/ptmx", "/dev/pts"} <= set(fs["read_write"]), f.name


def test_tell_agent_types_literal_text_then_enter(monkeypatch):
    calls = []
    monkeypatch.setattr(osh, "exec_", lambda name, cmd, **kw: calls.append(cmd))
    assert sb.tell_agent("sb", "line one\nline two\x1b[31m")
    assert len(calls) == 1
    assert calls[0] == ["tmux", "send-keys", "-t", "agent", "-l", "line one line two [31m",
                        ";", "send-keys", "-t", "agent", "Enter"]


def test_screen_returns_last_lines(monkeypatch):
    out = "\n".join(f"line {i}" for i in range(40)) + "\n\n\n"
    monkeypatch.setattr(osh, "exec_", lambda *a, **k: subprocess.CompletedProcess([], 0, out, ""))
    text = sb.screen("sb", lines=3)
    assert text == "line 37\nline 38\nline 39"
