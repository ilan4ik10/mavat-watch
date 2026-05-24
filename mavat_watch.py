#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["playwright>=1.49", "pymupdf>=1.27"]
# ///
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import queue
import re
import smtplib
import sys
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

import psycopg
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

from pdfdiff import make_highlighted_pdf, mutate_one_number


def _step(label, t0):
    print(f"[time] {label}: {(time.perf_counter() - t0) * 1000:.0f} ms", flush=True)
    return time.perf_counter()

SECTIONS = ["מסמכי התכנית", "מסמכי מידע מנהלי", "נוסחי פרסום"]
FILES_DIR = Path.home() / ".cache" / "mavat-watch" / "files"
ACTION_LABEL_HE = {"NEW": "חדש", "UPDATED": "עודכן", "REMOVED": "הוסר"}
ACTION_COLOR = {"NEW": "#16a34a", "UPDATED": "#ea580c", "REMOVED": "#dc2626"}
ACTION_BG = {"NEW": "#f0fdf4", "UPDATED": "#fff7ed", "REMOVED": "#fef2f2"}
MAX_ATTACH_BYTES = 20 * 1024 * 1024

DATABASE_URL = os.environ["DATABASE_URL"]

SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS tracks (
    url           TEXT PRIMARY KEY,
    added_at      TEXT NOT NULL,
    last_check    TEXT NOT NULL,
    plan_number   TEXT NOT NULL DEFAULT '',
    plan_title    TEXT NOT NULL DEFAULT '',
    rows_json     JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE TABLE IF NOT EXISTS history (
    id              BIGSERIAL PRIMARY KEY,
    url             TEXT NOT NULL,
    ts              TEXT NOT NULL,
    section         TEXT NOT NULL,
    action          TEXT NOT NULL,
    name            TEXT NOT NULL,
    category        TEXT NOT NULL DEFAULT '',
    scope           TEXT NOT NULL DEFAULT '',
    edit_date       TEXT NOT NULL DEFAULT '',
    prev_scope      TEXT NOT NULL DEFAULT '',
    prev_edit_date  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_history_url_id ON history (url, id DESC);
"""


with psycopg.connect(DATABASE_URL) as _conn:
    _conn.execute(SCHEMA_DDL)
    _conn.commit()


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


OPEN_ALL_SECTIONS_JS = r"""
(sections) => {
    document.querySelectorAll('ul.uk-accordion > li').forEach(li => {
        const title = li.querySelector(':scope > .uk-accordion-title');
        if (!title) return;
        const text = (title.innerText || '').trim();
        if (sections.some(s => text.includes(s)) && !li.classList.contains('uk-open')) {
            title.click();
        }
    });
}
"""


WAIT_ALL_OPEN_JS = r"""
(sections) => {
    let opened = 0;
    document.querySelectorAll('ul.uk-accordion > li').forEach(li => {
        const title = li.querySelector(':scope > .uk-accordion-title');
        if (!title) return;
        const text = (title.innerText || '').trim();
        if (sections.some(s => text.includes(s))) {
            const content = li.querySelector(':scope > .uk-accordion-content');
            if (li.classList.contains('uk-open') && content && content.children.length > 0) opened++;
        }
    });
    return opened === sections.length;
}
"""


EXPAND_NESTED_JS = r"""
() => {
    let n = 0;
    document.querySelectorAll('li.uk-open .uk-accordion-content li:not(.uk-open) > .uk-accordion-title')
        .forEach(t => {
            const r = t.getBoundingClientRect();
            if (r.width > 0 || r.height > 0) { t.click(); n++; }
        });
    return n;
}
"""


EXTRACT_ALL_ROWS_JS = r"""
(sections) => {
    const result = {};
    document.querySelectorAll('ul.uk-accordion > li').forEach(li => {
        const title = li.querySelector(':scope > .uk-accordion-title');
        if (!title) return;
        const text = (title.innerText || '').trim();
        const match = sections.find(s => text.includes(s));
        if (!match) return;
        const panel = li.querySelector(':scope > .uk-accordion-content');
        if (!panel) { result[match] = []; return; }
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
        result[match] = rows;
    });
    return result;
}
"""


def url_id(url):
    match = re.search(r"/SV4/\d+/(\d+)(?:/(\d+))?", url)
    if match:
        return f"{match.group(1)}_{match.group(2) or 'default'}"
    return re.sub(r"\W+", "_", url).strip("_")[:80] or "untitled"


def plan_label(url):
    track = load_track(url)
    if track and track.get("plan_number"):
        return f"תוכנית {track['plan_number']}"
    return "תוכנית (טוען…)"


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


EXTRACT_PLAN_INFO_JS = r"""
() => {
    const h1 = document.querySelector('h1.plan-name');
    const number = h1 ? h1.innerText.replace(/[^0-9-]/g, '').trim() : '';
    let title = '';
    document.querySelectorAll('h3').forEach(h => {
        const t = (h.innerText || '').trim();
        if (number && t.startsWith(number) && t.includes('|')) {
            title = t.split('|').slice(1).join('|').trim();
        }
    });
    return {number, title};
}
"""


def _do_capture(page, url):
    print(f"[time] === capture start: {url} ===", flush=True)
    start = time.perf_counter()
    t = start
    rows = {section: [] for section in SECTIONS}
    plan_number = ""
    plan_title = ""

    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    t = _step("goto + domcontentloaded", t)

    try:
        page.wait_for_function(
            "() => !!document.querySelector('h1.plan-name') && !!document.querySelector('ul.uk-accordion > li')",
            timeout=60_000,
        )
        t = _step("wait for h1 + accordion (combined)", t)
    except PWTimeout:
        t = _step("wait for h1 + accordion (combined) TIMEOUT", t)

    page.wait_for_timeout(300)
    t = _step("settle 300ms", t)

    try:
        info = page.evaluate(EXTRACT_PLAN_INFO_JS)
        plan_number = info.get("number", "") or ""
        plan_title = info.get("title", "") or ""
    except Exception:
        pass
    t = _step(f"extract plan info (number={plan_number!r})", t)

    try:
        page.evaluate(OPEN_ALL_SECTIONS_JS, SECTIONS)
    except Exception:
        pass
    t = _step("click all 3 sections", t)

    try:
        page.wait_for_function(WAIT_ALL_OPEN_JS, arg=SECTIONS, timeout=15_000)
        t = _step("wait for all 3 sections open", t)
    except PWTimeout:
        t = _step("wait for all 3 sections open TIMEOUT", t)

    expand_iters = 0
    expand_clicks = 0
    for _ in range(8):
        try:
            clicked = page.evaluate(EXPAND_NESTED_JS)
        except Exception:
            clicked = 0
        if not clicked:
            break
        expand_iters += 1
        expand_clicks += clicked
        page.wait_for_timeout(400)
    t = _step(f"expand nested ({expand_iters} iters, {expand_clicks} clicks)", t)

    page.wait_for_timeout(500)
    t = _step("final settle 500ms", t)

    try:
        all_data = page.evaluate(EXTRACT_ALL_ROWS_JS, SECTIONS)
    except Exception:
        all_data = {}
    for label in SECTIONS:
        for r in all_data.get(label, []):
            if r.get("name"):
                rows[label].append(Row(
                    section=label, category=r["category"], name=r["name"],
                    scope=r["scope"], edit_date=r["edit_date"],
                ))
    counts = {label: len(rows[label]) for label in SECTIONS}
    t = _step(f"extract all rows {counts}", t)

    total_ms = (time.perf_counter() - start) * 1000
    print(f"[time] === capture done in {total_ms:.0f} ms ===", flush=True)
    return {"rows": rows, "plan_number": plan_number, "plan_title": plan_title}


_capture_queue: queue.Queue = queue.Queue()
_capture_worker_started = False


def _new_ctx(browser):
    return browser.new_context(locale="he-IL", viewport={"width": 1500, "height": 1100})


def _prewarm_mavat(ctx):
    try:
        t = time.perf_counter()
        wp = ctx.new_page()
        wp.goto("https://mavat.iplan.gov.il/", wait_until="domcontentloaded", timeout=60_000)
        wp.wait_for_timeout(3_000)
        wp.close()
        _step("prewarm mavat homepage", t)
    except Exception as exc:
        print(f"[browser] prewarm failed: {type(exc).__name__}: {exc}", flush=True)


def _capture_worker():
    print("[browser] starting capture worker", flush=True)
    try:
        t = time.perf_counter()
        pw = sync_playwright().start()
        t = _step("sync_playwright().start()", t)
        browser = pw.chromium.launch(headless=True)
        t = _step("chromium.launch()", t)
        ctx = _new_ctx(browser)
        t = _step("shared context", t)
        _prewarm_mavat(ctx)
        print("[browser] capture worker ready", flush=True)
    except Exception as exc:
        print(f"[browser] worker failed to start: {type(exc).__name__}: {exc}", flush=True)
        return

    while True:
        url, result_q = _capture_queue.get()
        if url is None:
            break
        try:
            p_t = time.perf_counter()
            page = ctx.new_page()
            _step("new_page (shared ctx)", p_t)
            try:
                result = _do_capture(page, url)
                result_q.put(("ok", result))
            finally:
                close_t = time.perf_counter()
                try:
                    page.close()
                except Exception:
                    pass
                _step("page.close()", close_t)
        except Exception as exc:
            result_q.put(("err", exc))
            try:
                ctx.close()
            except Exception:
                pass
            try:
                browser.close()
            except Exception:
                pass
            try:
                browser = pw.chromium.launch(headless=True)
                ctx = _new_ctx(browser)
                print("[browser] worker restarted browser+context after error", flush=True)
            except Exception as exc2:
                print(f"[browser] restart failed: {exc2}", flush=True)
                return


def _ensure_capture_worker():
    global _capture_worker_started
    if not _capture_worker_started:
        _capture_worker_started = True
        threading.Thread(target=_capture_worker, daemon=True).start()


def capture(url):
    _ensure_capture_worker()
    enq_t = time.perf_counter()
    result_q: queue.Queue = queue.Queue()
    _capture_queue.put((url, result_q))
    status, payload = result_q.get(timeout=180)
    _step("capture() incl. queue wait", enq_t)
    if status == "err":
        raise payload
    return payload


_ensure_capture_worker()


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
    line = f"[{ACTION_LABEL_HE[c.action]}] {c.section}"
    if c.category:
        line += f" / {c.category}"
    line += f" · {c.name}"
    if c.action == "UPDATED":
        if c.edit_date != c.prev_edit_date:
            line += f"  תאריך עריכה: {c.prev_edit_date} → {c.edit_date}"
        if c.scope != c.prev_scope:
            line += f"  תחולה: {c.prev_scope} → {c.scope}"
    elif c.action == "NEW":
        line += f"  ({c.edit_date}, {c.scope})"
    elif c.action == "REMOVED":
        line += f"  (היה: {c.prev_edit_date}, {c.prev_scope})"
    return line


def load_track(url):
    with psycopg.connect(DATABASE_URL) as conn:
        row = conn.execute(
            "SELECT url, added_at, last_check, plan_number, plan_title, rows_json FROM tracks WHERE url = %s",
            (url,),
        ).fetchone()
    if not row:
        return None
    return {
        "url": row[0], "added_at": row[1], "last_check": row[2],
        "plan_number": row[3], "plan_title": row[4],
        "rows": row[5] or {},
    }


def save_track(url, track):
    with psycopg.connect(DATABASE_URL) as conn:
        conn.execute(
            """
            INSERT INTO tracks (url, added_at, last_check, plan_number, plan_title, rows_json)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (url) DO UPDATE SET
                last_check  = EXCLUDED.last_check,
                plan_number = EXCLUDED.plan_number,
                plan_title  = EXCLUDED.plan_title,
                rows_json   = EXCLUDED.rows_json
            """,
            (
                track["url"], track["added_at"], track["last_check"],
                track.get("plan_number", ""), track.get("plan_title", ""),
                json.dumps(track.get("rows", {}), ensure_ascii=False),
            ),
        )
        conn.commit()


def list_tracks():
    with psycopg.connect(DATABASE_URL) as conn:
        rows = conn.execute(
            "SELECT url, added_at, last_check, plan_number, plan_title, rows_json FROM tracks ORDER BY added_at"
        ).fetchall()
    return [
        {"url": r[0], "added_at": r[1], "last_check": r[2],
         "plan_number": r[3], "plan_title": r[4], "rows": r[5] or {}}
        for r in rows
    ]


def remove_track(url):
    with psycopg.connect(DATABASE_URL) as conn:
        conn.execute("DELETE FROM history WHERE url = %s", (url,))
        cur = conn.execute("DELETE FROM tracks WHERE url = %s", (url,))
        removed = cur.rowcount > 0
        conn.commit()
    return removed


def append_history(url, timestamp, changes):
    with psycopg.connect(DATABASE_URL) as conn:
        for c in changes:
            conn.execute(
                """
                INSERT INTO history
                  (url, ts, section, action, name, category, scope, edit_date, prev_scope, prev_edit_date)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (url, timestamp, c.section, c.action, c.name, c.category,
                 c.scope, c.edit_date, c.prev_scope, c.prev_edit_date),
            )
        conn.commit()


def load_history(url, limit=20):
    with psycopg.connect(DATABASE_URL) as conn:
        rows = conn.execute(
            """
            SELECT ts, section, action, name, category, scope, edit_date, prev_scope, prev_edit_date
            FROM history WHERE url = %s ORDER BY id DESC LIMIT %s
            """,
            (url, limit),
        ).fetchall()
    return [
        {"ts": r[0], "section": r[1], "action": r[2], "name": r[3], "category": r[4],
         "scope": r[5], "edit_date": r[6], "prev_scope": r[7], "prev_edit_date": r[8]}
        for r in rows
    ]


def history_count(url):
    with psycopg.connect(DATABASE_URL) as conn:
        return conn.execute("SELECT COUNT(*) FROM history WHERE url = %s", (url,)).fetchone()[0]


def _safe_row(name):
    return re.sub(r"[^\w-]+", "_", name).strip("_")[:80] or "row"


def _find_previous_version(out_dir, safe_row, exclude):
    marker = f"__{safe_row}__"
    candidates = [p for p in out_dir.iterdir()
                  if p.is_file() and p != exclude
                  and marker in p.name and "-highlighted" not in p.stem]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def download_changed_files(url, changes):
    targets = [c for c in changes if c.action in ("NEW", "UPDATED")]
    print(f"[download] {len(targets)} target(s) to download from {url}", flush=True)
    if not targets:
        return []
    out_dir = FILES_DIR / url_id(url)
    out_dir.mkdir(parents=True, exist_ok=True)
    saved = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            locale="he-IL", viewport={"width": 1500, "height": 1100}, accept_downloads=True,
        )
        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(6_000)
        for label in SECTIONS:
            try:
                section = open_section(page, label)
                expand_nested_accordions(section.locator(".uk-accordion-content").first, page)
            except Exception as exc:
                print(f"[download]   failed to open section '{label}': {type(exc).__name__}: {exc}", flush=True)
        page.wait_for_timeout(1_500)

        for change in targets:
            entry = {"action": change.action, "name": change.name, "section": change.section}
            print(f"[download] → {change.action}: {change.section} / {change.name!r}", flush=True)
            try:
                marker = page.evaluate(
                    r"""(args) => {
                        const [sectionLabel, targetName] = args;
                        document.querySelectorAll('[data-mavat-target]').forEach(e => e.removeAttribute('data-mavat-target'));
                        let panel = null;
                        document.querySelectorAll('ul.uk-accordion > li').forEach(li => {
                            const title = li.querySelector(':scope > .uk-accordion-title');
                            if (title && (title.innerText || '').includes(sectionLabel)) {
                                panel = li.querySelector(':scope > .uk-accordion-content');
                            }
                        });
                        if (!panel) return {found: false, reason: 'section panel not found / not open'};
                        let candidates = 0;
                        for (const g of panel.querySelectorAll('.uk-grid')) {
                            if (g.classList.contains('sv4-headline')) continue;
                            const cells = [...g.children];
                            const dateCell = cells.find(c => /\bli-date\b/.test(c.className || ''));
                            if (!dateCell) continue;
                            candidates++;
                            const nameCell = cells.find(c => /uk-width-expand|widthTitle/.test(c.className || ''));
                            if (!nameCell) continue;
                            const name = (nameCell.innerText || '').trim().replace(/\s+/g, ' ');
                            if (name === targetName) {
                                g.setAttribute('data-mavat-target', '1');
                                return {found: true};
                            }
                        }
                        return {found: false, reason: `not matched in ${candidates} data rows`};
                    }""",
                    [change.section, change.name],
                )
                print(f"[download]   row match: {marker}", flush=True)
                if not marker.get("found"):
                    raise RuntimeError(marker.get("reason", "no row matched"))
                row = page.locator('[data-mavat-target="1"]').first
                row.scroll_into_view_if_needed(timeout=5_000)
                buttons = row.locator(".fileIcon.download")
                btn_count = buttons.count()
                print(f"[download]   download buttons in row: {btn_count}", flush=True)
                if btn_count == 0:
                    raise RuntimeError("no .fileIcon.download button in row")
                with page.expect_download(timeout=20_000) as dl:
                    buttons.first.click(timeout=8_000)
                download = dl.value
                suggested = download.suggested_filename or f"{url_id(url)}_doc"
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                safe = _safe_row(change.name)
                target = out_dir / f"{stamp}__{safe}__{suggested}"
                download.save_as(target)
                size = target.stat().st_size
                entry["path"] = str(target)
                entry["filename"] = suggested
                entry["row"] = change.name
                entry["size"] = size
                print(f"[download]   ✓ saved {target.name} ({size} bytes)", flush=True)
                if change.action == "UPDATED" and target.suffix.lower() == ".pdf":
                    prev = _find_previous_version(out_dir, safe, target)
                    if prev:
                        stem = target.stem
                        hl_target = out_dir / f"{stem}-highlighted.pdf"
                        try:
                            n = make_highlighted_pdf(prev, target, hl_target)
                            if n > 0:
                                entry["highlighted_path"] = str(hl_target)
                                entry["highlighted_filename"] = f"{Path(suggested).stem}-highlighted.pdf"
                                entry["highlighted_size"] = hl_target.stat().st_size
                                print(f"[download]   ✓ highlighted {n} change(s) vs {prev.name} -> {hl_target}", flush=True)
                            else:
                                print(f"[download]   no text-level diff vs {prev.name} (image-only or identical text)", flush=True)
                        except Exception as exc:
                            print(f"[download]   pdfdiff failed: {type(exc).__name__}: {exc}", flush=True)
            except Exception as exc:
                entry["error"] = f"{type(exc).__name__}: {exc}"
                print(f"[download]   ✗ {type(exc).__name__}: {exc}", flush=True)
            saved.append(entry)
        browser.close()
    print(f"[download] done — {sum(1 for e in saved if 'path' in e)}/{len(saved)} succeeded", flush=True)
    return saved


def build_email_html(changes, url, files):
    files_by_name = {f["name"]: f for f in files}
    cards = []
    for c in changes:
        color = ACTION_COLOR[c.action]
        bg = ACTION_BG[c.action]
        action_he = ACTION_LABEL_HE[c.action]
        detail = ""
        if c.action == "UPDATED":
            parts = []
            if c.edit_date != c.prev_edit_date:
                parts.append(
                    f'תאריך עריכה: <span style="text-decoration:line-through;color:#9ca3af">{c.prev_edit_date}</span> ← <strong>{c.edit_date}</strong>'
                )
            if c.scope != c.prev_scope:
                parts.append(
                    f'תחולה: <span style="text-decoration:line-through;color:#9ca3af">{c.prev_scope or "—"}</span> ← <strong>{c.scope or "—"}</strong>'
                )
            detail = "<br>".join(parts)
        elif c.action == "NEW":
            detail = f"תאריך עריכה: <strong>{c.edit_date}</strong>"
            if c.scope:
                detail += f" · תחולה: <strong>{c.scope}</strong>"
        elif c.action == "REMOVED":
            detail = f"היה: {c.prev_edit_date or '—'}"

        file_line = ""
        f = files_by_name.get(c.name)
        if f:
            if "filename" in f:
                if f.get("size", 0) > MAX_ATTACH_BYTES:
                    file_line = f'<div style="font-size:12px;color:#6b7280;margin-top:8px">📎 הקובץ גדול מדי לצירוף ({f["size"] // 1024 // 1024} MB) — שמור מקומית.</div>'
                else:
                    file_line = f'<div style="font-size:12px;color:#6b7280;margin-top:8px">📎 מצורף: {f["filename"]}</div>'
                    if f.get("highlighted_filename") and f.get("highlighted_size", 0) <= MAX_ATTACH_BYTES:
                        file_line += f'<div style="font-size:12px;color:#16a34a;margin-top:4px">🟢 שינויים מודגשים: {f["highlighted_filename"]}</div>'
            elif "error" in f:
                file_line = '<div style="font-size:12px;color:#9ca3af;margin-top:8px">⚠️ לא ניתן להוריד את הקובץ אוטומטית.</div>'

        category_line = f" / {c.category}" if c.category else ""
        cards.append(f"""
<div dir="rtl" style="background:{bg};border-right:3px solid {color};padding:14px 16px;margin-bottom:10px;border-radius:6px;direction:rtl;text-align:right">
  <div style="display:inline-block;background:{color};color:#fff;font-size:10px;font-weight:700;padding:3px 10px;border-radius:10px;letter-spacing:0.5px">{action_he}</div>
  <div style="font-weight:600;color:#111827;font-size:15px;margin-top:8px;direction:rtl;text-align:right">{c.name}</div>
  <div style="font-size:12px;color:#6b7280;margin-top:2px;direction:rtl;text-align:right">{c.section}{category_line}</div>
  <div style="font-size:13px;color:#374151;margin-top:8px;direction:rtl;text-align:right">{detail}</div>
  {file_line}
</div>""")
    return f"""<!doctype html>
<html lang="he" dir="rtl">
<body style="margin:0;padding:24px;background:#f7f8fa;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#111827;direction:rtl;text-align:right">
  <div dir="rtl" style="max-width:560px;margin:0 auto;background:#fff;border-radius:12px;padding:24px;border:1px solid #e5e7eb;direction:rtl;text-align:right;unicode-bidi:isolate">
    <div style="font-size:22px;font-weight:700;color:#111827;direction:rtl;text-align:right">{plan_label(url)}</div>
    <div style="font-size:14px;color:#6b7280;margin:4px 0 18px;direction:rtl;text-align:right">זוהו {len(changes)} שינויים</div>
    {''.join(cards)}
    <a href="{url}" style="display:inline-block;margin-top:14px;padding:10px 16px;background:#2563eb;color:#fff;text-decoration:none;font-size:14px;font-weight:500;border-radius:6px;direction:rtl">פתח את עמוד התכנית</a>
  </div>
</body>
</html>"""


def build_email_plain(changes, url):
    lines = [f"זוהו {len(changes)} שינויים בתכנית {plan_label(url)}", ""]
    for c in changes:
        lines.append(f"• [{ACTION_LABEL_HE[c.action]}] {c.name}")
        if c.action == "UPDATED" and c.edit_date != c.prev_edit_date:
            lines.append(f"    תאריך עריכה: {c.prev_edit_date} → {c.edit_date}")
        elif c.action == "NEW":
            lines.append(f"    תאריך עריכה: {c.edit_date}")
    lines.append("")
    lines.append(url)
    return "\n".join(lines)


def build_email_subject(changes, url):
    label = plan_label(url)
    if len(changes) == 1:
        c = changes[0]
        return f"[מבאת] {label} · {ACTION_LABEL_HE[c.action]}: {c.name[:60]}"
    return f"[מבאת] {label} · {len(changes)} שינויים"


def send_whatsapp(changes, url):
    import urllib.request
    token = os.environ.get("WHAPI_TOKEN")
    to = os.environ.get("WHAPI_TO")
    if not token or not to:
        return "whatsapp דולג: WHAPI_TOKEN / WHAPI_TO לא הוגדרו"

    label = plan_label(url)
    lines = [f"📋 {label}", f"זוהו {len(changes)} שינויים", ""]
    for c in changes:
        tag = ACTION_LABEL_HE[c.action]
        line = f"• [{tag}] {c.name}"
        if c.action == "UPDATED" and c.edit_date != c.prev_edit_date:
            line += f" — תאריך עריכה: {c.prev_edit_date} → {c.edit_date}"
        elif c.action == "NEW":
            line += f" — תאריך עריכה: {c.edit_date}"
        lines.append(line)
    lines.append("")
    lines.append(url)
    body = "\n".join(lines)

    payload = json.dumps({"to": to, "body": body}).encode("utf-8")
    req = urllib.request.Request(
        "https://gate.whapi.cloud/messages/text",
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "mavat-watch/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            r.read()
        return f"whatsapp נשלח אל {to}"
    except urllib.error.HTTPError as exc:
        try:
            body_bytes = exc.read()
            err_body = body_bytes.decode("utf-8", errors="replace")[:200]
        except Exception:
            err_body = ""
        return f"whatsapp נכשל: HTTP {exc.code} — {err_body}"
    except Exception as exc:
        return f"whatsapp נכשל: {type(exc).__name__}: {exc}"


def send_email(changes, url, files=None):
    user = os.environ.get("MAVAT_GMAIL_USER")
    password = os.environ.get("MAVAT_GMAIL_PASS")
    recipient = os.environ.get("MAVAT_NOTIFY_TO", user)
    if not user or not password:
        return "מייל לא נשלח: לא הוגדרו MAVAT_GMAIL_USER / MAVAT_GMAIL_PASS"

    files = files or []
    message = EmailMessage()
    message["Subject"] = build_email_subject(changes, url)
    message["From"] = user
    message["To"] = recipient
    message.set_content(build_email_plain(changes, url))
    message.add_alternative(build_email_html(changes, url, files), subtype="html")

    print(f"[email] received {len(files)} file record(s) from downloader", flush=True)
    attached = 0
    for f in files:
        for path_key, name_key, size_key in (
            ("path", "filename", "size"),
            ("highlighted_path", "highlighted_filename", "highlighted_size"),
        ):
            path = f.get(path_key)
            if not path:
                if path_key == "path":
                    print(f"[email]   skip (no path): {f.get('name')} — {f.get('error', 'unknown')}", flush=True)
                continue
            size = f.get(size_key, 0)
            if size > MAX_ATTACH_BYTES:
                print(f"[email]   skip (too large, {size} bytes > {MAX_ATTACH_BYTES}): {f[name_key]}", flush=True)
                continue
            mime, _ = mimetypes.guess_type(f[name_key])
            maintype, subtype = (mime.split("/", 1) if mime else ("application", "octet-stream"))
            with open(path, "rb") as fh:
                message.add_attachment(fh.read(), maintype=maintype, subtype=subtype, filename=f[name_key])
            attached += 1
            print(f"[email]   attached {f[name_key]} ({size} bytes, {maintype}/{subtype})", flush=True)
    print(f"[email] {attached} attachment(s) added to message", flush=True)

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as smtp:
            smtp.login(user, password)
            smtp.send_message(message)
        suffix = f" ({attached} קבצים מצורפים)" if attached else ""
        return f"המייל נשלח אל {recipient}{suffix}"
    except Exception as exc:
        return f"שליחת המייל נכשלה: {type(exc).__name__}: {exc}"


def add_track(url):
    add_t = time.perf_counter()
    print(f"[time] === add_track start: {url} ===", flush=True)
    t = add_t
    if load_track(url):
        _step("load_track (exists check)", t)
        return {"status": "exists", "url": url}
    t = _step("load_track (exists check)", t)
    timestamp = datetime.now().isoformat(timespec="seconds")
    snapshot = capture(url)
    t = _step("add_track: capture() returned", t)
    rows = snapshot["rows"]
    total = sum(len(v) for v in rows.values())
    if total == 0:
        print(f"[add] refusing to save empty baseline for {url}", flush=True)
        return {"status": "capture_failed", "url": url, "total_rows": 0}
    save_track(url, {
        "url": url,
        "added_at": timestamp,
        "last_check": timestamp,
        "plan_number": snapshot["plan_number"],
        "plan_title": snapshot["plan_title"],
        "rows": rows_to_json(rows),
    })
    _step("save_track", t)
    total_ms = (time.perf_counter() - add_t) * 1000
    print(f"[time] === add_track done in {total_ms:.0f} ms ===", flush=True)
    return {"status": "added", "url": url, "total_rows": total}


def check_track(url, send_emails=True):
    track = load_track(url)
    if not track:
        return {"status": "not_tracked", "url": url}
    timestamp = datetime.now().isoformat(timespec="seconds")
    snapshot = capture(url)
    current_rows = snapshot["rows"]
    previous_rows = rows_from_json(track.get("rows", {}))

    for section in SECTIONS:
        prev_section = previous_rows.get(section, [])
        if not current_rows[section] and prev_section:
            print(f"[check] section '{section}' returned 0 rows for {url}; preserving previous {len(prev_section)} rows", flush=True)
            current_rows[section] = prev_section

    total_current = sum(len(rows) for rows in current_rows.values())
    total_previous = sum(len(rows) for rows in previous_rows.values())

    if total_current == 0 and total_previous > 0:
        print(f"[check] capture returned 0 rows for {url} but previous had {total_previous}; treating as failure", flush=True)
        track["last_check"] = timestamp
        save_track(url, track)
        return {"status": "capture_failed", "url": url, "total_rows": 0, "changes": [], "email_status": "", "files": []}

    if total_previous == 0 and total_current > 0:
        print(f"[check] previous baseline was empty for {url}; rebasing to {total_current} rows without writing history", flush=True)
        track["last_check"] = timestamp
        track["plan_number"] = snapshot["plan_number"] or track.get("plan_number", "")
        track["plan_title"] = snapshot["plan_title"] or track.get("plan_title", "")
        track["rows"] = rows_to_json(current_rows)
        save_track(url, track)
        return {"status": "rebased", "url": url, "total_rows": total_current, "changes": [], "email_status": "", "files": []}

    changes = diff_all(previous_rows, current_rows)

    track["last_check"] = timestamp
    track["plan_number"] = snapshot["plan_number"] or track.get("plan_number", "")
    track["plan_title"] = snapshot["plan_title"] or track.get("plan_title", "")
    track["rows"] = rows_to_json(current_rows)
    save_track(url, track)

    email_status = ""
    whatsapp_status = ""
    files = []
    if changes:
        append_history(url, timestamp, changes)
        if send_emails:
            files = download_changed_files(url, changes)
            email_status = send_email(changes, url, files=files)
            whatsapp_status = send_whatsapp(changes, url)
            print(f"[whatsapp] {whatsapp_status}", flush=True)

    return {
        "status": "checked",
        "url": url,
        "total_rows": total_current,
        "changes": changes,
        "email_status": email_status,
        "whatsapp_status": whatsapp_status,
        "files": files,
    }


def simulate_track(url, fake_date="01/01/1900", target_name=None):
    track = load_track(url)
    if not track:
        return False
    rows = rows_from_json(track.get("rows", {}))
    for section in SECTIONS:
        section_rows = rows.get(section, [])
        for i, row in enumerate(section_rows):
            if target_name is None or row.name == target_name:
                section_rows[i] = Row(**{**asdict(row), "edit_date": fake_date})
                track["rows"] = rows_to_json(rows)
                save_track(url, track)
                return True
            if target_name is None:
                break
    return False


def simulate_pdf_change(url):
    """For demos: pick a tracked row whose saved PDF can be mutated, change
    one number inside that PDF, then tamper that specific row's edit_date in
    the DB. On the next check, the new (real) download differs from the
    mutated file, producing a visible highlight diff. Looks up PDFs by row
    name via the __<safe_row>__ pattern in filenames; falls back to legacy
    (any-PDF) iteration for files saved before the naming refactor."""
    out_dir = FILES_DIR / url_id(url)
    if not out_dir.exists():
        return False
    track = load_track(url)
    if not track:
        return False
    rows_by_section = track.get("rows", {})

    for section in SECTIONS:
        for row in rows_by_section.get(section, []):
            name = row["name"] if isinstance(row, dict) else row.name
            if not name:
                continue
            safe = _safe_row(name)
            marker = f"__{safe}__"
            pdfs = sorted(
                (p for p in out_dir.glob("*.pdf")
                 if "-highlighted" not in p.stem and marker in p.name),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if pdfs and mutate_one_number(pdfs[0]):
                print(f"[simulate-pdf] mutated {pdfs[0].name} for row {name!r}", flush=True)
                return simulate_track(url, target_name=name)

    print(f"[simulate-pdf] no row-tagged PDF mutated; falling back to legacy scan", flush=True)
    legacy = sorted(
        (p for p in out_dir.glob("*.pdf")
         if "-highlighted" not in p.stem and "__" not in p.name),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    target_name = None
    for section in SECTIONS:
        rows = rows_by_section.get(section, [])
        if rows:
            first = rows[0]
            target_name = first["name"] if isinstance(first, dict) else first.name
            break
    for pdf in legacy:
        if mutate_one_number(pdf):
            print(f"[simulate-pdf] mutated legacy {pdf.name}", flush=True)
            if target_name:
                import shutil
                safe = _safe_row(target_name)
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                tagged = out_dir / f"{stamp}__{safe}__legacy_{pdf.name}"
                shutil.copy(pdf, tagged)
                print(f"[simulate-pdf] tagged copy for diff: {tagged.name}", flush=True)
            return simulate_track(url, target_name=target_name)
    print(f"[simulate-pdf] no mutable PDF found", flush=True)
    return False


def check_all(send_emails=True):
    results = []
    for track in list_tracks():
        results.append(check_track(track["url"], send_emails=send_emails))
    return results


def main():
    parser = argparse.ArgumentParser(description="Watch multiple Mavat plan pages.")
    subparsers = parser.add_subparsers(dest="cmd")
    subparsers.add_parser("list")
    add_cmd = subparsers.add_parser("add"); add_cmd.add_argument("url")
    remove_cmd = subparsers.add_parser("remove"); remove_cmd.add_argument("url")
    simulate_cmd = subparsers.add_parser("simulate"); simulate_cmd.add_argument("url")
    check_cmd = subparsers.add_parser("check"); check_cmd.add_argument("url"); check_cmd.add_argument("--no-email", action="store_true")
    check_all_cmd = subparsers.add_parser("check-all"); check_all_cmd.add_argument("--no-email", action="store_true")
    args = parser.parse_args()

    if not args.cmd:
        args.cmd = "check-all"
        args.no_email = False

    if args.cmd == "list":
        tracks = list_tracks()
        if not tracks:
            print("no tracked URLs")
            return 0
        for track in tracks:
            print(f"- {plan_label(track['url'])}  ({track['url']})")
            print(f"    added {track['added_at']}, last check {track['last_check']}")
        return 0

    if args.cmd == "add":
        result = add_track(args.url)
        if result["status"] == "exists":
            print(f"already tracking {args.url}")
        else:
            print(f"added {plan_label(args.url)} with {result['total_rows']} docs as baseline")
        return 0

    if args.cmd == "remove":
        if remove_track(args.url):
            print(f"removed {plan_label(args.url)}")
            return 0
        print(f"not tracked: {args.url}")
        return 1

    if args.cmd == "simulate":
        if simulate_track(args.url):
            print(f"tampered baseline for {plan_label(args.url)}; next check will report changes")
            return 0
        print(f"not tracked: {args.url}")
        return 1

    if args.cmd == "check":
        result = check_track(args.url, send_emails=not args.no_email)
        print_check_result(result)
        return 0

    if args.cmd == "check-all":
        for result in check_all(send_emails=not args.no_email):
            print_check_result(result)
        return 0

    return 0


def print_check_result(result):
    if result["status"] == "not_tracked":
        print(f"not tracked: {result['url']}")
        return
    label = plan_label(result["url"])
    if not result["changes"]:
        print(f"{label}: no changes ({result['total_rows']} docs)")
        return
    print(f"{label}: {len(result['changes'])} change(s)")
    for change in result["changes"]:
        print("  " + format_change(change))
    if result["email_status"]:
        print(f"  {result['email_status']}")


if __name__ == "__main__":
    sys.exit(main())
