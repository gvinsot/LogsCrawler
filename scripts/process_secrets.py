#!/usr/bin/env python3
"""Convert sensitive environment variables in a docker-compose file into Docker secrets.

Any environment variable whose name ends with one of the following suffixes is
extracted, stored as a Docker secret, and removed from the ``environment:`` block:

    _SECRET, _KEY, _TOKEN, _PASSWORD, _CONNECTIONSTRING, _CONNECTION_STRING

The secret is mounted into each service that previously declared the variable
under ``/run/secrets/<VAR_NAME>``. The application is expected to detect this
file and load its content as the corresponding environment variable (see
``backend/config.py`` and ``agent/config.py``).

Usage:
    process_secrets.py <input_compose> <output_compose> <stack_name>

Notes:
- Docker secrets are immutable. If a secret with the target name already exists
  it is reused (its value is **not** updated). Run ``docker secret rm`` manually
  if you need to rotate a value.
- Requires PyYAML. If not available the script exits 0 without changes.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from typing import Optional

try:
    import yaml  # type: ignore
except ImportError:
    sys.stderr.write(
        "[secrets] PyYAML not available — skipping Docker secret conversion.\n"
        "          Install with: pip install pyyaml\n"
    )
    # Fall back to copying the file unchanged
    if len(sys.argv) == 4 and sys.argv[1] != sys.argv[2]:
        try:
            with open(sys.argv[1], "rb") as src, open(sys.argv[2], "wb") as dst:
                dst.write(src.read())
        except OSError as e:
            sys.stderr.write(f"[secrets] Failed to copy compose file: {e}\n")
            sys.exit(1)
    sys.exit(0)


SECRET_SUFFIXES = (
    "_SECRET",
    "_KEY",
    "_TOKEN",
    "_PASSWORD",
    "_CONNECTIONSTRING",
    "_CONNECTION_STRING",
)
# Matches ${NAME} plus the full Compose / POSIX parameter-expansion family:
#   ${NAME}             - bare reference
#   ${NAME:-default}    - default if unset OR empty
#   ${NAME-default}     - default only if unset
#   ${NAME:?message}    - required, message if unset or empty (Compose errors;
#                         here we just resolve to the env value)
#   ${NAME?message}     - required if unset
#   ${NAME:+alt}        - alt if set AND non-empty
#   ${NAME+alt}         - alt if set (empty ok)
_VAR_REF_RE = re.compile(
    r"\$\{"
    r"([A-Za-z_][A-Za-z0-9_]*)"   # 1: variable name
    r"(?:(:?[-?+])([^}]*))?"      # 2: operator (one of - :- ? :? + :+) + 3: operand
    r"\}"
)


def is_secret_name(name: str) -> bool:
    upper = name.upper()
    return any(upper.endswith(suffix) for suffix in SECRET_SUFFIXES)


def parse_env_entry(entry: str):
    """Return (name, value-or-None) from a 'NAME=value' or 'NAME' string."""
    if "=" in entry:
        k, _, v = entry.partition("=")
        return k.strip(), v
    return entry.strip(), None


def resolve_value(name: str, raw_value: Optional[str]) -> Optional[str]:
    """Resolve Compose-style ${VAR...} substitutions using the current env.

    Supports the full parameter-expansion family (see ``_VAR_REF_RE``).
    The ``:?`` / ``?`` "required" variants don't error here - we just use the
    env value if it's there; downstream code decides if the missing-secret
    case matters.

    If ``raw_value`` is ``None`` (env entry like ``- FOO`` with no value) the
    value is read directly from the process environment.
    """
    if raw_value is None:
        return os.environ.get(name)

    def repl(match: "re.Match[str]") -> str:
        var = match.group(1)
        op = match.group(2)
        operand = match.group(3)
        env_val = os.environ.get(var)

        if op is None:
            return env_val if env_val is not None else ""
        if op == ":-":
            return env_val if env_val else (operand or "")
        if op == "-":
            return env_val if env_val is not None else (operand or "")
        if op in (":?", "?"):
            return env_val if env_val is not None else ""
        if op == ":+":
            return (operand or "") if env_val else ""
        if op == "+":
            return (operand or "") if env_val is not None else ""
        return env_val if env_val is not None else ""

    return _VAR_REF_RE.sub(repl, raw_value)


def docker_secret_exists(name: str) -> bool:
    result = subprocess.run(
        ["docker", "secret", "inspect", name],
        capture_output=True,
    )
    return result.returncode == 0


def create_docker_secret(name: str, value: str) -> None:
    if docker_secret_exists(name):
        sys.stderr.write(f"[secrets] Reusing existing Docker secret: {name}\n")
        return
    sys.stderr.write(f"[secrets] Creating Docker secret: {name}\n")
    proc = subprocess.run(
        ["docker", "secret", "create", name, "-"],
        input=value.encode("utf-8"),
        capture_output=True,
    )
    if proc.returncode != 0:
        sys.stderr.write(
            f"[secrets] Failed to create secret {name}: "
            f"{proc.stderr.decode(errors='replace').strip()}\n"
        )
        sys.exit(1)


def process(compose_path: str, output_path: str, stack_name: str) -> None:
    with open(compose_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    if not isinstance(data, dict) or "services" not in data:
        with open(output_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, sort_keys=False)
        return

    secrets_top = dict(data.get("secrets") or {})
    used_secrets: dict = {}  # var_name -> secret_name

    for svc_name, svc in (data.get("services") or {}).items():
        if not isinstance(svc, dict):
            continue
        env = svc.get("environment")
        if env is None:
            continue

        # Normalize environment to list of (key, value) tuples
        if isinstance(env, dict):
            items = [(str(k), v if v is None else str(v)) for k, v in env.items()]
            is_dict = True
        elif isinstance(env, list):
            items = []
            for entry in env:
                if isinstance(entry, str):
                    items.append(parse_env_entry(entry))
                else:
                    items.append((str(entry), None))
            is_dict = False
        else:
            continue

        # Existing secret references on the service
        svc_secrets = list(svc.get("secrets") or [])
        existing_secret_names = set()
        for s in svc_secrets:
            if isinstance(s, str):
                existing_secret_names.add(s)
            elif isinstance(s, dict) and "source" in s:
                existing_secret_names.add(s["source"])

        new_items = []
        for key, value in items:
            if not is_secret_name(key):
                new_items.append((key, value))
                continue

            resolved = resolve_value(key, value)
            if resolved is None or resolved == "":
                sys.stderr.write(
                    f"[secrets] {svc_name}: {key} has no value — leaving as env var\n"
                )
                new_items.append((key, value))
                continue

            secret_name = f"{stack_name}_{key}"
            create_docker_secret(secret_name, resolved)
            used_secrets[key] = secret_name

            if secret_name not in existing_secret_names:
                svc_secrets.append({"source": secret_name, "target": key})
                existing_secret_names.add(secret_name)

            sys.stderr.write(
                f"[secrets] {svc_name}: {key} -> /run/secrets/{key} "
                f"(docker secret '{secret_name}')\n"
            )

        # Write back the cleaned environment
        if is_dict:
            svc["environment"] = {k: v for k, v in new_items}
            if not svc["environment"]:
                del svc["environment"]
        else:
            new_list = [
                f"{k}={v}" if v is not None else k for k, v in new_items
            ]
            if new_list:
                svc["environment"] = new_list
            else:
                del svc["environment"]

        if svc_secrets:
            svc["secrets"] = svc_secrets

    # Declare the secrets at the top level as external (they were created above)
    for secret_name in used_secrets.values():
        if secret_name not in secrets_top:
            secrets_top[secret_name] = {"external": True}

    if secrets_top:
        data["secrets"] = secrets_top

    with open(output_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False)


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.stderr.write(
            "Usage: process_secrets.py <input_compose> <output_compose> <stack_name>\n"
        )
        sys.exit(2)
    process(sys.argv[1], sys.argv[2], sys.argv[3])
