import re
from pathlib import Path

from openshell_tg import messages

SRC = Path(__file__).parent.parent / "src" / "openshell_tg"


def test_languages_have_same_keys():
    assert set(messages.EN) == set(messages.VI)


def test_placeholders_match():
    for key, en in messages.EN.items():
        vi = messages.VI[key]
        assert set(re.findall(r"{(\w+)}", en)) == set(re.findall(r"{(\w+)}", vi)), key


def test_every_literal_key_in_code_exists():
    used = set()
    for path in SRC.glob("*.py"):
        text = path.read_text()
        used |= set(re.findall(r"m\.t\(\"([a-z_]+)\"", text))
        used |= set(re.findall(r"self\.say\(\"([a-z_]+)\"", text))
        used |= set(re.findall(r"key = \"([a-z_]+)\"", text))
    missing = sorted(k for k in used if k not in messages.EN)
    assert not missing


def test_dynamic_keys_exist():
    for prefix, names in {
        "closed_": ["approved", "rejected", "expired", "expired_off", "approved_elsewhere",
                    "rejected_elsewhere", "gone"],
        "phase_": ["running", "stopped", "missing", "busy"],
        "cmd_": ["help", "list", "status", "rules"],
        "help_": ["up", "status", "list", "pull", "destroy"],
        "reason_": ["broad", "unneeded", "forbidden", "expired"],
    }.items():
        for name in names:
            assert prefix + name in messages.EN


def test_duration():
    assert messages.duration(None) == "–"
    assert messages.duration(42) == "42 s"
    assert messages.duration(192) == "3 min 12 s"
