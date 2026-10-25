from a2a_invoice_agent.continuation import build_receipt

proposals = []

for i in range(1, 13):
    proposals.append({
        "packageId": f"INV-{i:03}",
        "actionId": f"invoice-action-{i:03}-unique",
        "action": "hold_invoice",
        "facts": {
            "vendorName": f"Vendor {i}",
            "invoiceNumber": f"INV-{i:03}",
            "amountMinor": 10000 * i,
            "currency": "INR"
        },
        "evidenceRefs": ["[DOC-1]", "[DOC-2]", "[DOC-3]"],
        "rationale": (
            "The action hold_invoice is appropriate because the "
            "supporting documents require further verification. "
            "Evidence [DOC-1] and [DOC-2] support this decision."
        )
    })

task = {
    "artifacts": [{
        "parts": [{
            "mediaType": "application/vnd.ga5.invoice-action-proposals+json",
            "data": {
                "batchId": "BATCH-001",
                "proposals": proposals
            }
        }]
    }]
}

results = {
    "batchId": "BATCH-001",
    "results": [
        {
            "packageId": f"INV-{i:03}",
            "actionId": f"invoice-action-{i:03}-unique",
            "action": "hold_invoice",
            "outcome": "ACCEPTED" if i <= 6 else "REJECTED",
            "receiptNonce": f"NONCE-{i:03}"
        }
        for i in range(1, 13)
    ]
}

# Test 1: All 12 results
receipt = build_receipt(task, results)
assert len(receipt["executions"]) == 6
print("PASS: 12 results accepted; 6 receipts generated.")

# Test 2: Only 11 results
try:
    build_receipt(task, {
        **results,
        "results": results["results"][:11]
    })
    raise AssertionError("11 results should have failed")
except ValueError:
    print("PASS: Missing package result rejected.")

# Test 3: Duplicate package result
try:
    duplicate_results = {
        **results,
        "results": results["results"] + [results["results"][0]]
    }
    build_receipt(task, duplicate_results)
    raise AssertionError("Duplicate should have failed")
except ValueError:
    print("PASS: Duplicate package result rejected.")

# Test 4: Wrong batch ID
try:
    build_receipt(task, {
        **results,
        "batchId": "WRONG-BATCH"
    })
    raise AssertionError("Wrong batch should have failed")
except ValueError:
    print("PASS: Wrong batch ID rejected.")

print("\nALL CONTINUATION TESTS PASSED")
