"""Helpers for loading Docker secrets mounted under ``/run/secrets/``.

Docker Swarm mounts each secret as a file at ``/run/secrets/<NAME>``. This
module exposes :func:`load_docker_secrets_into_env` which copies the content of
those files into ``os.environ`` so that the rest of the application can keep
reading configuration through the usual environment-variable mechanism.

Behaviour:
- Existing environment variables are **never** overwritten.
- File names that do not look like valid env var identifiers are ignored.
- A trailing newline in the secret file is stripped.
- Errors reading individual files are silently ignored to avoid breaking
  startup when one secret is misconfigured.
"""
from __future__ import annotations

import os
import re

_SECRETS_DIR = "/run/secrets"
_VALID_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def load_docker_secrets_into_env(secrets_dir: str = _SECRETS_DIR) -> None:
    """Populate ``os.environ`` from files in ``secrets_dir``.

    Safe to call multiple times. Existing environment variables take
    precedence over file content.
    """
    if not os.path.isdir(secrets_dir):
        return
    try:
        names = os.listdir(secrets_dir)
    except OSError:
        return
    for name in names:
        if not _VALID_NAME_RE.match(name):
            continue
        if name in os.environ:
            continue
        path = os.path.join(secrets_dir, name)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as fh:
                value = fh.read()
        except OSError:
            continue
        # Strip a single trailing newline (common when secrets are created
        # from files) but preserve any intentional trailing whitespace.
        if value.endswith("\n"):
            value = value[:-1]
        os.environ[name] = value
