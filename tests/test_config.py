import os

import pytest

import config


@pytest.fixture
def env(tmp_path, monkeypatch):
    home_cfg = tmp_path / "xdg"
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home_cfg))
    monkeypatch.delenv(config.CONFIG_ENV_VAR, raising=False)
    monkeypatch.delenv("TEST_SOURCE", raising=False)
    monkeypatch.chdir(cwd)
    yield home_cfg / config.APP_NAME / config.CONFIG_FILENAME, cwd / ".env"
    os.environ.pop("TEST_SOURCE", None)  # set by load_dotenv


def _write(path, source):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"TEST_SOURCE={source}\n")


def test_home_config_wins_over_local_env(env):
    home, local = env
    _write(home, "home")
    _write(local, "local")
    assert config.load_config_file() == str(home)
    assert os.environ["TEST_SOURCE"] == "home"


def test_falls_back_to_local_env(env):
    _, local = env
    _write(local, "local")
    assert config.load_config_file() == str(local)
    assert os.environ["TEST_SOURCE"] == "local"


def test_explicit_path_wins(env, tmp_path, monkeypatch):
    home, _ = env
    _write(home, "home")
    explicit = tmp_path / "server.env"
    _write(explicit, "explicit")
    monkeypatch.setenv(config.CONFIG_ENV_VAR, str(explicit))
    assert config.load_config_file() == str(explicit)
    assert os.environ["TEST_SOURCE"] == "explicit"


def test_missing_explicit_path_raises(env, tmp_path):
    with pytest.raises(FileNotFoundError):
        config.load_config_file(str(tmp_path / "nope.env"))


def test_no_config_file(env):
    assert config.load_config_file() is None
