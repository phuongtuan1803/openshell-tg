from collections import namedtuple

import pytest

from openshell_tg import host

Usage = namedtuple("Usage", "total used free")
GIB = 2**30


def _stub(monkeypatch, mem, free_gib):
    monkeypatch.setattr(host, "_vm_mem_mib", lambda: 8192)
    monkeypatch.setattr(host, "_mem_available_mib", lambda: mem)
    monkeypatch.setattr(host.shutil, "disk_usage", lambda p: Usage(100 * GIB, 0, free_gib * GIB))


def test_low_memory_stops(monkeypatch):
    _stub(monkeypatch, 4096, 90)
    with pytest.raises(host.SetupError, match="4.0 GB free"):
        host.check_resources()


def test_low_disk_stops(monkeypatch):
    _stub(monkeypatch, 16000, 5)
    with pytest.raises(host.SetupError, match="5.0 GB free"):
        host.check_resources()


def test_enough_resources_pass(monkeypatch):
    _stub(monkeypatch, 16000, 90)
    host.check_resources()


def test_unknown_memory_does_not_block(monkeypatch):
    _stub(monkeypatch, None, 90)
    host.check_resources()
