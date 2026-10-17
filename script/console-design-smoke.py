#!/usr/bin/env python3
"""Check console layout and interaction states in Chromium.

Run the mock server separately, then:
  uv run --offline --with playwright python script/console-design-smoke.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile

try:
    from playwright.sync_api import Error as PlaywrightError, Page, sync_playwright
except ImportError:
    print("SKIP  Playwright is not installed")
    raise SystemExit(0)

BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:8097")
SHOTS = Path(os.environ.get("CONSOLE_DESIGN_SHOTS_DIR", Path(tempfile.gettempdir()) / "tameion-console-design-smoke"))
CHROMIUM_PATH = os.environ.get("CHROMIUM_PATH")
ROUTES = {
    "overview": "Overview",
    "attention": "Exceptions",
    "queue": "Payment queue",
    "payments": "Settlements",
    "audit": "Audit chain",
    "worker": "Worker",
    "treasury": "Treasury and risk",
    "setup": "Setup",
    "invoice/demo-invoice-suspicious": "Invoice",
    "invoice/demo-invoice-legitimate": "Invoice",
    "payments/demo-invoice-legitimate": "Settlements",
}


def prepare_demo(page: Page) -> None:
    response = page.request.post(f"{BASE_URL}/demo/start")
    assert response.ok, f"demo seed failed: HTTP {response.status}"
    response = page.request.post(f"{BASE_URL}/invoices/demo-invoice-suspicious/evaluate")
    assert response.ok, f"demo evaluation failed: HTTP {response.status}"


def add_long_hash_payment(page: Page) -> None:
    transaction_hash = "0x" + "ab" * 32
    body = {
        "totals": {"value_usdc": 1250, "fees_usdc": 0.1, "settled": 1, "settled_not_recorded": 0, "unconfirmed": 0, "failed": 0},
        "payments": [{
            "invoice_id": "demo-invoice-legitimate",
            "invoice_number": "ACME-INV-2026-001",
            "supplier_id": "supplier-acme",
            "amount_usdc": 1250,
            "recipient": "0x1111111111111111111111111111111111111111",
            "confirmation_status": "CONFIRMED",
            "simulated": True,
            "erp_status": "ERP_RECORDED",
            "fee_usdc": 0.1,
            "transaction_hash": transaction_hash,
            "explorer_url": None,
            "settled_at": "2026-10-15T09:00:00+00:00",
            "outcome": {"code": "settled_and_recorded", "success": True, "summary": "Settled and recorded"},
        }],
    }

    def fulfill_payments(route):
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    page.route("**/payments", fulfill_payments)


def add_payment_report_fixture(page: Page) -> None:
    report = {
        "outcome": {"code": "settled_and_recorded", "success": True, "summary": "Payment settled and recorded."},
        "reference": {"matched_by": "invoice id", "value": "demo-invoice-legitimate"},
        "invoice": {"id": "demo-invoice-legitimate", "invoice_number": "ACME-INV-2026-001"},
        "settlement": {
            "amount_usdc": 1250,
            "recipient": "0x1111111111111111111111111111111111111111",
            "payer": "0x2222222222222222222222222222222222222222",
            "confirmation_status": "CONFIRMED",
            "simulated": True,
            "settled_at": "2026-10-15T09:00:00+00:00",
            "transaction_hash": "0x" + "ab" * 32,
        },
        "authorization": {"signature_verified": True},
        "ledger": {"status": "ERP_RECORDED", "fee_status": "ERP_RECORDED", "attempts": 1},
        "audit": {"ok": True, "entries": 1},
        "timeline": [{
            "when": 1792057080,
            "event": "INVOICE_RECEIVED",
            "state": "ERP_PENDING",
            "explanation": "Invoice received for processing.",
            "entry_hash": "0x" + "cd" * 32,
        }],
    }

    def fulfill_report(route):
        route.fulfill(status=200, content_type="application/json", body=json.dumps(report))

    page.route("**/payments/demo-invoice-legitimate*", fulfill_report)


def add_draft_fixture(page: Page) -> None:
    def fulfill_case(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body='''{
              "invoice_id":"demo-invoice-suspicious",
              "status":"open",
              "last_checked_at":null,
              "items":[{
                "check_code":"receipt_match",
                "detail":"Invoice line has no matching goods receipt.",
                "fix":"source_record",
                "source_type":"Purchase Receipt",
                "source_id":"PR-0001",
                "owner_role":"Receiving"
              }],
              "events":[],
              "draft":{
                "text":"To Receiving: review Purchase Receipt PR-0001 in the accounting system.",
                "source":"template",
                "check_code":"receipt_match",
                "owner_role":"Receiving",
                "created_at":"2026-10-15T09:00:00+00:00"
              }
            }''',
        )

    page.route("**/invoices/demo-invoice-suspicious/case", fulfill_case)


def assert_view(page: Page, route: str, title: str, width: int) -> None:
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(f"{BASE_URL}/console/#/{route}", wait_until="domcontentloaded")
    page.locator("#view .card").first.wait_for(state="visible", timeout=10000)
    page.wait_for_timeout(150)

    result = page.evaluate(
        """() => ({
          title: document.title,
          width: window.innerWidth,
          documentWidth: document.documentElement.scrollWidth,
          bodyWidth: document.body.scrollWidth,
          undersized: [...document.querySelectorAll('button, summary')]
            .filter((el) => el.getClientRects().length)
            .map((el) => ({text: el.textContent.trim(), height: el.getBoundingClientRect().height}))
            .filter((el) => el.height < 44),
          smallTargets: [...document.querySelectorAll('td a, .term, input:not([type="checkbox"])')]
            .filter((el) => el.getClientRects().length)
            .map((el) => ({text: el.textContent.trim(), height: el.getBoundingClientRect().height}))
            .filter((el) => el.height < 44),
          tableWidths: [...document.querySelectorAll('.table-wrapper')]
            .map((el) => ({scrollWidth: el.scrollWidth, clientWidth: el.clientWidth, heading: el.querySelector('th')?.textContent.trim()})),
          overflowingTables: [...document.querySelectorAll('.table-wrapper')]
            .filter((el) => el.scrollWidth > el.clientWidth)
            .map((el) => ({scrollWidth: el.scrollWidth, clientWidth: el.clientWidth, heading: el.innerText.slice(0, 80)})),
          splitBadgeWords: [...document.querySelectorAll('#view .badge')]
            .filter((el) => el.getClientRects().length)
            .flatMap((el) => {
              const split = [];
              const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
              while (walker.nextNode()) {
                const node = walker.currentNode;
                for (const match of node.textContent.matchAll(/[A-Za-z]{3,}/g)) {
                  const range = document.createRange();
                  range.setStart(node, match.index);
                  range.setEnd(node, match.index + match[0].length);
                  if (range.getClientRects().length > 1) split.push(match[0]);
                }
              }
              return split;
            }),
          splitMono: [...document.querySelectorAll('#view .table-wrapper td.mono')]
            .filter((el) => { const r = document.createRange(); r.selectNodeContents(el); return r.getClientRects().length > 1; })
            .map((el) => el.textContent.trim()),
          renderError: document.querySelector('#view .error strong')?.textContent || null
        })"""
    )
    assert result["title"] == f"{title} · Tameion", (route, result["title"])
    assert result["documentWidth"] <= width, (route, width, result["documentWidth"])
    assert result["bodyWidth"] <= width, (route, width, result["bodyWidth"])
    if width < 1024:
        assert not result["undersized"], (route, width, result["undersized"])
        assert not result["smallTargets"], (route, width, result["smallTargets"])
    assert not result["overflowingTables"], (route, width, result["overflowingTables"])
    assert not result["splitBadgeWords"], (route, width, "a status word is broken across lines", result["splitBadgeWords"])
    if width == 1280:
        assert not result["splitMono"], (route, result["splitMono"])
        if route in {"payments", "audit", "treasury"}:
            targets = result["tableWidths"]
            if route == "treasury":
                targets = [item for item in targets if item["heading"] == "Supplier"]
            print(f"WIDTH  {route}: {targets}")
    assert not result["renderError"], (route, result["renderError"])


def assert_case_and_badges(page: Page) -> None:
    page.set_viewport_size({"width": 390, "height": 900})
    page.goto(f"{BASE_URL}/console/#/invoice/demo-invoice-suspicious", wait_until="domcontentloaded")
    page.locator("#view h2").filter(has_text="Draft message to the record owner").wait_for(timeout=10000)
    result = page.evaluate(
        """() => {
          const draft = [...document.querySelectorAll('#view .card')]
            .find((card) => card.querySelector('h2')?.textContent === 'Draft message to the record owner');
          const head = draft?.querySelector('.finding-head');
          const badge = head?.querySelector('.badge');
          const meta = head?.querySelector('.finding-code');
          const words = [];
          const table = document.querySelector('#view .responsive-table');
          if (table) {
            const walker = document.createTreeWalker(table, NodeFilter.SHOW_TEXT);
            while (walker.nextNode()) {
              const node = walker.currentNode;
              if (!node.textContent.trim()) continue;
              const parent = node.parentElement;
              if (parent.closest('.mono, .badge')) continue;
              for (const match of node.textContent.matchAll(/[A-Za-z]{4,}/g)) {
                const range = document.createRange();
                range.setStart(node, match.index);
                range.setEnd(node, match.index + match[0].length);
                words.push({word: match[0], rects: range.getClientRects().length});
              }
            }
          }
          const touching = [...document.querySelectorAll('.badge + .badge')].some((badge) => {
            const previous = badge.previousElementSibling;
            if (!previous?.classList.contains('badge')) return false;
            const a = previous.getBoundingClientRect();
            const b = badge.getBoundingClientRect();
            return Math.abs(a.top - b.top) < 1 && b.left < a.right;
          });
          return {
            headDisplay: head && getComputedStyle(head).display,
            badgeDirectChild: badge?.parentElement === head,
            metaDirectChild: meta?.parentElement === head,
            overlap: (() => {
              if (!badge || !meta) return true;
              const a = badge.getBoundingClientRect();
              const b = meta.getBoundingClientRect();
              return a.left < b.right && a.right > b.left && a.top < b.bottom && a.bottom > b.top;
            })(),
            splitWords: words.filter((item) => item.rects > 1),
            touching
          };
        }"""
    )
    assert result["headDisplay"] == "flex", result
    assert result["badgeDirectChild"] and result["metaDirectChild"] and not result["overlap"], result
    assert not result["splitWords"], result["splitWords"]
    assert not result["touching"], result



def assert_payment_report(page: Page) -> None:
    add_payment_report_fixture(page)
    page.set_viewport_size({"width": 1280, "height": 900})
    page.goto(f"{BASE_URL}/console/#/payments/demo-invoice-legitimate", wait_until="domcontentloaded")
    page.locator("#view .card h2").filter(has_text="Audit chain").wait_for(timeout=10000)
    result = page.evaluate(
        """() => {
          const audit = [...document.querySelectorAll('#view .card')]
            .find((card) => card.querySelector('h2')?.textContent.trim() === 'Audit chain');
          const row = audit?.querySelector('tbody tr');
          return {
            event: row?.children[1]?.textContent.trim(),
            state: row?.children[2]?.textContent.trim(),
            time: row?.children[0]?.textContent.trim(),
            badgeFonts: [...document.querySelectorAll('dl.facts dd .badge')].map((el) => getComputedStyle(el).fontFamily)
          };
        }"""
    )
    assert result["event"] == "Invoice received", result
    assert result["state"] == "Pending ledger sync", result
    assert result["time"] != "1792057080" and "2026" in result["time"], result
    assert result["badgeFonts"] and all("Public Sans" in family for family in result["badgeFonts"]), result


def assert_reconcile_finding(page: Page) -> None:
    transaction_hash = "0x" + "ab" * 32

    def fulfill_reconcile(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=(
                '{"coverage":{"complete_from_block":0,"provider":"mock","guard_address":"0xguard"},'
                '"blocking":1,"review":1,"records":1,"observations":0,"findings":[{'
                '"severity":"blocking","kind":"unconfirmed","detail":"Mock settlement was not observed.",'
                '"invoice_id":"demo-invoice-suspicious","transaction_hash":"'
                + transaction_hash
                + '","simulated":true}]}'
            ),
        )

    page.route("**/reconciliation/run", fulfill_reconcile)
    page.set_viewport_size({"width": 1280, "height": 900})
    page.goto(f"{BASE_URL}/console/#/payments", wait_until="domcontentloaded")
    page.locator("#view button").filter(has_text="Reconcile with the chain").wait_for(timeout=10000)
    page.get_by_role("button", name="Reconcile with the chain").click()
    page.locator(f".mono:text-is('{transaction_hash}')").wait_for(timeout=10000)
    cell = page.locator(".mono").filter(has_text=transaction_hash).locator("..").last
    assert transaction_hash in cell.inner_text(), cell.inner_text()
    assert cell.locator(".badge", has_text="Simulated").count() == 1
    page.unroute("**/reconciliation/run", fulfill_reconcile)
    page.close()


def assert_layout_rules(browser) -> None:
    """Regressions found by eye: stray text with the explanations off, a button off its input's line,
    and a filter row pressed against its table."""
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    context.add_init_script("sessionStorage.setItem('arc_payables.guide', 'off')")
    page = context.new_page()
    for route in ROUTES:
        page.goto(f"{BASE_URL}/console/#/{route}", wait_until="domcontentloaded")
        page.locator("#view .card, #view .stats").first.wait_for(state="visible", timeout=10000)
        stray = page.evaluate(
            """() => {
              const hits = [];
              const walker = document.createTreeWalker(document.getElementById('view'), NodeFilter.SHOW_TEXT);
              while (walker.nextNode()) {
                const text = walker.currentNode.textContent.trim();
                if (['null', 'undefined', 'NaN'].includes(text)) hits.push(text);
              }
              return hits;
            }"""
        )
        assert not stray, (route, "stray text with explanations off", stray)

    page.goto(f"{BASE_URL}/console/#/queue", wait_until="domcontentloaded")
    page.get_by_role("button", name="Import payable").wait_for(timeout=10000)
    boxes = page.evaluate(
        """() => {
          const button = [...document.querySelectorAll('#view button')].find((el) => el.textContent.trim() === 'Import payable');
          const input = button.parentElement.querySelector('input');
          const a = input.getBoundingClientRect();
          const b = button.getBoundingClientRect();
          return {inputBottom: a.bottom, buttonBottom: b.bottom, inputHeight: a.height, buttonHeight: b.height};
        }"""
    )
    assert abs(boxes["inputBottom"] - boxes["buttonBottom"]) <= 1, ("import button is off its input", boxes)

    page.goto(f"{BASE_URL}/console/#/payments", wait_until="domcontentloaded")
    page.get_by_role("button", name="Failed").wait_for(timeout=10000)
    gap = page.evaluate(
        """() => {
          const chips = document.querySelector('#view button[aria-pressed]').parentElement;
          const table = chips.closest('.card').querySelector('.table-wrapper');
          return table.getBoundingClientRect().top - chips.getBoundingClientRect().bottom;
        }"""
    )
    assert gap >= 8, ("filter row sits on its table", gap)
    context.close()


def save_screenshots(browser) -> None:
    SHOTS.mkdir(parents=True, exist_ok=True)
    for theme in ("light", "dark"):
        for width in (1280, 390):
            context = browser.new_context(viewport={"width": width, "height": 1000}, color_scheme=theme)
            context.add_init_script(f"sessionStorage.setItem('arc_payables.theme', '{theme}')")
            page = context.new_page()
            add_draft_fixture(page)
            page.goto(f"{BASE_URL}/console/#/invoice/demo-invoice-suspicious", wait_until="domcontentloaded")
            page.locator("#view h2").filter(has_text="Draft message to the record owner").wait_for(timeout=10000)
            page.screenshot(path=str(SHOTS / f"after-invoice-{theme}-{width}.png"), full_page=True)
            context.close()


def main() -> None:
    with sync_playwright() as playwright:
        try:
            launch_options = {"headless": True}
            if CHROMIUM_PATH:
                launch_options["executable_path"] = CHROMIUM_PATH
            browser = playwright.chromium.launch(**launch_options)
        except PlaywrightError as error:
            print(f"SKIP  Chromium is unavailable: {error}")
            return
        bootstrap = browser.new_page()
        prepare_demo(bootstrap)
        bootstrap.close()

        for width in (1280, 1024, 900, 390):
            context = browser.new_context(viewport={"width": width, "height": 900})
            page = context.new_page()
            add_draft_fixture(page)
            add_long_hash_payment(page)
            add_payment_report_fixture(page)
            for route, title in ROUTES.items():
                assert_view(page, route, title, width)
            if width == 390:
                assert_case_and_badges(page)
            context.close()
        assert_payment_report(browser.new_page(viewport={"width": 1280, "height": 900}))
        assert_reconcile_finding(browser.new_page(viewport={"width": 1280, "height": 900}))
        assert_layout_rules(browser)
        save_screenshots(browser)
        browser.close()
    print("PASS  11 routes at 1280px, 1024px, 900px and 390px, table widths, phone targets, invoice layout and four screenshots")


if __name__ == "__main__":
    main()
