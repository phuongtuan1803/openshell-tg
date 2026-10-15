import subprocess

import pytest

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
