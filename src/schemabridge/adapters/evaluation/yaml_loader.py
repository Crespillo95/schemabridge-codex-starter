"""Strict YAML loading shared by evaluation evidence readers."""

from __future__ import annotations

import yaml
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: _UniqueKeyLoader,
    node: MappingNode,
    deep: bool = False,
) -> dict[object, object]:
    loader.flatten_mapping(node)
    result: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found duplicate key",
                key_node.start_mark,
            )
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def load_unique_yaml(value: str) -> object:
    """Load safe YAML and reject duplicate mapping keys at every depth."""

    try:
        return yaml.load(value, Loader=_UniqueKeyLoader)
    except yaml.YAMLError:
        raise ValueError("evaluation YAML is invalid or contains duplicate keys") from None


__all__ = ["load_unique_yaml"]
