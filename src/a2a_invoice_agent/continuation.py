
import json

from a2a_invoice_agent.models import (
    ActionResults,
    RECEIPT_MEDIA_TYPE,
)


def build_receipt(task_data: dict, results_data: dict) -> dict:
    """Validate grader results against stored proposals and build receipts."""

    results = ActionResults.model_validate(results_data)

    artifacts = task_data.get("artifacts", [])
    proposal_artifacts = [
        artifact
        for artifact in artifacts
        for part in artifact.get("parts", [])
        if part.get("mediaType")
        == "application/vnd.ga5.invoice-action-proposals+json"
    ]

    if len(proposal_artifacts) != 1:
        raise ValueError("Expected exactly one proposal artifact")

    proposal_part = next(
        part for part in proposal_artifacts[0]["parts"]
        if part["mediaType"]
        == "application/vnd.ga5.invoice-action-proposals+json"
    )
    proposal_data = proposal_part["data"]

    if results.batch_id != proposal_data["batchId"]:
        raise ValueError("Batch ID mismatch")

    proposals = {
        item["packageId"]: item
        for item in proposal_data["proposals"]
    }

    seen = set()
    executions = []

    for result in results.results:
        package_id = result.package_id

        if package_id in seen:
            raise ValueError("Duplicate package result")
        seen.add(package_id)

        proposal = proposals.get(package_id)

        if proposal is None:
            raise ValueError("Unknown package ID")

        if result.action_id != proposal["actionId"]:
            raise ValueError("Action ID mismatch")

        if result.action != proposal["action"]:
            raise ValueError("Action mismatch")

        if result.outcome == "ACCEPTED":
            executions.append({
                "packageId": package_id,
                "actionId": proposal["actionId"],
                "action": proposal["action"],
                "receiptNonce": result.receipt_nonce,
                "facts": proposal["facts"],
                "evidenceRefs": proposal["evidenceRefs"],
            })

    if seen != set(proposals):
        raise ValueError("Results must cover all invoice packages")

    return {
        "batchId": proposal_data["batchId"],
        "executions": executions,
    }



