"""Trusted composition root for optional domain workflows; no business ID switches."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.behavior import BusinessBehavior
from app.businesses import BusinessConfig

if TYPE_CHECKING:
    from app.agent import GeminiAgent


@dataclass(frozen=True)
class BehaviorDefinition:
    required_tools: frozenset[str]
    factory: Callable[[GeminiAgent], BusinessBehavior]


class BehaviorRegistry:
    def __init__(self, definitions: tuple[BehaviorDefinition, ...] = ()) -> None:
        self.definitions = definitions

    def for_business(self, business: BusinessConfig, runtime: GeminiAgent) -> BusinessBehavior:
        for definition in self.definitions:
            if definition.required_tools <= set(business.enabled_tools):
                return definition.factory(runtime)
        return BusinessBehavior(runtime)


def build_behaviors() -> BehaviorRegistry:
    from app.membership_behavior import MembershipBehavior
    from app.training_behavior import TrainingBehavior

    return BehaviorRegistry(
        (
            BehaviorDefinition(frozenset({"getCourseDetails"}), TrainingBehavior),
            BehaviorDefinition(frozenset({"getMembershipDetails"}), MembershipBehavior),
        )
    )
