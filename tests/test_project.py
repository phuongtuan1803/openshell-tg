from pathlib import Path

import pytest

from openshell_tg import project
from openshell_tg.project import ConfigError


def write(folder: Path, text: str) -> None:
    (folder / "openshell").mkdir(exist_ok=True)
    (folder / "openshell" / "policy.yaml").write_text("version: 1\nnetwork_policies: {}\n")
    (folder / "openshell-tg.yaml").write_text(text)


def test_create_then_load(tmp_path):
    project.create(tmp_path, "codex", "subscription", github=True)
    proj = project.load(tmp_path)
    assert proj.agent == "codex" and proj.providers == ["github"]
    assert proj.idle_stop == 24 * 3600
    assert "codex_subscription" in (tmp_path / "openshell" / "policy.yaml").read_text()


def test_api_key_adds_agent_provider(tmp_path):
    write(tmp_path, "agent: claude\nauth: api_key\nproviders: [github]\n")
    proj = project.load(tmp_path)
    assert project.required_providers(proj) == ["claude-code", "github"]


def test_subscription_needs_no_provider(tmp_path):
    write(tmp_path, "agent: codex\n")
    assert project.required_providers(project.load(tmp_path)) == []


def test_opencode_needs_api_key(tmp_path):
    write(tmp_path, "agent: opencode\nauth: subscription\n")
    with pytest.raises(ConfigError, match="API key"):
        project.load(tmp_path)


def test_unknown_key(tmp_path):
    write(tmp_path, "agent: codex\ncpu: 4\n")
    with pytest.raises(ConfigError, match="cpu"):
        project.load(tmp_path)


def test_secret_in_config_is_refused(tmp_path):
    write(tmp_path, "agent: codex\nname: ghp_abcdefghijklmnopqrstuvwxyz0123456789\n")
    with pytest.raises(ConfigError, match="line 2"):
        project.load(tmp_path)


def test_secret_in_policy_is_refused(tmp_path):
    write(tmp_path, "agent: codex\n")
    (tmp_path / "openshell" / "policy.yaml").write_text("version: 1\n# sk-ant-api03-abcdefghijklmnopqrstuvwxyz\n")
    with pytest.raises(ConfigError, match="policy.yaml"):
        project.load(tmp_path)


def test_bad_duration(tmp_path):
    write(tmp_path, "agent: codex\nidle_stop: soon\n")
    with pytest.raises(ConfigError, match="idle_stop"):
        project.load(tmp_path)


def test_sandbox_name_is_stable_and_unique(tmp_path):
    a, b = tmp_path / "a" / "app", tmp_path / "b" / "app"
    for folder in (a, b):
        folder.mkdir(parents=True)
        write(folder, "agent: codex\n")
    pa, pb = project.load(a), project.load(b)
    assert pa.sandbox == project.load(a).sandbox
    assert pa.sandbox != pb.sandbox
    assert pa.sandbox.startswith("otg-app-")


def test_find_walks_up(tmp_path):
    write(tmp_path, "agent: codex\n")
    deep = tmp_path / "src" / "pkg"
    deep.mkdir(parents=True)
    assert project.find(deep) == tmp_path
