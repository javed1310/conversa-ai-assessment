"""Single-authority file configuration; no database copy and no tool execution."""

import json
from collections.abc import Collection
from pathlib import Path
from types import MappingProxyType
from typing import Annotated

from pydantic import Field, ValidationError, model_validator

from app.contracts import Contract, Identifier, Text, ToolName
from app.settings import ConfigurationError


class BusinessConfig(Contract):
    business_id: Identifier
    business_name: Text
    agent_role: Text
    instructions: tuple[Text, ...] = Field(min_length=1)
    knowledge_base_id: Identifier
    enabled_tools: tuple[ToolName, ...] = Field(min_length=1)
    config_version: Annotated[int, Field(strict=True, ge=1)]

    @model_validator(mode="after")
    def unique_tools(self) -> "BusinessConfig":
        if len(set(self.enabled_tools)) != len(self.enabled_tools):
            raise ValueError("Enabled tools must be unique")
        return self


class BusinessSummary(Contract):
    business_id: Identifier
    business_name: Text
    agent_role: Text


class BusinessList(Contract):
    businesses: tuple[BusinessSummary, ...]


class BusinessRegistry:
    def __init__(self, configurations: tuple[BusinessConfig, ...]) -> None:
        values = {item.business_id: item for item in configurations}
        if not values or len(values) != len(configurations):
            raise ConfigurationError("Business configuration is empty or has duplicate IDs.")
        if len({item.knowledge_base_id for item in configurations}) != len(configurations):
            raise ConfigurationError("Business knowledge-base IDs must be isolated.")
        self._values = MappingProxyType(values)

    @classmethod
    def load(
        cls, root: Path, *, registered_tools: Collection[str] | None = None
    ) -> "BusinessRegistry":
        try:
            paths = sorted(root.glob("*/config.json"))
            configurations = tuple(
                BusinessConfig.model_validate_json(path.read_text(encoding="utf-8"))
                for path in paths
            )
            if registered_tools is None:
                from app.tools import build_tool_registry

                registered_tools = build_tool_registry().names
            if any(
                name not in registered_tools
                for config in configurations
                for name in config.enabled_tools
            ):
                raise ValueError("Business enables an unregistered tool")
            for path, configuration in zip(paths, configurations, strict=True):
                if path.parent.name != configuration.business_id:
                    raise ValueError("Directory must match business ID")
            return cls(configurations)
        except (OSError, ValidationError, ValueError, json.JSONDecodeError):
            raise ConfigurationError(
                "Invalid business configuration. Check business config files and known tool names."
            ) from None

    def get(self, business_id: str) -> BusinessConfig | None:
        return self._values.get(business_id)

    def configurations(self) -> tuple[BusinessConfig, ...]:
        return tuple(self._values.values())

    def summaries(self) -> BusinessList:
        return BusinessList(
            businesses=tuple(
                BusinessSummary(
                    business_id=item.business_id,
                    business_name=item.business_name,
                    agent_role=item.agent_role,
                )
                for item in self._values.values()
            )
        )
