import pytest

from openshell_tg import cli, host, osh, project
from openshell_tg import sandbox as sb


@pytest.fixture
def wired(tmp_path, monkeypatch):
    calls = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(host, "check_kvm", lambda: calls.append("kvm"))
    monkeypatch.setattr(host, "check_resources", lambda: calls.append("resources"))
    monkeypatch.setattr(host, "ensure_openshell", lambda ask: calls.append("openshell"))
    monkeypatch.setattr(host, "ensure_gateway", lambda image, say: calls.append("gateway"))
    monkeypatch.setattr(host, "setup_telegram", lambda *a: calls.append("telegram"))
    monkeypatch.setattr(osh, "enable_proposals", lambda: calls.append("advisor"))
    monkeypatch.setattr(osh, "providers", lambda: set())
    monkeypatch.setattr(osh, "import_profile", lambda path, pid: calls.append(f"import:{pid}"))
    monkeypatch.setattr(osh, "create_provider", lambda name, prof, creds: calls.append(f"provider:{name}:{sorted(creds)}"))
    monkeypatch.setattr(sb, "state", lambda name: None)
    monkeypatch.setattr(sb, "ensure_running", lambda proj, providers, say, confirm: calls.append(f"sandbox:{providers}") or True)
    monkeypatch.setattr(sb, "attach", lambda proj: calls.append("attach") or 0)
    monkeypatch.setattr(sb, "agent_running", lambda name: True)
    monkeypatch.setattr(cli, "prompt_secret", lambda q: "secret-value")
    return calls


def test_up_first_run(wired, tmp_path, monkeypatch):
    answers = iter(["claude", "api_key", "y"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    assert cli.main(["up"]) == 0
    assert (tmp_path / "openshell-tg.yaml").exists()
    assert wired == ["kvm", "openshell", "gateway", "telegram", "advisor",
                     "import:claude-code", "provider:claude-code:['ANTHROPIC_API_KEY']",
                     "import:github", "provider:github:['GITHUB_TOKEN']",
                     "resources", "sandbox:['claude-code', 'github']", "attach"]


def test_up_stops_on_config_error(wired, tmp_path, capsys):
    (tmp_path / "openshell-tg.yaml").write_text("agent: nope\n")
    assert cli.main(["up"]) == 1
    assert "agent is nope" in capsys.readouterr().out
    assert wired == []


def test_up_reports_openshell_failure_plainly(wired, tmp_path, monkeypatch, capsys):
    project.create(tmp_path, "codex", "subscription", github=False)

    def boom(*a):
        raise osh.OpenShellError(["sandbox", "create"], 1, "status: Internal")

    monkeypatch.setattr(sb, "ensure_running", boom)
    assert cli.main(["up"]) == 1
    out = capsys.readouterr().out
    assert "Stopped at: openshell sandbox create" in out and "Traceback" not in out


def test_up_says_when_agent_exited_at_once(wired, tmp_path, monkeypatch, capsys):
    answers = iter(["claude", "api_key", "n"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    monkeypatch.setattr(sb, "agent_running", lambda name: False)
    assert cli.main(["up"]) == 1
    out = capsys.readouterr().out
    assert "is not running" in out and "openshell sandbox exec -n" in out
