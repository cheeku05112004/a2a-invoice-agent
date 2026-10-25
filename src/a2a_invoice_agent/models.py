
from typing import Any, Literal

from pydantic import BaseModel, Field, ConfigDict, field_validator


ACTIONS = Literal[
    "settle_invoice",
    "request_approval",
    "hold_invoice",
    "reject_duplicate",
    "open_exception",
]

PROPOSAL_MEDIA_TYPE = (
    "application/vnd.ga5.invoice-action-proposals+json"
)
RECEIPT_MEDIA_TYPE = (
    "application/vnd.ga5.invoice-action-receipts+json"
)
BATCH_MEDIA_TYPE = (
    "application/vnd.ga5.invoice-claim-batch+json"
)
RESULTS_MEDIA_TYPE = (
    "application/vnd.ga5.invoice-action-results+json"
)


class StrictModel(BaseModel):
    model_config = ConfigDict(
        populate_by_name=True,
        extra="forbid",
    )


class InvoiceBatch(StrictModel):
    batch_id: str = Field(alias="batchId", min_length=1)
    policy_revision: str = Field(alias="policyRevision", min_length=1)
    packages: list[dict[str, Any]] = Field(min_length=12, max_length=12)


class InvoiceFacts(StrictModel):
    vendor_name: str = Field(alias="vendorName", min_length=1)
    invoice_number: str = Field(alias="invoiceNumber", min_length=1)
    amount_minor: int = Field(alias="amountMinor", ge=0, strict=True)
    currency: str = Field(min_length=1)


class InvoiceProposal(StrictModel):
    package_id: str = Field(alias="packageId", min_length=1)
    action_id: str = Field(alias="actionId", min_length=12)
    action: ACTIONS
    facts: InvoiceFacts
    evidence_refs: list[str] = Field(
        alias="evidenceRefs",
        min_length=3,
        max_length=3,
    )
    rationale: str = Field(min_length=60, max_length=1500)

    @field_validator("evidence_refs")
    @classmethod
    def validate_evidence_refs(cls, refs):
        if len(set(refs)) != 3:
            raise ValueError("Evidence references must be unique")

        if any(
            not isinstance(ref, str)
            or len(ref) < 3
            or not ref.startswith("[")
            or not ref.endswith("]")
            for ref in refs
        ):
            raise ValueError("Evidence references must be bracketed")

        return refs


class ProposalBatch(StrictModel):
    batch_id: str = Field(alias="batchId", min_length=1)
    proposals: list[InvoiceProposal] = Field(min_length=1)


class ActionResult(StrictModel):
    package_id: str = Field(alias="packageId", min_length=1)
    action_id: str = Field(alias="actionId", min_length=12)
    action: ACTIONS
    outcome: Literal["ACCEPTED", "REJECTED"]
    receipt_nonce: str = Field(alias="receiptNonce", min_length=1)


class ActionResults(StrictModel):
    batch_id: str = Field(alias="batchId", min_length=1)
    results: list[ActionResult] = Field(min_length=1)


class MessagePart(StrictModel):
    media_type: str = Field(alias="mediaType", min_length=1)
    data: dict[str, Any] | None = None


class A2AMessage(StrictModel):
    message_id: str = Field(alias="messageId", min_length=1)
    role: Literal["ROLE_USER", "ROLE_AGENT"]
    parts: list[MessagePart] = Field(min_length=1)
    task_id: str | None = Field(default=None, alias="taskId")
    context_id: str | None = Field(default=None, alias="contextId")


class A2AConfiguration(StrictModel):
    return_immediately: bool = Field(alias="returnImmediately")
    history_length: int = Field(alias="historyLength", ge=0)
    accepted_output_modes: list[str] = Field(
        alias="acceptedOutputModes",
        min_length=1,
    )


class A2ARequest(StrictModel):
    message: A2AMessage
    configuration: A2AConfiguration | None = None


class TaskStatus(StrictModel):
    state: Literal[
        "TASK_STATE_SUBMITTED",
        "TASK_STATE_WORKING",
        "TASK_STATE_INPUT_REQUIRED",
        "TASK_STATE_COMPLETED",
        "TASK_STATE_CANCELED",
        "TASK_STATE_FAILED",
    ]
    message: A2AMessage | None = None


class ArtifactPart(StrictModel):
    media_type: str = Field(alias="mediaType", min_length=1)
    data: dict[str, Any]


class Artifact(StrictModel):
    artifact_id: str = Field(alias="artifactId", min_length=1)
    name: str = Field(min_length=1)
    parts: list[ArtifactPart] = Field(min_length=1)


class A2ATask(StrictModel):
    id: str = Field(min_length=1)
    context_id: str = Field(alias="contextId", min_length=1)
    status: TaskStatus
    history: list[A2AMessage] = Field(default_factory=list)
    artifacts: list[Artifact] = Field(default_factory=list)


class TaskListResponse(StrictModel):
    tasks: list[A2ATask] = Field(default_factory=list)


class TaskResponse(StrictModel):
    task: A2ATask

