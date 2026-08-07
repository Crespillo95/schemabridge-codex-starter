"""Shared normalized physical value types without registry/catalog coupling."""

from enum import StrEnum


class PhysicalValueType(StrEnum):
    STRING = "string"
    INTEGER = "integer"
    FLOAT = "float"
    DECIMAL = "decimal"
    BOOLEAN = "boolean"
    DATE = "date"
    TIMESTAMP = "timestamp"
    BINARY = "binary"
    STRUCT = "struct"
    ARRAY = "array"
    UNKNOWN = "unknown"


__all__ = ["PhysicalValueType"]
