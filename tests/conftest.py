import pytest


@pytest.fixture(autouse=True)
def isolate_run_output(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
