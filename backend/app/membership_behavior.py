"""Membership references use owning-session displayed receipts, never course state."""

import re
from uuid import uuid4

from pydantic import ValidationError

from app.agent_context import PreparedTurn, TurnWork
from app.behavior import BusinessBehavior
from app.contracts import ActionResult, ChatResult, CustomerState, ExecutionContext
from app.generation import ReplySelection
from app.persistence_models import Membership
from app.state import StateService
from app.tools import MembershipResult, ToolCall
from app.turn_context import TurnDialogue

ORDINALS = {
    "first": 0,
    "1st": 0,
    "second": 1,
    "2nd": 1,
    "third": 2,
    "3rd": 2,
    "fourth": 3,
    "4th": 3,
}


class MembershipBehavior(BusinessBehavior):
    instructions = (
        "Use owning-session membershipReferences for references such as first/second one "
        "and discussedMembershipId for a follow-up about it. These identify records, not "
        "current prices. Use getMembershipDetails with membershipID for fresh facts. "
        "A membership choice expresses interest only; never claim enrollment, payment or booking."
    )

    async def _references(
        self, context: ExecutionContext, service: StateService, dialogue: TurnDialogue
    ) -> tuple[tuple[Membership, ...], str | None]:
        records = (
            await service.repo.db.turns.find(
                service.repo.owner(context)
                | {
                    "status": "completed",
                    "sequence": {"$lt": dialogue.turn.sequence},
                    "output.actions.result.tool": "getMembershipDetails",
                },
                {"output": 1, "_id": 0},
            )
            .sort("sequence", -1)
            .limit(12)
            .to_list(12)
        )
        latest: tuple[Membership, ...] | None = None
        focus = None
        for record in records:
            output = record.get("output")
            if not isinstance(output, dict):
                continue
            for raw in output.get("actions", []):
                try:
                    action = ActionResult.model_validate(raw)
                    if (
                        action.result.tool != "getMembershipDetails"
                        or action.result.status != "success"
                    ):
                        continue
                    members = MembershipResult.model_validate(action.result.data).memberships
                except ValidationError:
                    continue
                if any(member.business_id != context.business_id for member in members):
                    continue
                positions = [output.get("reply", "").find(member.name) for member in members]
                if any(position < 0 for position in positions) or positions != sorted(positions):
                    continue  # Do not guess an ordinal from a differently rendered list.
                if latest is None:
                    latest = members
                    if len(members) != 1:
                        return members, None
                    focus = members[0].membership_id
                elif len(members) > 1 and any(member.membership_id == focus for member in members):
                    return members, focus
        return latest or (), focus

    async def context_fields(
        self,
        context: ExecutionContext,
        service: StateService,
        state: CustomerState,
        dialogue: TurnDialogue,
    ) -> dict[str, object]:
        options, focus = await self._references(context, service, dialogue)
        return {
            "membershipReferences": [
                {"membershipId": member.membership_id, "name": member.name} for member in options
            ],
            "discussedMembershipId": focus,
        }

    async def prepare(
        self,
        context: ExecutionContext,
        service: StateService,
        work: TurnWork,
        dialogue: TurnDialogue,
    ) -> PreparedTurn:
        state = await service.view(context)
        match = re.fullmatch(
            r"\s*(?:(?:i(?:'ll| will)?|i would like to)\s+"
            r"(?:choose|select|take|prefer|want)\s+)?(?:the\s+)?"
            r"(first|second|third|fourth|1st|2nd|3rd|4th)"
            r"(?:\s+(?:one|option|plan|membership))?\s*[.!]?\s*",
            dialogue.turn.input_text,
            re.I,
        )
        field = None
        if re.fullmatch(
            r"\s*(?:how much (?:does (?:it|this plan|that plan) cost|"
            r"is (?:it|this plan|that plan))|"
            r"what(?:'s| is) (?:its|the) (?:price|fee|cost))\s*[?.!]*\s*",
            dialogue.turn.input_text,
            re.I,
        ):
            field = "fee"
        elif re.fullmatch(
            r"\s*(?:how long (?:does (?:it|this plan|that plan) last|"
            r"is (?:it|this plan|that plan)(?: valid)?(?: for)?)|"
            r"what(?:'s| is) (?:its|the) duration)\s*[?.!]*\s*",
            dialogue.turn.input_text,
            re.I,
        ):
            field = "duration"
        if match is None and field is None:
            return PreparedTurn(state)
        options, focus = await self._references(context, service, dialogue)
        membership_id = focus
        if match is not None:
            index = ORDINALS[match[1].lower()]
            membership_id = options[index].membership_id if index < len(options) else None
        if membership_id is None:
            return PreparedTurn(
                state,
                ChatResult(
                    message_id=dialogue.turn.message_id,
                    session_id=context.session_id,
                    reply="Which membership do you mean? "
                    "Please ask to see the options or name the plan.",
                ),
                "clarify",
            )
        await self._operate(
            context,
            ToolCall(
                call_id=str(uuid4()),
                name="getMembershipDetails",
                arguments={"membershipID": membership_id},
            ),
            service,
            work,
        )
        work.response_locked = True
        if field is not None and work.actions[-1].result.status == "success":
            members = MembershipResult.model_validate(work.actions[-1].result.data).memberships
            if len(members) == 1 and members[0].membership_id == membership_id:
                member = members[0]
                unit = "month" if member.duration_months == 1 else "months"
                detail = (
                    f"costs INR {member.fee:g} for {member.duration_months} {unit}."
                    if field == "fee"
                    else f"lasts {member.duration_months} {unit}."
                )
                return PreparedTurn(
                    state,
                    ChatResult(
                        message_id=dialogue.turn.message_id,
                        session_id=context.session_id,
                        reply=f"{member.name} {detail}",
                        actions=tuple(work.actions),
                    ),
                    "evidence",
                )
        reply = await self._assemble(
            context,
            service,
            state,
            ReplySelection(
                kind="evidence",
                evidence_keys=tuple(item.key for item in work.evidence),
                clarification="none",
            ),
            work,
            dialogue,
        )
        return PreparedTurn(state, reply, "evidence")
