"""Small strict OpenAPI schema-to-TypeScript exporter for this repository's schema subset."""

import json
from typing import Any


def type_expression(schema: dict[str, Any]) -> str:
    if "$ref" in schema:
        return str(schema["$ref"].rsplit("/", 1)[1])
    for key, separator in (("anyOf", " | "), ("oneOf", " | "), ("allOf", " & ")):
        if key in schema:
            return "(" + separator.join(type_expression(s) for s in schema[key]) + ")"
    if "const" in schema:
        return json.dumps(schema["const"])
    if "enum" in schema:
        return " | ".join(json.dumps(v) for v in schema["enum"])
    kind = schema.get("type")
    if kind == "null":
        return "null"
    if kind in {"integer", "number"}:
        return "number"
    if kind in {"boolean", "string"}:
        return str(kind)
    if kind == "array":
        return "Array<" + type_expression(schema.get("items", {})) + ">"
    if kind == "object" or "properties" in schema:
        required = schema.get("required", [])
        fields = [
            json.dumps(k) + ("" if k in required else "?") + ": " + type_expression(v)
            for k, v in schema.get("properties", {}).items()
        ]
        additional = schema.get("additionalProperties")
        if additional is True:
            fields.append("[key: string]: unknown")
        elif isinstance(additional, dict):
            fields.append("[key: string]: " + type_expression(additional))
        return "{ " + "; ".join(fields) + " }" if fields else "Record<string, never>"
    if not schema or set(schema) <= {"title", "description", "default", "examples"}:
        return "unknown"
    raise ValueError("Unsupported OpenAPI schema; extend the exporter explicitly")


def export_types(openapi: dict[str, Any]) -> str:
    lines = ["// Generated from backend OpenAPI by app.export_contracts. Do not edit."]
    for name, schema in sorted(openapi["components"]["schemas"].items()):
        lines.append(f"export type {name} = {type_expression(schema)};")
    return "\n".join(lines) + "\n"
