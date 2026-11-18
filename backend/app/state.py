"""Owning-turn state updates with durable staged checkpoints and session CAS receipts."""

import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import ValidationError
from pymongo import ReturnDocument

from app.contracts import (
    ActionResult,
    CallbackIntent,
    Contract,
    CustomerEvidence,
    CustomerFields,
    CustomerState,
    DisplayedCourse,
    ExecutionContext,
    Session,
    StateReceipt,
)
from app.customer_values import explicit_clear, supports_value
from app.dialogue import (
    affirmation,
    callback_status_question,
    decline,
    expresses_discussed_interest,
    safe_discussion_choice,
    starts_discussed_enquiry,
)
from app.generation import EvidencedProposal
from app.intent import callback_request, callback_revoked
from app.persistence_models import Course, StateCheckpoint, Turn
from app.repositories import RecordNotFound, Repository, parse, stored
from app.tools import CourseSearchResult


class StateFailure(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__("Customer state update could not complete.")
        self.code = code


async def callback_permission(repo: Repository, context: ExecutionContext, turn: Turn) -> bool:
    """Explicit request or affirmation of the immediately preceding owned callback offer."""
    if callback_status_question(turn.input_text):
        return False
    if callback_request(turn.input_text):
        return True
    if not affirmation(turn.input_text):
        return False
    return await callback_offer_pending(repo, context, turn)


async def callback_offer_pending(repo: Repository, context: ExecutionContext, turn: Turn) -> bool:
    """Only a persisted server offer immediately preceding this customer's turn counts."""
    prior = await repo.db.turns.find_one(
        repo.owner(context) | {"sequence": {"$lt": turn.sequence}, "status": "completed"},
        {"agent_trace.next_step": 1, "agent_trace.outcome": 1},
        sort=[("sequence", -1)],
    )
    trace = (prior or {}).get("agent_trace") or {}
    if trace.get("next_step") != "offer_callback" or trace.get("outcome") != "clarify":
        return False
    try:
        await repo.lead_for_session(context)
    except RecordNotFound:
        return False
    return True


async def discussed_course(repo: Repository, context: ExecutionContext, turn: Turn) -> str | None:
    previous = await repo.db.turns.find_one(
        repo.owner(context) | {"sequence": {"$lt": turn.sequence}, "status": "completed"},
        {"agent_trace.discussed_course_id": 1},
        sort=[("sequence", -1)],
    )
    trace = (previous or {}).get("agent_trace") or {}
    if "discussed_course_id" in trace:
        value = trace["discussed_course_id"]
        return value if isinstance(value, str) else None
    # Legacy turns have no discussion field. Recover only an unambiguous rendered course.
    catalog = await repo.courses()
    cursor = (
        repo.db.turns.find(
            repo.owner(context) | {"sequence": {"$lt": turn.sequence}, "status": "completed"},
            {"output.reply": 1},
        )
        .sort("sequence", -1)
        .limit(12)
    )
    async for prior in cursor:
        text = (prior.get("output") or {}).get("reply", "")
        matches = [
            c.course_id
            for c in catalog
            if any(
                re.search(r"\b" + re.escape(value) + r"\b", text, re.I)
                for value in (c.course_id, c.name)
            )
        ]
        if matches:
            return matches[0] if len(matches) == 1 else None
    return None


class Qualification(Contract):
    missing: tuple[str, ...]
    needs_clarification: bool
    can_create_lead: bool


def qualification(state: CustomerState) -> Qualification:
    missing = [field for field in ("name", "phone") if getattr(state.fields, field) is None]
    if state.fields.course_id is None and state.fields.stated_interest is None:
        missing.append("course_or_interest")
    conflict = bool(state.selection_conflicts)
    return Qualification(
        missing=tuple(missing),
        needs_clarification=conflict,
        can_create_lead=not missing and "missing_course" not in state.selection_conflicts,
    )


class StateService:
    def __init__(self, repository: Repository) -> None:
        self.repo = repository

    async def _consistent(self, state: CustomerState) -> CustomerState:
        conflicts: list[Literal["budget", "schedule", "full", "missing_course"]] = []
        if state.fields.course_id is not None:
            try:
                course = await self.repo.course(state.fields.course_id)
            except RecordNotFound:
                conflicts.append("missing_course")
            else:
                if state.fields.budget is not None and course.fee > state.fields.budget:
                    conflicts.append("budget")
                if state.fields.schedule is not None and course.schedule != state.fields.schedule:
                    conflicts.append("schedule")
                if course.availability == "full":
                    conflicts.append("full")
        return state.model_copy(update={"selection_conflicts": tuple(conflicts)})

    async def view(self, context: ExecutionContext) -> CustomerState:
        return await self._consistent((await self.repo.session(context)).state)

    async def _commit(
        self,
        context: ExecutionContext,
        stage: str,
        payload: object,
        transform: Callable[[CustomerState], Awaitable[CustomerState]],
    ) -> Session:
        turn = await self.repo.turn(context)
        if turn.status != "processing":
            raise StateFailure("TURN_NOT_PROCESSING")
        current = await self.repo.session(context)
        if current.state_sequence > turn.sequence:
            raise StateFailure("STALE_TURN")
        fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest()
        if current.state_turn_id == turn.turn_id:
            prior = next((step for step in current.state_steps if step.stage == stage), None)
            if prior:
                if prior.fingerprint != fingerprint:
                    raise StateFailure("CHECKPOINT_CONFLICT")
                return current
        checkpoint = next((item for item in turn.state_checkpoints if item.stage == stage), None)
        if checkpoint is None:
            if len(turn.state_checkpoints) >= 8:
                raise StateFailure("CHECKPOINT_LIMIT")
            after = await self._consistent(await transform(current.state))
            after = CustomerState.model_validate(after.model_dump(mode="python"))
            checkpoint = StateCheckpoint(
                stage=stage,
                fingerprint=fingerprint,
                base_revision=current.state_revision,
                request_json=json.dumps(payload, sort_keys=True, allow_nan=False),
                after=after,
            )
            key: dict[str, Any] = self.repo.owner(context) | {
                "turn_id": turn.turn_id,
                "status": "processing",
                "state_checkpoints.stage": {"$ne": stage},
                "$expr": {"$lt": [{"$size": {"$ifNull": ["$state_checkpoints", []]}}, 8]},
            }
            if context.lease_token is not None:
                key["processing_token"] = context.lease_token
            prepared = await self.repo.db.turns.update_one(
                key, {"$push": {"state_checkpoints": stored(checkpoint)}}
            )
            if prepared.modified_count != 1:
                raced = await self.repo.turn(context)
                checkpoint = next(
                    (item for item in raced.state_checkpoints if item.stage == stage), None
                )
                if checkpoint is None:
                    raise StateFailure("STATE_CONFLICT")
        if checkpoint.fingerprint != fingerprint:
            raise StateFailure("CHECKPOINT_CONFLICT")
        receipts = current.state_steps if current.state_turn_id == turn.turn_id else ()
        revision_filter: dict[str, Any] = {"state_revision": checkpoint.base_revision}
        if checkpoint.base_revision == 0:
            revision_filter = {
                "$or": [{"state_revision": 0}, {"state_revision": {"$exists": False}}]
            }
        key = self.repo.owner(context) | {
            "$and": [
                revision_filter,
                {
                    "$or": [
                        {"state_sequence": {"$lte": turn.sequence}},
                        {"state_sequence": {"$exists": False}},
                    ]
                },
            ]
        }
        if context.lease_token is not None:
            key |= {
                "_pending_token": context.lease_token,
                "_pending_expires_at": {"$gt": datetime.now(UTC)},
            }
        saved = await self.repo.db.sessions.find_one_and_update(
            key,
            {
                "$set": {
                    "state": stored(checkpoint.after),
                    "state_revision": checkpoint.base_revision + 1,
                    "state_sequence": turn.sequence,
                    "state_turn_id": turn.turn_id,
                    "state_steps": [
                        stored(step)
                        for step in (*receipts, StateReceipt(stage=stage, fingerprint=fingerprint))
                    ],
                    "updated_at": datetime.now(UTC),
                }
            },
            return_document=ReturnDocument.AFTER,
        )
        if saved is None:
            latest = await self.repo.session(context)
            if latest.state_turn_id == turn.turn_id and any(
                step.stage == stage and step.fingerprint == fingerprint
                for step in latest.state_steps
            ):
                return latest
            raise StateFailure("STATE_CONFLICT")
        return parse(Session, saved)

    async def resume(self, context: ExecutionContext) -> Session:
        """Replay prepared state checkpoints without asking a model to regenerate a proposal."""
        turn = await self.repo.turn(context)

        async def never_recompute(_state: CustomerState) -> CustomerState:
            raise StateFailure("CHECKPOINT_MISSING")

        for checkpoint in turn.state_checkpoints:
            await self._commit(
                context, checkpoint.stage, json.loads(checkpoint.request_json), never_recompute
            )
        return await self.repo.session(context)

    async def apply(self, context: ExecutionContext, proposed: EvidencedProposal) -> Session:
        try:
            proposed = EvidencedProposal.model_validate(
                proposed.model_dump(mode="python", exclude_unset=True)
            )
        except ValidationError:
            raise StateFailure("INVALID_PATCH") from None
        turn = await self.repo.turn(context)
        changes = proposed.proposal.set_fields.model_dump(exclude_unset=True)
        touched = set(changes) | set(proposed.proposal.clear_fields)
        if touched != set(proposed.evidence):
            raise StateFailure("INVALID_PROVENANCE")
        for field, proof in proposed.evidence.items():
            if proof.turn_id != turn.turn_id or proof.excerpt not in turn.input_text:
                raise StateFailure("INVALID_PROVENANCE")
            if field in changes and not supports_value(field, changes[field], proof.excerpt):
                raise StateFailure("INVALID_PROVENANCE")
            if (
                field == "phone"
                and field in changes
                and not supports_value("phone", changes[field], turn.input_text)
            ):
                raise StateFailure("INVALID_PROVENANCE")
            if field in proposed.proposal.clear_fields and not (
                explicit_clear(field, proof.excerpt) and explicit_clear(field, turn.input_text)
            ):
                raise StateFailure("CLEAR_REQUIRES_INTENT")
        if "course_id" in changes:
            await self.repo.course(str(changes["course_id"]))

        async def transform(state: CustomerState) -> CustomerState:
            values = state.fields.model_dump()
            evidence = dict(state.provenance)
            values.update(changes)
            for field in proposed.proposal.clear_fields:
                values[field] = None
                evidence.pop(field, None)
            evidence.update(
                {key: value for key, value in proposed.evidence.items() if key in changes}
            )
            return state.model_copy(
                update={"fields": CustomerFields.model_validate(values), "provenance": evidence}
            )

        return await self._commit(
            context,
            "customer",
            proposed.model_dump(
                mode="json",
                exclude_unset=True,
                exclude={"course_question", "enquiry_request", "social_reply"},
            ),
            transform,
        )

    async def record_displayed(
        self, context: ExecutionContext, call_id: str, ordered_ids: tuple[str, ...]
    ) -> Session:
        await self.repo.turn(context)
        document = await self.repo.db.tool_executions.find_one(
            {
                "business_id": context.business_id,
                "session_id": context.session_id,
                "turn_id": context.turn_id,
                "call_id": call_id,
                "status": "completed",
            }
        )
        if document is None:
            raise StateFailure("DISPLAY_REQUIRES_TOOL_RESULT")
        trace = ActionResult.model_validate(document["trace"])
        if trace.result.status != "success" or trace.result.tool != "searchCourses":
            raise StateFailure("DISPLAY_REQUIRES_TOOL_RESULT")
        result = CourseSearchResult.model_validate(trace.result.data)
        catalog = {course.course_id: course for course in result.courses}
        if (
            len(ordered_ids) > 20
            or len(set(ordered_ids)) != len(ordered_ids)
            or not set(ordered_ids) <= catalog.keys()
        ):
            raise StateFailure("INVALID_DISPLAY")
        displayed = tuple(
            DisplayedCourse(course_id=key, name=catalog[key].name) for key in ordered_ids
        )

        async def transform(state: CustomerState) -> CustomerState:
            return state.model_copy(update={"last_displayed_courses": displayed})

        return await self._commit(
            context, "display", {"callId": call_id, "order": ordered_ids}, transform
        )

    async def resolve_reference(self, context: ExecutionContext, excerpt: str) -> Course:
        turn = await self.repo.turn(context)
        if excerpt not in turn.input_text or re.search(
            r"\b(not|neither|don't)\b", turn.input_text, re.I
        ):
            raise StateFailure("REFERENCE_AMBIGUOUS")
        state = (await self.repo.session(context)).state
        ordinals = {
            "first": 1,
            "1st": 1,
            "second": 2,
            "2nd": 2,
            "third": 3,
            "3rd": 3,
            "fourth": 4,
            "4th": 4,
        }
        words = re.findall(r"\b(first|1st|second|2nd|third|3rd|fourth|4th)\b", excerpt.casefold())
        direct = [
            course.course_id
            for course in await self.repo.courses()
            if re.search(r"\b" + re.escape(course.course_id) + r"\b", excerpt, re.I)
        ]
        if len(words) + len(direct) != 1:
            raise StateFailure("REFERENCE_AMBIGUOUS")
        if direct:
            return await self.repo.course(direct[0])
        ordinal = ordinals[words[0]]
        if ordinal > len(state.last_displayed_courses):
            raise StateFailure("REFERENCE_UNAVAILABLE")
        return await self.repo.course(state.last_displayed_courses[ordinal - 1].course_id)

    async def select_reference(self, context: ExecutionContext, excerpt: str) -> Session:
        course = await self.resolve_reference(context, excerpt)
        return await self._select_course(context, excerpt, course)

    async def select_discussed(
        self,
        context: ExecutionContext,
        excerpt: str,
        course_id: str,
        *,
        reviewed_choice: bool = False,
    ) -> Session:
        turn = await self.repo.turn(context)
        discussed = await discussed_course(self.repo, context, turn)
        literal_choice = reviewed_choice and bool(
            re.search(r"\b" + re.escape(course_id) + r"\b", excerpt, re.I)
        )
        if (
            excerpt != turn.input_text
            or not (
                starts_discussed_enquiry(excerpt)
                or expresses_discussed_interest(excerpt)
                or (reviewed_choice and safe_discussion_choice(excerpt))
            )
            or (discussed != course_id and not literal_choice)
        ):
            raise StateFailure("REFERENCE_AMBIGUOUS")
        return await self._select_course(context, excerpt, await self.repo.course(course_id))

    async def _select_course(
        self, context: ExecutionContext, excerpt: str, course: Course
    ) -> Session:

        async def transform(state: CustomerState) -> CustomerState:
            values = state.fields.model_dump() | {"course_id": course.course_id}
            provenance = dict(state.provenance)
            provenance["course_id"] = CustomerEvidence(turn_id=context.turn_id, excerpt=excerpt)
            return state.model_copy(
                update={"fields": CustomerFields.model_validate(values), "provenance": provenance}
            )

        return await self._commit(
            context, "selection", {"excerpt": excerpt, "courseId": course.course_id}, transform
        )

    async def record_callback_intent(self, context: ExecutionContext, excerpt: str) -> Session:
        turn = await self.repo.turn(context)
        if (
            excerpt not in turn.input_text
            or not await callback_permission(self.repo, context, turn)
            or not (callback_request(excerpt) or excerpt.strip() == turn.input_text.strip())
        ):
            raise StateFailure("CALLBACK_REQUIRES_INTENT")

        async def transform(state: CustomerState) -> CustomerState:
            return state.model_copy(
                update={
                    "callback_intent": CallbackIntent(
                        requested=True,
                        evidence=CustomerEvidence(turn_id=context.turn_id, excerpt=excerpt),
                    )
                }
            )

        return await self._commit(context, "callback", {"excerpt": excerpt}, transform)

    async def revoke_callback_intent(self, context: ExecutionContext, excerpt: str) -> Session:
        turn = await self.repo.turn(context)
        offered_decline = (
            excerpt.strip() == turn.input_text.strip()
            and decline(turn.input_text)
            and await callback_offer_pending(self.repo, context, turn)
        )
        if (
            excerpt not in turn.input_text
            or not (offered_decline or callback_revoked(turn.input_text))
            or not (offered_decline or callback_revoked(excerpt))
        ):
            raise StateFailure("CALLBACK_REQUIRES_INTENT")

        async def transform(state: CustomerState) -> CustomerState:
            return state.model_copy(update={"callback_intent": None})

        return await self._commit(context, "callback", {"revoke": excerpt}, transform)
