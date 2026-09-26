"""Secrets arrive on stdin while the ordinary config writer keeps profile ownership."""

import argparse
import io
import os
import subprocess
import sys
from pathlib import Path

import pytest
from dotenv import dotenv_values

from hermes_cli.config import config_command
from hermes_cli.subcommands.config import build_config_parser


def test_stdin_credentials_use_real_cli_and_isolate_homes(tmp_path):
    homes = [tmp_path / "a", tmp_path / "b"]
    values = ["synthetic-first", "synthetic-second", "synthetic-replaced"]
    for home, value in zip([homes[0], homes[1], homes[0]], values):
        env = dict(os.environ, HOME=str(tmp_path), HERMES_HOME=str(home))
        command = [sys.executable, "-m", "hermes_cli.main", "config", "set",
                   "DISCORD_BOT_TOKEN", "--stdin"]
        result = subprocess.run(command, input=value + "\n", text=True,
                                capture_output=True, env=env, timeout=30,
                                cwd=Path(__file__).resolve().parents[2])
        assert result.returncode == 0, result.stderr + result.stdout
        assert value not in result.stdout + result.stderr
        assert value not in command
        assert dotenv_values(home / ".env")["DISCORD_BOT_TOKEN"] == value
    assert dotenv_values(homes[1] / ".env")["DISCORD_BOT_TOKEN"] == values[1]
    assert dotenv_values(homes[0] / ".env")["DISCORD_BOT_TOKEN"] == values[2]


@pytest.mark.parametrize("value,stream", [
    ("synthetic-argv", "synthetic-pipe"), (None, ""), (None, "\n"),
])
def test_stdin_refuses_ambiguous_or_empty_input_before_writing(
        tmp_path, monkeypatch, capsys, value, stream):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(sys, "stdin", io.StringIO(stream))
    parser = argparse.ArgumentParser()
    build_config_parser(parser.add_subparsers(), cmd_config=config_command)
    argv = ["config", "set", "DISCORD_BOT_TOKEN", "--stdin"]
    if value is not None:
        argv.insert(-1, value)
    args = parser.parse_args(argv)
    with pytest.raises(SystemExit) as exc:
        args.func(args)
    assert exc.value.code != 0
    assert not (tmp_path / ".env").exists()
    captured = capsys.readouterr()
    assert "synthetic-argv" not in captured.out + captured.err
    assert "synthetic-pipe" not in captured.out + captured.err
