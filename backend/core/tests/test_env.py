from pathlib import Path

import pytest

from core.env import MissingEnvError, env_bool, env_list, env_str, load_dotenv


def test_env_str_requires_value_without_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MARCELINE_TEST_VAR", raising=False)

    with pytest.raises(MissingEnvError):
        env_str("MARCELINE_TEST_VAR")
    assert env_str("MARCELINE_TEST_VAR", default="fallback") == "fallback"


def test_env_bool_and_list_parse_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MARCELINE_TEST_BOOL", "Yes")
    monkeypatch.setenv("MARCELINE_TEST_LIST", "a, b,,c")

    assert env_bool("MARCELINE_TEST_BOOL", default=False) is True
    assert env_list("MARCELINE_TEST_LIST", default=[]) == ["a", "b", "c"]


def test_load_dotenv_does_not_override_existing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text('# comment\nMARCELINE_A="from file"\nexport MARCELINE_B=file\n')
    monkeypatch.delenv("MARCELINE_A", raising=False)
    monkeypatch.setenv("MARCELINE_B", "from env")

    load_dotenv(dotenv)

    assert env_str("MARCELINE_A") == "from file"
    assert env_str("MARCELINE_B") == "from env"
    monkeypatch.delenv("MARCELINE_A")
