import os

from vra.envfile import load_env_file, parse_env


def test_parse_env_handles_quotes_comments_export():
    text = '# comment\nA=1\nexport B="two words"\nC=\'x\'\nD=val # trailing\nE=\n\nnot a pair\nF = spaced \n'
    assert parse_env(text) == {"A": "1", "B": "two words", "C": "x", "D": "val", "E": "", "F": "spaced"}


def test_load_env_file_does_not_override_and_never_returns_values(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_text("VRA_T_NEW=fromfile\nVRA_T_KEEP=fromfile\nVRA_T_EMPTY=\n", encoding="utf-8-sig")
    monkeypatch.setenv("VRA_T_KEEP", "fromshell")
    monkeypatch.delenv("VRA_T_NEW", raising=False)
    monkeypatch.delenv("VRA_T_EMPTY", raising=False)
    loaded = load_env_file(f)
    assert loaded == ["VRA_T_NEW"]                       # names only
    assert os.environ["VRA_T_NEW"] == "fromfile" and os.environ["VRA_T_KEEP"] == "fromshell"
    assert "VRA_T_EMPTY" not in os.environ                # empty values are ignored (e.g. unset API key)
    assert load_env_file(tmp_path / "missing.env") == []
    monkeypatch.delenv("VRA_T_NEW")
