import json
import uuid
import asyncio
import traceback

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from a2a_invoice_agent.ai_engine import generate_proposals
from a2a_invoice_agent.continuation import build_receipt
from a2a_invoice_agent.security import verify_token

from a2a_invoice_agent.database import (
    get_task_by_id,
    get_message_task,
    list_tasks_by_principal,
    save_task_idempotently,
    canonical_message_hash,
    complete_task_with_message_idempotently,
    update_task_atomically,
    reserve_message,
    release_reservation,
    IdempotencyConflict,
    TaskStateConflict,
    MessageInProgress,
)

from a2a_invoice_agent.models import (
    A2ARequest,
    A2ATask,
    TaskStatus,
    Artifact,
    ArtifactPart,
    InvoiceBatch,
    BATCH_MEDIA_TYPE,
    RESULTS_MEDIA_TYPE,
    RECEIPT_MEDIA_TYPE,
    PROPOSAL_MEDIA_TYPE,
)

router = APIRouter(prefix="/a2a")

MAX_BODY_SIZE = 512 * 1024

NON_TERMINAL_STATES = [
    "TASK_STATE_SUBMITTED",
    "TASK_STATE_WORKING",
    "TASK_STATE_INPUT_REQUIRED",
]


def check_a2a_version(request):
    if request.headers.get("a2a-version") != "1.0":
        raise HTTPException(
            status_code=400,
            detail="A2A-Version must be 1.0",
        )


def check_content_type(request):
    content_type = request.headers.get("content-type", "")
    media_type = content_type.split(";")[0].strip().lower()

    if media_type != "application/a2a+json":
        raise HTTPException(
            status_code=415,
            detail="Content-Type must be application/a2a+json",
        )


def a2a_response(data, status_code=200):
    body = json.dumps(
        data,
        ensure_ascii=False,
    ).encode("utf-8")

    if len(body) > MAX_BODY_SIZE:
        raise HTTPException(
            status_code=500,
            detail="Response is too large",
        )

    return JSONResponse(
        content=data,
        status_code=status_code,
        media_type="application/a2a+json",
    )


async def read_request(request):
    body = await request.body()

    if len(body) > MAX_BODY_SIZE:
        raise HTTPException(
            status_code=413,
            detail="Request body is too large",
        )

    try:
        raw_data = json.loads(body)
        parsed = A2ARequest.model_validate(raw_data)

    except (json.JSONDecodeError, ValidationError):
        raise HTTPException(
            status_code=400,
            detail="Invalid A2A request",
        )

    return raw_data, parsed


def get_owned_task(task_id, principal):
    stored = get_task_by_id(task_id, principal)

    if stored is None:
        raise HTTPException(
            status_code=404,
            detail="Task not found",
        )

    try:
        return json.loads(stored["task_json"])

    except (KeyError, TypeError, json.JSONDecodeError):
        raise HTTPException(
            status_code=500,
            detail="Stored task data is invalid",
        )


@router.post("/message:send")
async def send_message(
    request: Request,
    principal: str = Depends(verify_token),
):
    check_a2a_version(request)
    check_content_type(request)

    raw_data, request_data = await read_request(request)
    message = request_data.message

    if message.role != "ROLE_USER":
        raise HTTPException(
            status_code=400,
            detail="Expected ROLE_USER message",
        )

    if len(message.parts) != 1:
        raise HTTPException(
            status_code=400,
            detail="Exactly one message part is required",
        )

    part = message.parts[0]

    # --------------------------------------------
    # CONTINUATION MESSAGE
    # --------------------------------------------

    if message.task_id or message.context_id:

        if not message.task_id or not message.context_id:
            raise HTTPException(
                status_code=400,
                detail="Both taskId and contextId are required",
            )

        if part.media_type != RESULTS_MEDIA_TYPE or part.data is None:
            raise HTTPException(
                status_code=400,
                detail="Invalid action-results media type or data",
            )

        task_data = get_owned_task(
            message.task_id,
            principal,
        )

        if task_data.get("contextId") != message.context_id:
            raise HTTPException(
                status_code=404,
                detail="Task not found",
            )

        message_hash = canonical_message_hash(raw_data["message"])

        try:
            previous_task = get_message_task(
                principal, message.message_id, message_hash
            )
        except IdempotencyConflict:
            raise HTTPException(
                status_code=409,
                detail="IDEMPOTENCY_CONFLICT",
            )

        if previous_task is not None:
            return a2a_response({"task": previous_task})

        if task_data.get("status", {}).get("state") != (
            "TASK_STATE_INPUT_REQUIRED"
        ):
            raise HTTPException(
                status_code=409,
                detail="Task is not awaiting action results",
            )

        try:
            receipt_data = build_receipt(
                task_data,
                part.data,
            )

        except (ValueError, ValidationError, KeyError, TypeError):
            raise HTTPException(
                status_code=400,
                detail="Invalid action results",
            )

        task_data.setdefault("artifacts", []).append({
            "artifactId": str(uuid.uuid4()),
            "name": "Invoice action receipts",
            "parts": [{
                "mediaType": RECEIPT_MEDIA_TYPE,
                "data": receipt_data,
            }],
        })

        task_data.setdefault("history", []).append(
            message.model_dump(
                by_alias=True,
                mode="json",
                exclude_none=True,
            )
        )

        task_data["status"] = {
            "state": "TASK_STATE_COMPLETED",
        }

        task_json = json.dumps(
            task_data,
            ensure_ascii=False,
            separators=(",", ":"),
        )

        message_hash = canonical_message_hash(
            raw_data["message"]
        )

        try:
            result = complete_task_with_message_idempotently(
                task_id=message.task_id,
                principal=principal,
                message_id=message.message_id,
                message_hash=message_hash,
                task_json=task_json,
            )

        except IdempotencyConflict:
            raise HTTPException(
                status_code=409,
                detail="IDEMPOTENCY_CONFLICT",
            )

        except TaskStateConflict:
            raise HTTPException(
                status_code=409,
                detail="Task state changed",
            )

        return a2a_response({
            "task": result["task"],
        })

    # --------------------------------------------
    # INITIAL REQUEST CONFIGURATION VALIDATION
    # --------------------------------------------

    config = request_data.configuration

    if config is None:
        raise HTTPException(
            status_code=400,
            detail="Initial request configuration is required",
        )

    if config.return_immediately is not False:
        raise HTTPException(
            status_code=400,
            detail="returnImmediately must be false",
        )

    if config.history_length != 20:
        raise HTTPException(
            status_code=400,
            detail="historyLength must be 20",
        )

    required_modes = {
        PROPOSAL_MEDIA_TYPE,
        RECEIPT_MEDIA_TYPE,
    }

    if not required_modes.issubset(
        set(config.accepted_output_modes)
    ):
        raise HTTPException(
            status_code=400,
            detail="Required output modes are missing",
        )

    # --------------------------------------------
    # INITIAL INVOICE BATCH
    # --------------------------------------------

    if part.media_type != BATCH_MEDIA_TYPE or part.data is None:
        raise HTTPException(
            status_code=400,
            detail="Invalid invoice batch media type or data",
        )

    try:
        batch = InvoiceBatch.model_validate(part.data)

    except ValidationError:
        raise HTTPException(
            status_code=400,
            detail="Invalid invoice batch",
        )

    if not batch.packages:
        raise HTTPException(
            status_code=400,
            detail="Invoice batch is empty",
        )

    message_hash = canonical_message_hash(
        raw_data["message"]
    )

    # Reserve the message ID BEFORE calling AI.
    try:
        reservation = reserve_message(
            principal,
            message.message_id,
            message_hash,
        )

    except IdempotencyConflict:
        raise HTTPException(
            status_code=409,
            detail="IDEMPOTENCY_CONFLICT",
        )

    if reservation["status"] == "completed":
        existing = get_task_by_id(
            reservation["task_id"],
            principal,
        )

        if existing is None:
            raise HTTPException(
                status_code=404,
                detail="Task not found",
            )

        return a2a_response({
            "task": json.loads(existing["task_json"]),
        })

    if reservation["status"] == "processing":
        deadline = asyncio.get_running_loop().time() + 42

        while asyncio.get_running_loop().time() < deadline:
            try:
                previous_task = get_message_task(
                    principal, message.message_id, message_hash
                )
            except IdempotencyConflict:
                raise HTTPException(
                    status_code=409,
                    detail="IDEMPOTENCY_CONFLICT",
                )

            if previous_task is not None:
                return a2a_response({"task": previous_task})

            await asyncio.sleep(0.2)

        raise HTTPException(
            status_code=409,
            detail="Original request is still processing; retry shortly",
        )

    # AI runs only after successful reservation.
    try:
        proposal_batch = generate_proposals(
            batch.model_dump(
                by_alias=True,
                mode="json",
            )
        )

        task_id = str(uuid.uuid4())
        context_id = str(uuid.uuid4())

        task = A2ATask(
            id=task_id,
            contextId=context_id,
            status=TaskStatus(
                state="TASK_STATE_INPUT_REQUIRED",
            ),
            history=[message],
            artifacts=[
                Artifact(
                    artifactId=str(uuid.uuid4()),
                    name="Invoice action proposals",
                    parts=[
                        ArtifactPart(
                            mediaType=PROPOSAL_MEDIA_TYPE,
                            data=proposal_batch.model_dump(
                                by_alias=True,
                                mode="json",
                            ),
                        )
                    ],
                )
            ],
        )

        task_json = task.model_dump_json(
            by_alias=True,
            exclude_none=True,
        )

        result = save_task_idempotently(
            principal=principal,
            message_id=message.message_id,
            message_hash=message_hash,
            reservation_token=reservation["reservation_token"],
            task_id=task_id,
            context_id=context_id,
            batch_id=batch.batch_id,
            state="TASK_STATE_INPUT_REQUIRED",
            task_json=task_json,
        )

        if not result["created"]:
            existing = get_task_by_id(
                result["task_id"],
                principal,
            )

            if existing is None:
                raise HTTPException(
                    status_code=404,
                    detail="Task not found",
                )

            return a2a_response({
                "task": json.loads(existing["task_json"]),
            })

        return a2a_response({
            "task": json.loads(task_json),
        })

    except IdempotencyConflict:
        raise HTTPException(
            status_code=409,
            detail="IDEMPOTENCY_CONFLICT",
        )

    except MessageInProgress:
        raise HTTPException(
            status_code=409,
            detail="Message reservation is missing",
        )

    except HTTPException:
        raise

    except Exception:
        traceback.print_exc()
        raise HTTPException(
            status_code=502,
            detail="Invoice analysis failed",
        )

    finally:
        release_reservation(
            principal,
            message.message_id,
            message_hash,
            reservation.get("reservation_token"),
        )


@router.get("/tasks")
def list_tasks(
    request: Request,
    principal: str = Depends(verify_token),
):
    check_a2a_version(request)

    return a2a_response({
        "tasks": list_tasks_by_principal(principal),
    })


@router.get("/tasks/{task_id}")
def get_task(
    task_id: str,
    request: Request,
    principal: str = Depends(verify_token),
):
    check_a2a_version(request)

    return a2a_response(
        get_owned_task(task_id, principal)
    )


@router.post("/tasks/{task_id}:cancel")
def cancel_task(
    task_id: str,
    request: Request,
    principal: str = Depends(verify_token),
):
    check_a2a_version(request)
    check_content_type(request)

    task_data = get_owned_task(
        task_id,
        principal,
    )

    if task_data.get("status", {}).get("state") not in (
        NON_TERMINAL_STATES
    ):
        raise HTTPException(
            status_code=409,
            detail="Task is already in a terminal state",
        )

    task_data["status"] = {
        "state": "TASK_STATE_CANCELED",
    }

    task_json = json.dumps(
        task_data,
        ensure_ascii=False,
        separators=(",", ":"),
    )

    try:
        update_task_atomically(
            task_id=task_id,
            principal=principal,
            expected_states=NON_TERMINAL_STATES,
            new_state="TASK_STATE_CANCELED",
            task_json=task_json,
        )

    except TaskStateConflict:
        raise HTTPException(
            status_code=409,
            detail="Task state changed",
        )

    return a2a_response(task_data)
