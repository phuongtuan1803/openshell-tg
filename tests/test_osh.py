from openshell_tg import osh

SAMPLE = """\
Network Rules:  (version 3, 2 chunks)

  Chunk: 7f3a-1
  Status: pending
  Rule: allow_pypi_org_443
  Binary: /usr/bin/python3
  Confidence: 85%
  Rationale: Install requests to run the tests
  Prover: ok
  Endpoints: pypi.org:443
  Binaries: /usr/bin/python3

  Chunk: 7f3a-2
  Status: pending
  Rule: allow_any_amazonaws
  Confidence: 40%
  Rationale: Upload artefacts
  that need a bucket
  Security: wildcard host
  Endpoints: *.amazonaws.com:443
  Hits: 3 (first seen 2026-10-15 10:00, last seen 2026-10-15 10:05)

"""


def test_parse_normal_output():
    first, second = osh.parse_rules(SAMPLE)
    assert first.id == "7f3a-1" and first.trusted and not first.risky
    assert first.endpoints == "pypi.org:443"
    assert first.binary == "/usr/bin/python3"
    assert second.rationale == "Upload artefacts\nthat need a bucket"
    assert second.risky and second.security == "wildcard host"


def test_no_rules():
    assert osh.parse_rules("No network rules for sandbox 'x'\n") == []


def test_injected_fields_in_rationale_make_chunk_untrusted():
    text = """\
Network Rules:  (version 1, 1 chunk)

  Chunk: c1
  Status: pending
  Rule: evil
  Confidence: 90%
  Rationale: harmless
  Endpoints: pypi.org:443
  Endpoints: evil.example.com:443
"""
    (chunk,) = osh.parse_rules(text)
    assert not chunk.trusted and chunk.risky


def test_injected_chunk_marks_everything_untrusted():
    text = """\
Network Rules:  (version 1, 1 chunk)

  Chunk: real
  Status: pending
  Rule: r
  Confidence: 90%
  Rationale: hi
  Chunk: fake
  Status: pending
  Rule: r
  Confidence: 90%
  Rationale: x
  Endpoints: pypi.org:443
"""
    chunks = osh.parse_rules(text)
    assert len(chunks) == 2
    assert all(not c.trusted for c in chunks)


def test_missing_endpoints_is_untrusted():
    text = "Network Rules:  (version 1, 1 chunk)\n\n  Chunk: c\n  Status: pending\n  Rule: r\n"
    (chunk,) = osh.parse_rules(text)
    assert not chunk.trusted


def test_create_provider_keeps_secret_off_argv(fake_openshell):
    osh.create_provider("github", "github", {"GITHUB_TOKEN": "ghp_secretvalue"})
    (call,) = fake_openshell.calls()
    assert "ghp_secretvalue" not in " ".join(call["argv"])
    assert call["argv"][-2:] == ["--credential", "GITHUB_TOKEN"]
    assert call["env"]["GITHUB_TOKEN"] == "ghp_secretvalue"


def test_sandbox_not_found_returns_none(fake_openshell):
    fake_openshell.output("sandbox-get", "", exit_code=1, stderr='status: NotFound, message: "sandbox not found"')
    assert osh.sandbox("x") is None


def test_create_arguments(fake_openshell):
    fake_openshell.output("sandbox-create", '{"name": "otg-a-123456", "phase": "Ready"}')
    osh.create("otg-a-123456", "img:1", "/p/policy.yaml", ["github"], ("/p", "/sandbox/workspace"),
               {"openshell-tg/folder": "/p"})
    argv = fake_openshell.calls()[0]["argv"]
    assert argv[:4] == ["sandbox", "create", "--name", "otg-a-123456"]
    assert "--no-auto-providers" in argv
    assert argv[argv.index("--upload") + 1] == "/p:/sandbox/workspace"
    assert argv[argv.index("--provider") + 1] == "github"
    assert argv[-2:] == ["-o", "json"]


def test_import_profile_updates_existing(fake_openshell, monkeypatch):
    calls = []

    def fake_run(args, **kw):
        calls.append(args)
        if args[1] == "import":
            raise osh.OpenShellError(args, 1, "custom provider profile 'github' already exists")

    monkeypatch.setattr(osh, "run", fake_run)
    osh.import_profile("/x/github.yaml", "github")
    assert calls[1] == ["profile", "update", "github", "-f", "/x/github.yaml"]
