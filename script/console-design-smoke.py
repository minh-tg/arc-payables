#!/usr/bin/env python3
"""Check console layout and interaction states in Chromium.

Run the mock server separately, then:
  uv run --offline --with playwright python script/console-design-smoke.py
"""

from __future__ import annotations

import os
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:8097")
SHOTS = Path("/home/minh/projects/hackathons/tameion/local-docs/orchestration/ux-shots")
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
}


def prepare_demo(page: Page) -> None:
    response = page.request.post(f"{BASE_URL}/demo/start")
    assert response.ok, f"demo seed failed: HTTP {response.status}"
    response = page.request.post(f"{BASE_URL}/invoices/demo-invoice-suspicious/evaluate")
    assert response.ok, f"demo evaluation failed: HTTP {response.status}"


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
          renderError: document.querySelector('#view .error strong')?.textContent || null
        })"""
    )
    assert result["title"] == f"{title} · Tameion", (route, result["title"])
    assert result["documentWidth"] <= width, (route, width, result["documentWidth"])
    assert result["bodyWidth"] <= width, (route, width, result["bodyWidth"])
    if width < 900:
        assert not result["undersized"], (route, width, result["undersized"])
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
        executable = Path.home() / ".cache/ms-playwright/chromium-1243/chrome-linux64/chrome"
        browser = playwright.chromium.launch(executable_path=str(executable), headless=True)
        bootstrap = browser.new_page()
        prepare_demo(bootstrap)
        bootstrap.close()

        for width in (390, 899, 900):
            context = browser.new_context(viewport={"width": width, "height": 900})
            page = context.new_page()
            add_draft_fixture(page)
            for route, title in ROUTES.items():
                assert_view(page, route, title, width)
            if width == 390:
                assert_case_and_badges(page)
            context.close()

        save_screenshots(browser)
        browser.close()
    print("PASS  9 routes at 390px and 900px, mobile targets, invoice layout and four screenshots")


if __name__ == "__main__":
    main()
