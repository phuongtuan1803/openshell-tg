import pytest

from openshell_tg import cli, diagnose, osh
from openshell_tg import messages as m

IMAGE_DENIED = """Provisioning sandbox (structured output on stdout)...
Error:   × sandbox entered error phase while provisioning: ProvisioningFailed: failed
  │ to authenticate registry access for vm sandbox image 'ghcr.io/x/y:0.1.0': Authentication failure:
  │ {"errors":[{"code":"DENIED","message":"requested access to the resource is
  │ denied"}]}"""


def err(text, code=1, args=("sandbox", "create", "--name")):
    return osh.OpenShellError(list(args), code, text)


@pytest.mark.parametrize("text,code,expected", [
    (IMAGE_DENIED, 1, "image_denied"),
    ("manifest unknown", 1, "image_missing"),
    ("ProvisioningTimedOut: Provisioning repair window expired after 300 seconds", 1, "provisioning"),
    ("transport error: tcp connect error: Connection refused", 1, "gateway_down"),
    ("timed out after 600 s", 124, "timeout"),
    ("write failed: No space left on device", 1, "disk_full"),
    ("open /dev/kvm: Permission denied", 1, "kvm"),
    ("sandbox must be Stopped, Completed, or a failed main-process Error to start", 1, "wrong_state"),
    ("something odd", 1, None),
])
def test_cause(text, code, expected):
    assert diagnose.cause(err(text, code)) == expected


def test_report_has_cause_next_step_detail_and_log(tmp_path):
    out = diagnose.report(err(IMAGE_DENIED), tmp_path / "x.log")
    assert "openshell sandbox create" in out
    assert "Public" in out
    assert "requested access to the resource is" in out
    assert str(tmp_path / "x.log") in out and "openshell-tg logs" in out
    assert "│" not in out


def test_every_cause_has_text_in_both_languages():
    for lang in ("en", "vi"):
        for key, _ in diagnose.CAUSES:
            assert m.t(f"cause_{key}", lang=lang)


def test_logs_command_prints_tail(capsys):
    path = cli.LOG_FILE()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(f"line {i}" for i in range(100)))
    assert cli.main(["logs", "-n", "3"]) == 0
    assert capsys.readouterr().out.strip().splitlines() == ["line 97", "line 98", "line 99"]


def test_unexpected_error_is_caught(monkeypatch, capsys):
    monkeypatch.setattr(cli, "cmd_list", lambda a: 1 / 0)
    assert cli.main(["list"]) == 1
    assert "openshell-tg logs" in capsys.readouterr().out
