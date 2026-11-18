"""Customer-authorized lead/callback operations; no model or channel authority."""

from typing import cast

from pydantic import BaseModel

from app.contracts import CallbackArguments, CustomerField, ExecutionContext, LeadInput
from app.customer_values import supports_value
from app.intent import callback_request as callback_request
from app.intent import callback_revoked as callback_revoked
from app.persistence_models import LeadRecord
from app.repositories import RecordNotFound, Repository
from app.state import StateService, callback_permission, qualification

WRITE_TOOLS = frozenset({"createLead", "requestCallback"})


class ActionRejected(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__("Business action is not authorized by customer state.")
        self.code = code


class LeadActions:
    def __init__(self, repository: Repository) -> None:
        self.repo = repository

    async def existing(self, context: ExecutionContext) -> LeadRecord | None:
        try:
            return await self.repo.lead_for_session(context)
        except RecordNotFound:
            return None

    async def candidate(self, context: ExecutionContext) -> LeadInput:
        turn = await self.repo.turn(context)
        if turn.status != "processing":
            raise ActionRejected("CONFLICT")
        state = await StateService(self.repo).view(context)
        status = qualification(state)
        if status.missing:
            raise ActionRejected("QUALIFICATION_REQUIRED")
        if "missing_course" in state.selection_conflicts:
            raise ActionRejected("PREFERENCES_CONFLICT")
        for field, value in state.fields.model_dump().items():
            if value is None:
                continue
            proof = state.provenance[cast(CustomerField, field)]
            evidence_turn = await self.repo.turn(
                context.model_copy(update={"turn_id": proof.turn_id})
            )
            if (
                evidence_turn.sequence > turn.sequence
                or proof.excerpt not in evidence_turn.input_text
            ):
                raise ActionRejected("INVALID_PROVENANCE")
            supported = supports_value(field, value, proof.excerpt)
            if field == "course_id" and not supported:
                supported = any(
                    point.stage == "selection"
                    and point.after.fields.course_id == value
                    and point.after.provenance.get("course_id") == proof
                    for point in evidence_turn.state_checkpoints
                )
            if not supported:
                raise ActionRejected("INVALID_PROVENANCE")
        return LeadInput.model_validate(state.fields.model_dump())

    async def authorize(self, name: str, context: ExecutionContext, arguments: BaseModel) -> None:
        if (await self.repo.turn(context)).status != "processing":
            raise ActionRejected("CONFLICT")
        existing = await self.existing(context)
        if name == "createLead":
            if existing is not None:
                return
            candidate = await self.candidate(context)
            proposed = LeadInput.model_validate(arguments.model_dump())
            for field, value in proposed.model_dump(exclude_none=True).items():
                if value != getattr(candidate, field):
                    raise ActionRejected("INVALID_PROVENANCE")
            return
        args = CallbackArguments.model_validate(arguments.model_dump())
        if existing is None or existing.lead_id != args.lead_id:
            raise ActionRejected("NOT_FOUND")
        session = await self.repo.session(context)
        intent = session.state.callback_intent
        if intent is None:
            raise ActionRejected("CALLBACK_INTENT_REQUIRED")
        evidence = await self.repo.turn(
            context.model_copy(update={"turn_id": intent.evidence.turn_id})
        )
        current = await self.repo.turn(context)
        if (
            evidence.sequence > current.sequence
            or intent.evidence.excerpt not in evidence.input_text
            or not await callback_permission(self.repo, context, evidence)
        ):
            raise ActionRejected("CALLBACK_INTENT_REQUIRED")
        if existing.customer.model_dump() != session.state.fields.model_dump():
            raise ActionRejected("SAVED_DETAILS_CHANGED")

    async def create(self, context: ExecutionContext, arguments: LeadInput) -> LeadRecord:
        await self.authorize("createLead", context, arguments)
        existing = await self.existing(context)
        if existing is not None:
            return existing
        candidate = await self.candidate(context)
        return await self.repo.create_lead(context, candidate)

    async def callback(self, context: ExecutionContext, arguments: CallbackArguments) -> LeadRecord:
        await self.authorize("requestCallback", context, arguments)
        intent = (await self.repo.session(context)).state.callback_intent
        assert intent is not None
        return await self.repo.request_callback(context, arguments.lead_id, intent)

    async def reconcile(
        self, name: str, context: ExecutionContext, arguments: BaseModel
    ) -> LeadRecord | None:
        lead = await self.existing(context)
        if lead is None:
            return None
        if name == "requestCallback":
            requested = CallbackArguments.model_validate(arguments.model_dump())
            if requested.lead_id != lead.lead_id or lead.callback_request_id is None:
                return None
        return lead

    async def receipt(
        self, name: str, context: ExecutionContext, lead: LeadRecord
    ) -> dict[str, object]:
        state = (await self.repo.session(context)).state
        data: dict[str, object] = {
            "leadId": lead.lead_id,
            "activeDetailsDiffer": lead.customer.model_dump() != state.fields.model_dump(),
        }
        if name == "requestCallback":
            if lead.callback_request_id is None or lead.callback_requested_at is None:
                raise ActionRejected("OUTCOME_UNKNOWN")
            data.update(
                requested=True,
                requestId=lead.callback_request_id,
                requestedAt=lead.callback_requested_at.isoformat(),
            )
        return data
