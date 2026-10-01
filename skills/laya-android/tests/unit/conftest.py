import pytest


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """No test touches the real home dir or the CLI session files."""
    monkeypatch.setenv("LAYA_ANDROID_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    monkeypatch.delenv("LAYA_ANDROID_RECORD", raising=False)
    yield
