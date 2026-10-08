"""History is observable advice, never an execution/approval endpoint."""
from arc_payables import worker


def test_preview_and_history_reads_never_create_or_execute_worker_plans(runtime):
    client, store = runtime["client"], runtime["store"]
    assert client.get("/plans").json() == {"plans": [], "count": 0}
    assert client.get("/plan").status_code == 200
    assert store.payment_plan_history() == []
    assert runtime["payment"].submission_calls == 0

    worker.run_pass(runtime["workflow"], intake=True, autopay=True, rescreen=False)
    before_events = store.audit_head()
    before_calls = runtime["payment"].submission_calls
    response = client.get("/plans")
    assert response.status_code == 200
    history = response.json()
    assert history["count"] == len(history["plans"])
    executed = next(row for row in history["plans"] if row["status"] == "executed")
    assert executed["outcome"]["confirmation_status"] == "CONFIRMED"
    assert executed["outcome"]["erp_status"] == "RECORDED"
    assert executed["plan"]["ordered"][0]["invoice_id"] == executed["outcome"]["invoice_id"]
    assert executed["plan"]["input_digest"]
    assert client.get("/plans").json() == history
    assert store.audit_head() == before_events
    assert runtime["payment"].submission_calls == before_calls
    assert client.post("/plans").status_code == 405


def test_plan_history_uses_the_same_api_authentication(runtime):
    runtime["settings"].api_key = "test-operator-key"
    client = runtime["client"]
    assert client.get("/plans").status_code == 401
    assert client.get("/plans", headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.get("/plans", headers={"X-API-Key": "test-operator-key"}).status_code == 200
