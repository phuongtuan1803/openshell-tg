import logging

from openshell_tg import logsetup
from openshell_tg.logsetup import redact


def test_redacts_credentials():
    token = "123456789:" + "A" * 35
    text = redact(f"POST https://api.telegram.org/bot{token}/getMe token={token} sk-ant-abc123456789012345 "
                  "Authorization: Bearer abcdefghijkl ghp_" + "x" * 30 + " password: hunter2hunter2")
    for secret in (token, "sk-ant-abc", "abcdefghijkl", "ghp_x", "hunter2hunter2"):
        assert secret not in text
    assert text.count("[hidden]") >= 5


def test_leaves_ordinary_lines_alone():
    line = "request: sandbox=otg-oma-0aaee3 chunk=ab12 rule=allow_pypi endpoints=pypi.org:443 risky=False"
    assert redact(line) == line


def test_setup_writes_role_and_hides_secrets(monkeypatch):
    root = logging.getLogger()
    before = list(root.handlers), root.level
    try:
        logsetup.setup_logging(service=True)
        logging.getLogger("openshell_tg.test").warning("failed with token=abcdef123456")
        for h in root.handlers:
            h.flush()
        text = logsetup.LOG_FILE().read_text()
        assert "[serve]" in text and "abcdef123456" not in text and "WARNING" in text
    finally:
        for h in list(root.handlers):
            root.removeHandler(h)
            h.close()
        for h in before[0]:
            root.addHandler(h)
        root.setLevel(before[1])


def test_redacts_name_value_forms_but_not_prose():
    assert "abc123456789" not in redact("GITHUB_TOKEN=abc123456789")
    assert "xyz987654321" not in redact('{"token": "xyz987654321"}')
    assert "supersecret1" not in redact("password: supersecret1")
    assert redact("token: expired") == "token: expired"
    assert redact("credentials: not found") == "credentials: not found"
