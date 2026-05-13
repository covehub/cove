from __future__ import annotations

import yaml


class _LiteralStringDumper(yaml.SafeDumper):
    pass


def _represent_string(
    dumper: yaml.SafeDumper,
    value: str,
):  # pragma: no cover - exercised via callers
    style = "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_LiteralStringDumper.add_representer(str, _represent_string)


def dump_yaml(payload: object) -> str:
    return yaml.dump(
        payload,
        Dumper=_LiteralStringDumper,
        sort_keys=False,
        default_flow_style=False,
    )
