#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "playwright>=1.49",
# ]
# ///
"""
Watch a Mavat plan page for per-file changes under the three document sections:
  - מסמכי התכנית
  - מסמכי מידע מנהלי
  - נוסחי פרסום

Each detected change (NEW / REMOVED / UPDATED) is printed, appended to a JSONL
history log, and emailed via Gmail SMTP.

Usage:
  uv run ~/mavat_watch.py                  # one check; print + email + log any diffs
  uv run ~/mavat_watch.py --no-email       # skip the email step
  uv run ~/mavat_watch.py --simulate       # fake a row update so the next run triggers a notification
  uv run ~/mavat_watch.py --url <URL>      # different plan page
  uv run ~/mavat_watch.py --headed         # show browser (debugging)
  watch -n 600 -- uv run ~/mavat_watch.py  # poll every 10 minutes

Gmail SMTP configuration (env vars; if missing, email is skipped with a warning):
  MAVAT_GMAIL_USER  - gmail address you're sending FROM (must have an App Password)
  MAVAT_GMAIL_PASS  - 16-char App Password (Google Account → Security → 2FA → App passwords)
  MAVAT_NOTIFY_TO   - recipient address (defaults to MAVAT_GMAIL_USER)
"""

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
from typing import Any

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
    category: str   # sub-category heading like "תשריט"; "" if none
    name: str       # שם המסמך + description, joined with " — "
    scope: str      # תחולה (e.g. "מחייב")
    edit_date: str  # תאריך עריכה (raw text, e.g. "29/12/2020")

    @property
    def key(self) -> tuple[str, str]:
        return (self.section, self.name)


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------

# JS that extracts data rows from a single section's accordion-content panel.
# A "data row" is a .uk-grid that has a li-date cell (the date column). Headers
# (.sv4-headline) and category banners are skipped.
ROW_EXTRACT_JS = r"""
(el) => {
    const rows = [];
    // Track most-recently-seen sub-category heading (.uk-text-lead is the
    // visual marker Mavat uses for "תשריט", "תקנון" etc.).
    let lastCategory = '';
    const grids = el.querySelectorAll('.uk-grid');
    grids.forEach(g => {
        // Update category if this grid is itself (or contains a top-level) lead heading.
        const lead = g.querySelector(':scope > .uk-text-lead');
        if (lead) lastCategory = (lead.innerText || '').trim();

        if (g.classList.contains('sv4-headline')) return;        // header row
        // Look for the date cell: class contains 'li-date'
        const dateCell = [...g.children].find(c => /\bli-date\b/.test(c.className||''));
        if (!dateCell) return;

        const nameCell = [...g.children].find(c => /uk-width-expand|widthTitle/.test(c.className||''));
        const scopeCell = [...g.children].find(c => /\bli-file\b/.test(c.className||'') && !/\bli-date\b/.test(c.className||''));

        const name = nameCell ? (nameCell.innerText || '').trim().replace(/\s+/g, ' ') : '';
        const scope = scopeCell ? (scopeCell.innerText || '').trim() : '';
        const editDate = (dateCell.innerText || '').trim();

        rows.push({category: lastCategory, name, scope, edit_date: editDate});
    });
    return rows;
}
"""


def open_section(page, label: str):
    """Ensure the UIkit accordion <li> with this title is expanded. Return the <li> locator."""
    li = page.locator(
        f'ul.uk-accordion > li:has(> .uk-accordion-title:has-text("{label}"))'
    ).first
    li.wait_for(state="attached", timeout=15_000)
    li.scroll_into_view_if_needed(timeout=10_000)
    h = li.element_handle()
    if not h.evaluate("e => e.classList.contains('uk-open')"):
        li.locator(".uk-accordion-title").first.click(timeout=10_000)
        page.wait_for_function(
            "el => el.classList.contains('uk-open')", arg=h, timeout=10_000,
        )
        page.wait_for_timeout(1_500)
    return li


def expand_nested_accordions(content, page, max_passes: int = 10) -> None:
    """Open any nested UIkit accordion items inside `content` that are still closed."""
    for _ in range(max_passes):
        togs = content.locator("li:not(.uk-open) > .uk-accordion-title")
        n = togs.count()
        clicked = 0
        for i in range(n):
            try:
                t = togs.nth(i)
                if t.is_visible():
                    t.click(timeout=2_000)
                    clicked += 1
            except Exception:  # noqa: BLE001 — best-effort
                pass
        if clicked == 0:
            break
        page.wait_for_timeout(600)


def capture(url: str, headed: bool) -> dict[str, list[Row]]:
    """Return {section_label: [Row, ...]}."""
    out: dict[str, list[Row]] = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        ctx = browser.new_context(locale="he-IL", viewport={"width": 1500, "height": 1100})
        page = ctx.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(6_000)

        for label in SECTIONS:
            try:
                li = open_section(page, label)
                content = li.locator(".uk-accordion-content").first
                expand_nested_accordions(content, page)
                page.wait_for_timeout(800)
                raw = content.evaluate(ROW_EXTRACT_JS)
                out[label] = [
                    Row(section=label,
                        category=r.get("category", ""),
                        name=r.get("name", ""),
                        scope=r.get("scope", ""),
                        edit_date=r.get("edit_date", ""))
                    for r in raw if r.get("name")
                ]
            except PWTimeout:
                print(f"  WARN: section '{label}' not found / timed out", file=sys.stderr)
                out[label] = []
            except Exception as e:  # noqa: BLE001
                print(f"  WARN: section '{label}' error: {type(e).__name__}: {e}", file=sys.stderr)
                out[label] = []
        browser.close()
    return out


# ---------------------------------------------------------------------------
# State / diffing
# ---------------------------------------------------------------------------


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def rows_to_jsonable(rows_by_section: dict[str, list[Row]]) -> dict:
    return {sec: [asdict(r) for r in rows] for sec, rows in rows_by_section.items()}


def rows_from_jsonable(blob: dict) -> dict[str, list[Row]]:
    out: dict[str, list[Row]] = {}
    for sec, lst in (blob or {}).items():
        out[sec] = [Row(**r) for r in lst]
    return out


@dataclass
class Change:
    section: str
    action: str   # "NEW" | "REMOVED" | "UPDATED"
    name: str
    category: str = ""
    scope: str = ""
    edit_date: str = ""
    prev_edit_date: str = ""
    prev_scope: str = ""

    def short(self) -> str:
        bits = [f"[{self.action}]", self.section]
        if self.category:
            bits.append(f"/ {self.category}")
        bits.append(f"· {self.name}")
        if self.action == "UPDATED":
            if self.edit_date != self.prev_edit_date:
                bits.append(f"  edit_date: {self.prev_edit_date!r} → {self.edit_date!r}")
            if self.scope != self.prev_scope:
                bits.append(f"  scope: {self.prev_scope!r} → {self.scope!r}")
        elif self.action == "NEW":
            bits.append(f"  ({self.edit_date}, {self.scope})")
        elif self.action == "REMOVED":
            bits.append(f"  (was: {self.prev_edit_date}, {self.prev_scope})")
        return " ".join(bits)


def diff_section(prev: list[Row], curr: list[Row], section: str) -> list[Change]:
    prev_by_name = {r.name: r for r in prev}
    curr_by_name = {r.name: r for r in curr}
    changes: list[Change] = []
    for name, r in curr_by_name.items():
        if name not in prev_by_name:
            changes.append(Change(section=section, action="NEW", name=name,
                                  category=r.category, scope=r.scope, edit_date=r.edit_date))
        else:
            p = prev_by_name[name]
            if r.edit_date != p.edit_date or r.scope != p.scope:
                changes.append(Change(section=section, action="UPDATED", name=name,
                                      category=r.category, scope=r.scope, edit_date=r.edit_date,
                                      prev_scope=p.scope, prev_edit_date=p.edit_date))
    for name, p in prev_by_name.items():
        if name not in curr_by_name:
            changes.append(Change(section=section, action="REMOVED", name=name,
                                  category=p.category, prev_scope=p.scope, prev_edit_date=p.edit_date))
    return changes


def diff_all(prev: dict[str, list[Row]], curr: dict[str, list[Row]]) -> list[Change]:
    out: list[Change] = []
    for sec in SECTIONS:
        out.extend(diff_section(prev.get(sec, []), curr.get(sec, []), sec))
    return out


# ---------------------------------------------------------------------------
# History log
# ---------------------------------------------------------------------------


def append_history(now: str, url: str, changes: list[Change]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with HISTORY_FILE.open("a", encoding="utf-8") as f:
        for c in changes:
            f.write(json.dumps({"ts": now, "url": url, **asdict(c)}, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------


def send_email(changes: list[Change], url: str) -> str:
    """Send a digest email via Gmail SMTP. Returns a status string for printing."""
    user = os.environ.get("MAVAT_GMAIL_USER")
    pw = os.environ.get("MAVAT_GMAIL_PASS")
    to = os.environ.get("MAVAT_NOTIFY_TO", user)
    if not user or not pw:
        return ("email skipped: set MAVAT_GMAIL_USER and MAVAT_GMAIL_PASS env vars to enable "
                "(Gmail App Password required)")

    subject = f"[Mavat] {len(changes)} change(s) on plan {url.rsplit('/', 2)[-2]}"
    lines = [
        f"{len(changes)} change(s) detected at {datetime.now().isoformat(timespec='seconds')}",
        f"Plan: {url}",
        "",
    ]
    by_action = {"NEW": [], "UPDATED": [], "REMOVED": []}
    for c in changes:
        by_action[c.action].append(c)
    for action in ("NEW", "UPDATED", "REMOVED"):
        if not by_action[action]:
            continue
        lines.append(f"== {action} ({len(by_action[action])}) ==")
        for c in by_action[action]:
            lines.append(f"  • {c.section}" + (f" / {c.category}" if c.category else ""))
            lines.append(f"      {c.name}")
            if action == "NEW":
                lines.append(f"      date: {c.edit_date}   scope: {c.scope}")
            elif action == "UPDATED":
                if c.edit_date != c.prev_edit_date:
                    lines.append(f"      edit_date: {c.prev_edit_date} → {c.edit_date}")
                if c.scope != c.prev_scope:
                    lines.append(f"      scope: {c.prev_scope} → {c.scope}")
            elif action == "REMOVED":
                lines.append(f"      (was: date={c.prev_edit_date}, scope={c.prev_scope})")
        lines.append("")
    body = "\n".join(lines)

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to
    msg.set_content(body)

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
            s.login(user, pw)
            s.send_message(msg)
        return f"email sent to {to}"
    except smtplib.SMTPAuthenticationError as e:
        return f"email FAILED (auth): {e.smtp_code} {e.smtp_error!r} — App Password correct?"
    except Exception as e:  # noqa: BLE001
        return f"email FAILED: {type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--simulate", action="store_true",
                    help="Tamper with stored state so the next run reports an UPDATE on row 0 of each section.")
    ap.add_argument("--no-email", action="store_true", help="Skip sending the digest email.")
    ap.add_argument("--headed", action="store_true", help="Show the browser window (debugging).")
    args = ap.parse_args()

    state = load_state()

    if args.simulate:
        prev_rows = rows_from_jsonable(state.get("rows", {}))
        if not any(prev_rows.values()):
            print("No baseline yet — run once normally first.")
            return 1
        mutated = 0
        for sec, lst in prev_rows.items():
            if lst:
                lst[0] = Row(section=lst[0].section, category=lst[0].category, name=lst[0].name,
                             scope=lst[0].scope, edit_date="01/01/1900")
                mutated += 1
        state["rows"] = rows_to_jsonable(prev_rows)
        save_state(state)
        print(f"Mutated {mutated} stored row(s) (edit_date → 01/01/1900). Next run will report UPDATED changes.")
        return 0

    now = datetime.now().isoformat(timespec="seconds")
    print(f"[{now}] checking {args.url}")
    curr_rows = capture(args.url, headed=args.headed)
    total = sum(len(v) for v in curr_rows.values())
    print(f"  found {total} document row(s): " + ", ".join(f"{sec}={len(curr_rows[sec])}" for sec in SECTIONS))

    prev_rows = rows_from_jsonable(state.get("rows", {}))
    if not any(prev_rows.values()):
        save_state({"url": args.url, "last_check": now, "rows": rows_to_jsonable(curr_rows)})
        print("Baseline recorded.")
        return 0

    changes = diff_all(prev_rows, curr_rows)
    if not changes:
        state["last_check"] = now
        state["url"] = args.url
        state["rows"] = rows_to_jsonable(curr_rows)
        save_state(state)
        print("No changes.")
        return 0

    print(f"!! {len(changes)} change(s):")
    for c in changes:
        print("  " + c.short())

    append_history(now, args.url, changes)
    if args.no_email:
        print("(email skipped by --no-email)")
    else:
        print(send_email(changes, args.url))

    save_state({"url": args.url, "last_check": now, "rows": rows_to_jsonable(curr_rows)})
    return 0


if __name__ == "__main__":
    sys.exit(main())
