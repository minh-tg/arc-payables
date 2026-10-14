#!/usr/bin/env python3
"""Offline consistency checks only: never contacts providers or changes saved evidence."""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path

from eth_abi import decode
from eth_utils import keccak

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "docs/evidence"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def main():
    snapshots = {}
    for path in sorted(EVIDENCE.glob("*.json")):
        raw = path.read_bytes()
        snapshots[path.name] = json.loads(raw)
        print(f"{hashlib.sha256(raw).hexdigest()}  {path.name}")
    require(len(snapshots) == 5, "Expected all five saved snapshots")
    chain = snapshots["arc-testnet-settlement.json"]
    circle = snapshots["circle-settlement.json"]
    books = snapshots["erpnext-sandbox-settlement.json"]
    allowance = snapshots["arc-testnet-allowance-history.json"]
    availability = snapshots["application-record-availability.json"]
    allowed = {"eth_chainId", "eth_getTransactionReceipt", "eth_call", "eth_getLogs"}
    for requests in (chain["requests"], allowance["log_requests"], allowance["receipt_requests"]):
        require(all(request["method"] in allowed for request in requests), "Non-read-only RPC request")
    require(all(request["method"] == "GET" for request in circle["requests"]), "Non-GET provider request")
    responses = {response["id"]: response["result"] for response in chain["responses"]}
    require(int(responses[1], 16) == 5042002, "Wrong chain")
    receipt = responses[2]
    require(int(receipt["status"], 16) == 1, "Settlement receipt unsuccessful")
    event = chain["decoded"]["payment_executed"]
    tx_hash = receipt["transactionHash"]
    topic = "0x" + keccak(text="PaymentExecuted(bytes32,bytes32,address,address,address,uint256)").hex()
    events = [log for log in receipt["logs"] if log["address"].lower() == chain["guard"].lower()
              and log["topics"][0].lower() == topic]
    require(len(events) == 1, "Expected one guard payment event")
    log = events[0]
    recipient, token, amount = decode(["address", "address", "uint256"], bytes.fromhex(log["data"][2:]))
    require(log["topics"][1] == event["payment_id"] and log["topics"][2] == event["evidence_hash"],
            "Payment/evidence hash mismatch")
    payer = "0x" + log["topics"][3][-40:]
    require(payer == event["payer"] and recipient == event["recipient"]
            and token == event["token"] and amount == event["amount_units"], "Decoded guard event mismatch")
    transfer_topic = "0x" + keccak(text="Transfer(address,address,uint256)").hex()
    transfers = [log for log in receipt["logs"] if log["address"].lower() == token
                 and log["topics"][0] == transfer_topic
                 and "0x" + log["topics"][1][-40:] == payer
                 and "0x" + log["topics"][2][-40:] == recipient
                 and int(log["data"], 16) == amount]
    require(len(transfers) == 1 and amount == 10000 and int(responses[13], 16) == 6,
            "Wrong token transfer/precision")
    require(circle["transaction"]["txHash"] == tx_hash and circle["transaction"]["state"] == "COMPLETE",
            "Circle settlement linkage mismatch")
    require(circle["wallet"]["address"] == payer and circle["wallet"]["accountType"] == "SCA"
            and circle["wallet"]["blockchain"] == "ARC-TESTNET", "Circle payer provenance mismatch")
    require(books["links"]["transaction_hash"] == tx_hash
            and books["links"]["guard_payment_id"] == event["payment_id"]
            and books["links"]["guard_evidence_hash"] == event["evidence_hash"], "ERP linkage mismatch")
    require(books["payment_entry"]["reference_no"].endswith(tx_hash)
            and books["fee_journal_entry"]["cheque_no"].endswith(tx_hash), "ERP reference mismatch")
    require(Decimal(str(books["payment_entry"]["paid_amount"])) == Decimal(amount) / 10**6,
            "ERP supplier amount mismatch")
    for voucher in (books["payment_entry"]["name"], books["fee_journal_entry"]["name"]):
        rows = [row for row in books["gl_entries"] if row["voucher_no"] == voucher and row["is_cancelled"] == 0]
        require(len(rows) == 2 and sum(Decimal(str(row["debit"])) - Decimal(str(row["credit"])) for row in rows) == 0,
                "Unbalanced saved GL")
    pair = allowance["inferred_pair"]
    operations = {operation["transaction_hash"]: operation for operation in allowance["user_operations"]}
    meters = []
    operation_topic = "0x" + keccak(text="UserOperationEvent(bytes32,address,address,uint256,bool,uint256,uint256)").hex()
    for tx in (pair["approval_transaction_hash"], tx_hash):
        receipt = next(response["result"] for response in allowance["receipt_responses"]
                       if response["result"]["transactionHash"] == tx)
        logs = [log for log in receipt["logs"] if log["topics"][0] == operation_topic
                and "0x" + log["topics"][2][-40:] == payer]
        require(len(logs) == 1, "Expected one wallet operation")
        nonce, success, cost, gas = decode(["uint256", "bool", "uint256", "uint256"], bytes.fromhex(logs[0]["data"][2:]))
        require(success and str(cost) == operations[tx]["actual_gas_cost_native_units"], "Raw gas meter mismatch")
        meters.append(cost)
    total = sum(meters)
    require(str(total) == pair["exact_native_cost_units"], "Inferred pair arithmetic mismatch")
    require(str((total + 10**12 - 1) // 10**12) == pair["ceil_once_usdc_six_decimal_units"], "Fee rounding mismatch")
    require(Decimal(circle["transaction"]["networkFee"]) == Decimal(meters[1]) / 10**18,
            "Circle fee and operation meter mismatch")
    require(availability["target_transaction_hash"] == tx_hash, "Availability scope mismatch")
    require(availability["database_checks"]["payments"]["target_or_binding_identifier_matches"] == 0,
            "Availability finding changed; review evidence claims")
    print("PASS: saved receipt/provider/ERP links, transfer, balanced GL and inferred gas arithmetic")
    print("NOT VERIFIED: historical app/approval record, ultimate sponsorship/expense treatment, publication permission")


if __name__ == "__main__":
    main()
