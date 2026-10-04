"""Isolate server tests: run in a tmp cwd (relative data/ paths)."""

import os

import pytest


@pytest.fixture(autouse=True)
def _tmpcwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
