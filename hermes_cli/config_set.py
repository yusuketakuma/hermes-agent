"""Input handling for the config set command, including secret-safe stdin."""

import sys


def set_config_command(args):
    from hermes_cli import config

    key = getattr(args, "key", None)
    value = getattr(args, "value", None)
    from_stdin = bool(getattr(args, "stdin", False))
    if not key or (value is None and not from_stdin):
        config._usage_exit(*config._USAGE_SET)
    if from_stdin:
        if value is not None:
            config._exit_invalid("Pass either a value or --stdin, not both.")
        if sys.stdin.isatty():
            config._exit_invalid("--stdin requires piped input or an input file.")
        try:
            value = sys.stdin.read()
        except (OSError, UnicodeError):
            config._exit_invalid("Could not read the configuration value from stdin.")
        if value.endswith("\n"):
            value = value[:-1].removesuffix("\r")
        if not value:
            config._exit_invalid("--stdin received an empty value; nothing was written.")
    config._run_write_command(
        config.set_config_value, key, value, bool(getattr(args, "force", False)))
