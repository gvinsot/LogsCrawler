#!/usr/bin/env python3
"""Prefix Traefik label names in a docker-compose file for QA-mode deployments.

When the same Docker Swarm cluster runs both a prod and a QA copy of a stack,
the Traefik labels declared in their compose files clash: routers, services
and locally-defined middlewares share the same names, so Traefik picks one
and the other becomes unreachable.

This helper rewrites Traefik labels so that every local Traefik identifier
(router / service / middleware defined in the same compose file) is prefixed
with the QA stack prefix. Provider-qualified references like ``noop@internal``
or ``global@file`` are left untouched.

Usage:
    process_qa_labels.py <input_compose> <output_compose> <prefix>

Example with prefix="qa-":
    "traefik.http.routers.foo.rule=..."         -> "traefik.http.routers.qa-foo.rule=..."
    "traefik.http.routers.foo.service=foo"      -> "traefik.http.routers.qa-foo.service=qa-foo"
    "traefik.http.routers.foo.service=x@file"   -> unchanged
"""
from __future__ import annotations

import re
import sys

try:
    import yaml  # type: ignore
except ImportError:
    sys.stderr.write(
        "[qa-labels] PyYAML not available - skipping Traefik label rewrite.\n"
        "          Install with: pip install pyyaml\n"
    )
    if len(sys.argv) == 4 and sys.argv[1] != sys.argv[2]:
        try:
            with open(sys.argv[1], "rb") as src, open(sys.argv[2], "wb") as dst:
                dst.write(src.read())
        except OSError as e:
            sys.stderr.write(f"[qa-labels] Failed to copy compose file: {e}\n")
            sys.exit(1)
    sys.exit(0)


# Matches "traefik.<proto>.<kind>.<name>.<rest>" where <name> is the user-defined
# identifier we want to rewrite. Only http/tcp/udp routers/services/middlewares
# are rewritten; other namespaces (e.g. tls.options, providers) are left alone.
_LABEL_KEY_RE = re.compile(
    r"^(traefik\.(?:http|tcp|udp)\.(?:routers|services|middlewares)\.)"
    r"([A-Za-z0-9][A-Za-z0-9_-]*)"
    r"(\..+)$"
)


def _normalise_labels(labels):
    """Return labels as a list of (key, value_or_None) pairs and a flag telling
    whether the original form was a dict or a list of strings."""
    if isinstance(labels, dict):
        pairs = []
        for k, v in labels.items():
            key = str(k)
            value = None if v is None else str(v)
            pairs.append((key, value))
        return pairs, True
    if isinstance(labels, list):
        pairs = []
        for entry in labels:
            s = str(entry)
            if "=" in s:
                k, _, v = s.partition("=")
                pairs.append((k.strip(), v))
            else:
                pairs.append((s.strip(), None))
        return pairs, False
    return None, False


def _collect_local_names(pairs) -> set:
    names = set()
    for key, _ in pairs:
        m = _LABEL_KEY_RE.match(key)
        if m:
            names.add(m.group(2))
    return names


def _rewrite_reference_list(values: str, local_names: set, prefix: str) -> str:
    """Rewrite a comma-separated list of references (e.g. middlewares=...).

    Items containing '@' are provider-qualified (e.g. 'global@file') and are
    left untouched. Items matching a locally defined name get the prefix.
    Unknown items are left as-is (likely external references).
    """
    parts = [p.strip() for p in values.split(",")]
    out = []
    for p in parts:
        if not p or "@" in p:
            out.append(p)
        elif p in local_names:
            out.append(prefix + p)
        else:
            out.append(p)
    return ",".join(out)


def _rewrite_pairs(pairs, prefix: str):
    local_names = _collect_local_names(pairs)
    if not local_names:
        return pairs

    rewritten = []
    for key, value in pairs:
        new_key = key
        m = _LABEL_KEY_RE.match(key)
        if m and m.group(2) in local_names:
            new_key = f"{m.group(1)}{prefix}{m.group(2)}{m.group(3)}"

        new_value = value
        if value is not None:
            if key.endswith(".service"):
                v = value.strip()
                if v and "@" not in v and v in local_names:
                    new_value = prefix + v
            elif key.endswith(".middlewares"):
                new_value = _rewrite_reference_list(value, local_names, prefix)

        rewritten.append((new_key, new_value))
    return rewritten


def _emit_labels(pairs, was_dict):
    if was_dict:
        return {k: ("" if v is None else v) for k, v in pairs}
    return [k if v is None else f"{k}={v}" for k, v in pairs]


def _process_service(svc: dict, prefix: str) -> None:
    # Container-level labels (rare for Swarm Traefik but supported)
    labels = svc.get("labels")
    pairs, was_dict = _normalise_labels(labels)
    if pairs is not None:
        new_pairs = _rewrite_pairs(pairs, prefix)
        svc["labels"] = _emit_labels(new_pairs, was_dict)

    # Swarm deploy.labels - this is where Traefik reads from in Swarm mode
    deploy = svc.get("deploy")
    if isinstance(deploy, dict):
        dlabels = deploy.get("labels")
        pairs, was_dict = _normalise_labels(dlabels)
        if pairs is not None:
            new_pairs = _rewrite_pairs(pairs, prefix)
            deploy["labels"] = _emit_labels(new_pairs, was_dict)


def process(compose_path: str, output_path: str, prefix: str) -> None:
    with open(compose_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    if isinstance(data, dict) and isinstance(data.get("services"), dict):
        for _, svc in data["services"].items():
            if isinstance(svc, dict):
                _process_service(svc, prefix)

    with open(output_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False)


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.stderr.write(
            "Usage: process_qa_labels.py <input_compose> <output_compose> <prefix>\n"
        )
        sys.exit(2)
    process(sys.argv[1], sys.argv[2], sys.argv[3])
