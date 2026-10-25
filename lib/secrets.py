"""Credential resolution for scripts in this repo.

Credentials come from the environment: a real environment variable, or a
gitignored .env file next to the scripts (or in any parent directory).
Nothing sensitive belongs in this repo.

Real environment variables always win over .env, so
`SUPABASE_ACCESS_TOKEN=... python apply_migration.py` works.
"""

import os

_DOTENV_LOADED = False


class SecretNotFound(RuntimeError):
    pass


def load_dotenv(start_dir=None):
    """Read KEY=VALUE pairs from .env, walking up from start_dir to the repo root.

    Real environment variables always win, so `FOO=x python3 script.py` works.
    Runs at most once per process.
    """
    global _DOTENV_LOADED
    if _DOTENV_LOADED:
        return
    _DOTENV_LOADED = True

    directory = os.path.abspath(start_dir or os.path.dirname(os.path.abspath(__file__)))
    seen = set()
    while directory and directory not in seen:
        seen.add(directory)
        path = os.path.join(directory, ".env")
        if os.path.isfile(path):
            _read_dotenv(path)
        parent = os.path.dirname(directory)
        if parent == directory:
            break
        directory = parent


def _read_dotenv(path):
    with open(path, encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key.startswith("export "):
                key = key[len("export "):].strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            os.environ.setdefault(key, value)


def get_secret(name, env_var=None, required=True):
    """Return a credential from the environment or .env.

    name:      the credential's name, e.g. "SUPABASE_ACCESS_TOKEN".
    env_var:   the environment variable to read; defaults to name.
    required:  raise SecretNotFound when nothing resolves, instead of None.
    """
    load_dotenv()
    var = env_var or name
    value = os.environ.get(var)
    if value:
        # Strip line endings defensively: a value pasted on Windows can carry "\r".
        return value.rstrip("\r\n")
    if required:
        raise SecretNotFound(f"{var} is not set; set it in the environment or in a .env file")
    return None
