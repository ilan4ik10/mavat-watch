#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["playwright>=1.49"]
# ///
from __future__ import annotations

import argparse
import json
import os
import smtplib
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

DEFAULT_URL = "https://mavat.iplan.gov.il/SV4/1/3005115162/310"
SECTIONS = ["מסמכי התכנית", "מסמכי מידע מנהלי", "נוסחי פרסום"]
STATE_DIR = Path.home() / ".cache" / "mavat-watch"
STATE_FILE = STATE_DIR / "state.json"
HISTORY_FILE = STATE_DIR / "history.jsonl"


@dataclass(frozen=True)
class Row:
    section: str
    category: str
    name: str
    scope: str
    edit_date: str


@dataclass
class Change:
    section: str
    action: str
    name: str
    category: str = ""
    scope: str = ""
    edit_date: str = ""
    prev_scope: str = ""
    prev_edit_date: str = ""


EXTRACT_ROWS_JS = r"""
(panel) => {
    const rows = [];
    let category = '';
    panel.querySelectorAll('.uk-grid').forEach(grid => {
        const lead = grid.querySelector(':scope > .uk-text-lead');
        if (lead) category = (lead.innerText || '').trim();
        if (grid.classList.contains('sv4-headline')) return;
        const cells = [...grid.children];
        const dateCell = cells.find(c => /\bli-date\b/.test(c.className || ''));
        if (!dateCell) return;
        const nameCell = cells.find(c => /uk-width-expand|widthTitle/.test(c.className || ''));
        const scopeCell = cells.find(c =>
            /\bli-file\b/.test(c.className || '') && !/\bli-date\b/.test(c.className || ''));
        rows.push({
            category,
            name: nameCell ? (nameCell.innerText || '').trim().replace(/\s+/g, ' ') : '',
            scope: scopeCell ? (scopeCell.innerText || '').trim() : '',
            edit_date: (dateCell.innerText || '').trim(),
        });
    });
    return rows;
}
"""


def open_section(page, label):
    section = page.locator(
        f'ul.uk-accordion > li:has(> .uk-accordion-title:has-text("{label}"))'
    ).first
    section.wait_for(state="attached", timeout=15_000)
    section.scroll_into_view_if_needed(timeout=10_000)
    handle = section.element_handle()
    already_open = handle.evaluate("el => el.classList.contains('uk-open')")
    if not already_open:
        section.locator(".uk-accordion-title").first.click(timeout=10_000)
        page.wait_for_function(
            "el => el.classList.contains('uk-open')", arg=handle, timeout=10_000,
        )
        page.wait_for_timeout(1_500)
    return section


def expand_nested_accordions(panel, page):
    for _ in range(10):
        closed = panel.locator("li:not(.uk-open) > .uk-accordion-title")
        clicked = 0
        for i in range(closed.count()):
            try:
                toggle = closed.nth(i)
                if toggle.is_visible():
                    toggle.click(timeout=2_000)
                    clicked += 1
            except Exception:
                pass
        if clicked == 0:
            break
        page.wait_for_timeout(600)


def capture(url):
    result = {section: [] for section in SECTIONS}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_context(
            locale="he-IL", viewport={"width": 1500, "height": 1100},
        ).new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(6_000)
        for label in SECTIONS:
            try:
                section = open_section(page, label)
                panel = section.locator(".uk-accordion-content").first
                expand_nested_accordions(panel, page)
                page.wait_for_timeout(800)
                raw_rows = panel.evaluate(EXTRACT_ROWS_JS)
                result[label] = [
                    Row(section=label, **{k: r[k] for k in ("category", "name", "scope", "edit_date")})
                    for r in raw_rows if r["name"]
                ]
            except (PWTimeout, Exception):
                result[label] = []
        browser.close()
    return result


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def rows_to_json(rows_by_section):
    return {section: [asdict(r) for r in rows] for section, rows in rows_by_section.items()}


def rows_from_json(blob):
    return {section: [Row(**r) for r in rows] for section, rows in (blob or {}).items()}


def diff_section(prev_rows, curr_rows, section):
    prev_by_name = {r.name: r for r in prev_rows}
    curr_by_name = {r.name: r for r in curr_rows}
    changes = []
    for name, curr in curr_by_name.items():
        if name not in prev_by_name:
            changes.append(Change(
                section=section, action="NEW", name=name,
                category=curr.category, scope=curr.scope, edit_date=curr.edit_date,
            ))
        else:
            prev = prev_by_name[name]
            if curr.edit_date != prev.edit_date or curr.scope != prev.scope:
                changes.append(Change(
                    section=section, action="UPDATED", name=name,
                    category=curr.category, scope=curr.scope, edit_date=curr.edit_date,
                    prev_scope=prev.scope, prev_edit_date=prev.edit_date,
                ))
    for name, prev in prev_by_name.items():
        if name not in curr_by_name:
            changes.append(Change(
                section=section, action="REMOVED", name=name,
                category=prev.category, prev_scope=prev.scope, prev_edit_date=prev.edit_date,
            ))
    return changes


def diff_all(prev, curr):
    return [
        change
        for section in SECTIONS
        for change in diff_section(prev.get(section, []), curr.get(section, []), section)
    ]


def format_change(c):
    line = f"[{c.action}] {c.section}"
    if c.category:
        line += f" / {c.category}"
    line += f" · {c.name}"
    if c.action == "UPDATED":
        if c.edit_date != c.prev_edit_date:
            line += f"  edit_date: {c.prev_edit_date} → {c.edit_date}"
        if c.scope != c.prev_scope:
            line += f"  scope: {c.prev_scope} → {c.scope}"
    elif c.action == "NEW":
        line += f"  ({c.edit_date}, {c.scope})"
    elif c.action == "REMOVED":
        line += f"  (was: {c.prev_edit_date}, {c.prev_scope})"
    return line


def append_history(timestamp, url, changes):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with HISTORY_FILE.open("a", encoding="utf-8") as f:
        for change in changes:
            f.write(json.dumps({"ts": timestamp, "url": url, **asdict(change)}, ensure_ascii=False) + "\n")


def send_email(changes, url):
    user = os.environ.get("MAVAT_GMAIL_USER")
    password = os.environ.get("MAVAT_GMAIL_PASS")
    recipient = os.environ.get("MAVAT_NOTIFY_TO", user)
    if not user or not password:
        return "email skipped: MAVAT_GMAIL_USER / MAVAT_GMAIL_PASS not set"

    plan_id = url.rsplit("/", 2)[-2]
    body_lines = [
        f"{len(changes)} change(s) detected at {datetime.now().isoformat(timespec='seconds')}",
        f"Plan: {url}",
        "",
        *(format_change(c) for c in changes),
    ]
    message = EmailMessage()
    message["Subject"] = f"[Mavat] {len(changes)} change(s) on plan {plan_id}"
    message["From"] = user
    message["To"] = recipient
    message.set_content("\n".join(body_lines))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
            smtp.login(user, password)
            smtp.send_message(message)
        return f"email sent to {recipient}"
    except Exception as exc:
        return f"email failed: {type(exc).__name__}: {exc}"


def run_check(url, send_emails=True):
    timestamp = datetime.now().isoformat(timespec="seconds")
    current_rows = capture(url)
    state = load_state()
    previous_rows = rows_from_json(state.get("rows", {}))
    is_new_baseline = not any(previous_rows.values()) or state.get("url") != url

    save_state({"url": url, "last_check": timestamp, "rows": rows_to_json(current_rows)})

    if is_new_baseline:
        return {
            "first_run": True,
            "total_rows": sum(len(rows) for rows in current_rows.values()),
            "changes": [],
            "email_status": "",
        }

    changes = diff_all(previous_rows, current_rows)
    email_status = ""
    if changes:
        append_history(timestamp, url, changes)
        if send_emails:
            email_status = send_email(changes, url)

    return {
        "first_run": False,
        "total_rows": sum(len(rows) for rows in current_rows.values()),
        "changes": changes,
        "email_status": email_status,
    }


def simulate(fake_date="01/01/1900"):
    state = load_state()
    rows = rows_from_json(state.get("rows", {}))
    if not any(rows.values()):
        return False
    for row_list in rows.values():
        if row_list:
            row_list[0] = Row(**{**asdict(row_list[0]), "edit_date": fake_date})
    state["rows"] = rows_to_json(rows)
    save_state(state)
    return True


def main():
    parser = argparse.ArgumentParser(description="Watch a Mavat plan page for document changes.")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--simulate", action="store_true")
    parser.add_argument("--no-email", action="store_true")
    args = parser.parse_args()

    if args.simulate:
        if simulate():
            print("Mutated stored state. Next run will report UPDATED changes.")
            return 0
        print("No baseline yet — run once normally first.")
        return 1

    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] checking {args.url}")
    result = run_check(args.url, send_emails=not args.no_email)

    if result["first_run"]:
        print(f"Baseline recorded ({result['total_rows']} rows).")
        return 0
    if not result["changes"]:
        print("No changes.")
        return 0

    print(f"!! {len(result['changes'])} change(s):")
    for change in result["changes"]:
        print("  " + format_change(change))
    if result["email_status"]:
        print(result["email_status"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
