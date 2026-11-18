"""Channel-independent tool catalog, validated read handlers and durable execution traces."""

import asyncio
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import AwareDatetime, Field, TypeAdapter, ValidationError
from pymongo.errors import DuplicateKeyError, PyMongoError

from app.businesses import BusinessConfig, BusinessRegistry
from app.contracts import (
    ActionResult,
    CallbackArguments,
    Content,
    Contract,
    CourseDetailsArguments,
    ExecutionContext,
    Identifier,
    LeadInput,
    SearchCoursesArguments,
    ToolFailure,
    ToolName,
    ToolSuccess,
)
from app.database import Database
from app.persistence_models import Course, Membership
from app.provider_control import can_start_write
from app.repositories import RecordConflict, RecordNotFound, Repository

logger = logging.getLogger(__name__)


class MembershipArguments(Contract):
    membership_id: Identifier | None = Field(default=None, alias="membershipID")


class CourseSearchResult(Contract):
    courses: tuple[Course, ...]


class CourseDetailsResult(Contract):
    course: Course


class MembershipResult(Contract):
    memberships: tuple[Membership, ...]


class LeadReceipt(Contract):
    lead_id: Identifier
    active_details_differ: bool = False


class CallbackReceipt(LeadReceipt):
    requested: Literal[True]
    request_id: Identifier
    requested_at: AwareDatetime


class ToolCall(Contract):
    call_id: Identifier
    name: Identifier
    arguments: dict[str, object]


@dataclass(frozen=True)
class ToolDefinition[Arguments: Contract, Result: Contract]:
    name: ToolName
    description: str
    arguments: type[Arguments]
    result: type[Result]
    handler: Callable[[Repository, ExecutionContext, Arguments], Awaitable[Result]] | None
    deferred: bool = False
    presentation: Callable[[Result], str] | None = None
    current_facts: bool = False

    def __post_init__(self) -> None:
        TypeAdapter(ToolName).validate_python(self.name)
        if (
            self.name
            not in {
                "searchCourses",
                "getCourseDetails",
                "createLead",
                "requestCallback",
                "getMembershipDetails",
            }
            and self.presentation is None
        ):
            raise ValueError("A new tool must register its result presentation")
        if not self.description.strip():
            raise ValueError("Tool description is required")
        if self.handler is None and not (
            self.deferred and self.name in {"createLead", "requestCallback"}
        ):
            raise ValueError("Executable handler required for an active tool")
        if self.handler is not None and self.deferred:
            raise ValueError("Executable handler cannot be deferred")


async def search_courses(
    repository: Repository,
    context: ExecutionContext,
    arguments: SearchCoursesArguments,
) -> CourseSearchResult:
    await repository.turn(context)
    return CourseSearchResult(courses=await repository.search_courses(arguments))


async def course_details(
    repository: Repository,
    context: ExecutionContext,
    arguments: CourseDetailsArguments,
) -> CourseDetailsResult:
    await repository.turn(context)
    return CourseDetailsResult(course=await repository.course(arguments.course_id))


async def membership_details(
    repository: Repository,
    context: ExecutionContext,
    arguments: MembershipArguments,
) -> MembershipResult:
    await repository.turn(context)
    members = (
        (await repository.membership(arguments.membership_id),)
        if arguments.membership_id is not None
        else await repository.memberships()
    )
    return MembershipResult(memberships=members)


def render_memberships(result: MembershipResult) -> str:
    return (
        "\n".join(
            f"{member.name}: INR {member.fee:g} for {member.duration_months} months. "
            f"{member.description}"
            for member in result.memberships
        )
        if result.memberships
        else "No matching membership was found."
    )


async def create_lead(
    repository: Repository, context: ExecutionContext, arguments: LeadInput
) -> LeadReceipt:
    from app.actions import LeadActions

    service = LeadActions(repository)
    lead = await service.create(context, arguments)
    return LeadReceipt.model_validate(await service.receipt("createLead", context, lead))


async def request_callback(
    repository: Repository, context: ExecutionContext, arguments: CallbackArguments
) -> CallbackReceipt:
    from app.actions import LeadActions

    service = LeadActions(repository)
    lead = await service.callback(context, arguments)
    return CallbackReceipt.model_validate(await service.receipt("requestCallback", context, lead))


class ToolRegistry:
    def __init__(self, definitions: tuple[ToolDefinition[Any, Any], ...]) -> None:
        self._definitions: dict[str, ToolDefinition[Any, Any]] = {
            definition.name: definition for definition in definitions
        }
        if len(self._definitions) != len(definitions):
            raise ValueError("Duplicate tool registration")

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._definitions)

    def presents_current_facts(self, name: str) -> bool:
        definition = self.get(name)
        return name in {"searchCourses", "getCourseDetails", "getMembershipDetails"} or bool(
            definition and definition.current_facts
        )

    def render(self, name: str, data: dict[str, object]) -> str | None:
        definition = self.get(name)
        if definition is None or definition.presentation is None:
            return None
        result = definition.result.model_validate(data)
        try:
            rendered = definition.presentation(result)
        except Exception:
            # Turn callback failures into the same safe invalid-output path as a bad value.
            rendered = None
        return TypeAdapter(Content).validate_python(rendered)

    def validate_business(self, business: BusinessConfig) -> None:
        if any(name not in self._definitions for name in business.enabled_tools):
            raise ValueError("Business enables an unregistered tool")

    def get(self, name: str) -> ToolDefinition[Any, Any] | None:
        return self._definitions.get(name)

    def specifications(self, business: BusinessConfig) -> tuple[dict[str, object], ...]:
        self.validate_business(business)
        return tuple(
            {
                "name": definition.name,
                "description": definition.description,
                "parameters": definition.arguments.model_json_schema(by_alias=True),
                "resultSchema": definition.result.model_json_schema(by_alias=True),
            }
            for name in business.enabled_tools
            if (definition := self.get(name)) is not None and definition.handler is not None
        )


def build_tool_registry() -> ToolRegistry:
    return ToolRegistry(
        (
            ToolDefinition(
                "searchCourses",
                "Search courses by category, inclusive budget and optional schedule.",
                SearchCoursesArguments,
                CourseSearchResult,
                search_courses,
            ),
            ToolDefinition(
                "getCourseDetails",
                "Get current fee, schedule and availability for a course ID.",
                CourseDetailsArguments,
                CourseDetailsResult,
                course_details,
            ),
            ToolDefinition(
                "getMembershipDetails",
                "List memberships or get one by optional membershipID.",
                MembershipArguments,
                MembershipResult,
                membership_details,
                presentation=render_memberships,
                current_facts=True,
            ),
            ToolDefinition(
                "createLead",
                "Save one qualified enquiry from actual customer state; reuse its real leadId.",
                LeadInput,
                LeadReceipt,
                create_lead,
            ),
            ToolDefinition(
                "requestCallback",
                "Request a callback with actual saved leadID and recorded customer intent.",
                CallbackArguments,
                CallbackReceipt,
                request_callback,
            ),
        )
    )


class ToolExecutor:
    def __init__(
        self, database: Database, businesses: BusinessRegistry, registry: ToolRegistry
    ) -> None:
        self.database = database
        self.businesses = businesses
        self.registry = registry

    async def specifications(self, context: ExecutionContext) -> tuple[dict[str, object], ...]:
        business = self.businesses.get(context.business_id)
        if business is None:
            raise RecordNotFound("Record not found.")
        await Repository(self.database, business).turn(context)
        return self.registry.specifications(business)

    async def execute(self, context: ExecutionContext, call: ToolCall) -> ActionResult:
        execution_id = str(uuid4())
        invoked = False

        def fail(code: Any, retryable: bool = False, replayed: bool = False) -> ActionResult:
            # No raw arguments, result bodies, customer IDs or exception messages in logs.
            logger.info(
                "tool_execution outcome=%s execution_id=%s request_id=%s",
                code,
                execution_id,
                context.request_id,
            )
            return ActionResult(
                execution_id=execution_id,
                replayed=replayed,
                handler_invoked=invoked,
                result=ToolFailure(
                    call_id=call.call_id, tool=call.name, code=code, retryable=retryable
                ),
            )

        business = self.businesses.get(context.business_id)
        definition = self.registry.get(call.name)
        if business is None or definition is None or call.name not in business.enabled_tools:
            return fail("NOT_ALLOWED")
        if definition.handler is None:
            return fail("UNAVAILABLE")
        try:
            arguments = definition.arguments.model_validate(call.arguments)
        except ValidationError:
            return fail("INVALID_ARGUMENTS")
        if call.name in {"createLead", "requestCallback"}:
            return await self._execute_write(context, call, definition, arguments)
        repo = Repository(self.database, business)
        key = {
            "business_id": context.business_id,
            "session_id": context.session_id,
            "turn_id": context.turn_id,
            "call_id": call.call_id,
        }
        fingerprint = hashlib.sha256(
            json.dumps(
                {"name": call.name, "arguments": arguments.model_dump(mode="json", by_alias=True)},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        collection = self.database.db.tool_executions
        try:
            await repo.turn(context)
            try:
                await collection.insert_one(
                    key
                    | {
                        "execution_id": execution_id,
                        "tool": call.name,
                        "arguments_hash": fingerprint,
                        "status": "processing",
                        "trace": None,
                        "created_at": datetime.now(UTC),
                    }
                )
            except DuplicateKeyError:
                previous = await collection.find_one(key)
                if previous is None:
                    return fail("UNAVAILABLE", True)
                execution_id = previous["execution_id"]
                if previous["arguments_hash"] != fingerprint:
                    return fail("CONFLICT")
                if previous["trace"] is None:
                    return fail("IN_PROGRESS", True, True)
                trace = ActionResult.model_validate(previous["trace"])
                return trace.model_copy(update={"replayed": True, "handler_invoked": False})
            invoked = True
            try:
                async with asyncio.timeout(5):
                    output = await definition.handler(repo, context, arguments)
                # Revalidation rejects extra/private fields even in model_copy/construct output.
                output = definition.result.model_validate(
                    output.model_dump(mode="python", warnings=False)
                )
                trace = ActionResult(
                    execution_id=execution_id,
                    handler_invoked=True,
                    result=ToolSuccess(
                        call_id=call.call_id,
                        tool=definition.name,
                        data=output.model_dump(mode="json", by_alias=True),
                    ),
                )
            except RecordNotFound:
                trace = fail("NOT_FOUND")
            except RecordConflict:
                trace = fail("CONFLICT")
            except ValidationError:
                trace = fail("INVALID_RESULT")
            except (PyMongoError, TimeoutError):
                trace = fail("UNAVAILABLE", True)
            except Exception:
                trace = fail("UNAVAILABLE", True)
            result = await collection.update_one(
                key | {"execution_id": execution_id, "status": "processing"},
                {
                    "$set": {
                        "status": "completed",
                        "trace": trace.model_dump(mode="python"),
                        "completed_at": datetime.now(UTC),
                    }
                },
            )
            if result.modified_count != 1:
                return fail("UNAVAILABLE", True)
            logger.info(
                "tool_execution outcome=%s execution_id=%s request_id=%s",
                trace.result.status,
                execution_id,
                context.request_id,
            )
            return trace
        except RecordNotFound:
            return fail("NOT_FOUND")
        except PyMongoError:
            return fail("UNAVAILABLE", True)

    async def _execute_write(
        self,
        context: ExecutionContext,
        call: ToolCall,
        definition: ToolDefinition[Any, Any],
        arguments: Contract,
    ) -> ActionResult:
        from app.actions import ActionRejected, LeadActions

        business = self.businesses.get(context.business_id)
        assert business is not None  # execute already checked the immutable registry.
        repo = Repository(self.database, business)
        service = LeadActions(repo)
        collection = self.database.db.tool_executions
        key = {
            "business_id": context.business_id,
            "session_id": context.session_id,
            "turn_id": context.turn_id,
            "call_id": call.call_id,
        }
        fingerprint = hashlib.sha256(
            json.dumps(
                {"name": call.name, "arguments": arguments.model_dump(mode="json", by_alias=True)},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        execution_id, invoked, replayed = str(uuid4()), False, False

        def failure(code: Any, retryable: bool = False) -> ActionResult:
            return ActionResult(
                execution_id=execution_id,
                handler_invoked=invoked,
                replayed=replayed,
                result=ToolFailure(
                    call_id=call.call_id, tool=call.name, code=code, retryable=retryable
                ),
            )

        async def success_from_record() -> ActionResult | None:
            proof = await service.reconcile(call.name, context, arguments)
            if proof is None:
                return None
            receipt = definition.result.model_validate(
                await service.receipt(call.name, context, proof)
            )
            return ActionResult(
                execution_id=execution_id,
                handler_invoked=invoked,
                replayed=replayed,
                result=ToolSuccess(
                    call_id=call.call_id,
                    tool=definition.name,
                    data=receipt.model_dump(mode="json", by_alias=True),
                ),
            )

        async def publish(trace: ActionResult) -> ActionResult:
            try:
                await collection.update_one(
                    key
                    | {
                        "execution_id": execution_id,
                        "status": {"$in": ["processing", "uncertain"]},
                    },
                    {
                        "$set": {
                            "status": "completed",
                            "trace": trace.model_dump(mode="python"),
                            "completed_at": datetime.now(UTC),
                        }
                    },
                )
            except PyMongoError:
                # A verified business outcome remains real even if trace acknowledgement fails.
                pass
            return trace

        try:
            await repo.turn(context)
            previous = await collection.find_one(key)
            if previous is not None:
                execution_id, replayed = previous["execution_id"], True
                if previous["arguments_hash"] != fingerprint:
                    return failure("CONFLICT")
                if previous.get("trace") is not None:
                    prior = ActionResult.model_validate(previous["trace"])
                    return prior.model_copy(update={"replayed": True, "handler_invoked": False})
                proof = await success_from_record()
                if proof is not None:
                    return await publish(proof)
                if previous["status"] != "uncertain":
                    return failure("IN_PROGRESS", True)
            await service.authorize(call.name, context, arguments)
            if previous is None:
                try:
                    await collection.insert_one(
                        key
                        | {
                            "execution_id": execution_id,
                            "tool": call.name,
                            "arguments_hash": fingerprint,
                            "status": "processing",
                            "trace": None,
                            "created_at": datetime.now(UTC),
                        }
                    )
                except DuplicateKeyError:
                    return failure("IN_PROGRESS", True)
            else:
                execution_id = str(uuid4())
                acquired = await collection.update_one(
                    key | {"status": "uncertain"},
                    {"$set": {"status": "processing", "execution_id": execution_id}},
                )
                if acquired.modified_count != 1:
                    return failure("IN_PROGRESS", True)
            if not can_start_write():
                return await publish(failure("UNAVAILABLE", True))
            invoked = True
            try:
                assert definition.handler is not None
                async with asyncio.timeout(5):
                    output = await definition.handler(repo, context, arguments)
                output = definition.result.model_validate(output.model_dump(mode="python"))
                verified = await success_from_record()
                trace = verified if verified is not None else failure("INVALID_RESULT")
            except (PyMongoError, TimeoutError):
                try:
                    proof = await success_from_record()
                except PyMongoError:
                    proof = None
                if proof is not None:
                    return await publish(proof)
                try:
                    await collection.update_one(
                        key | {"execution_id": execution_id}, {"$set": {"status": "uncertain"}}
                    )
                except PyMongoError:
                    pass
                return failure("OUTCOME_UNKNOWN", True)
            except ActionRejected as error:
                trace = failure(error.code)
            except RecordNotFound:
                trace = failure("NOT_FOUND")
            except Exception:
                proof = await success_from_record()
                if proof is not None:
                    return await publish(proof)
                trace = failure("UNAVAILABLE", True)
            return await publish(trace)
        except ActionRejected as error:
            return failure(error.code)
        except RecordNotFound:
            return failure("NOT_FOUND")
        except PyMongoError:
            return failure("OUTCOME_UNKNOWN" if invoked else "UNAVAILABLE", True)
