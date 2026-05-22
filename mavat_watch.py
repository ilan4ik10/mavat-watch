#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["playwright>=1.49"]
# ///
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import smtplib
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

SECTIONS = ["מסמכי התכנית", "מסמכי מידע מנהלי", "נוסחי פרסום"]
TRACKS_DIR = Path.home() / ".cache" / "mavat-watch" / "tracks"
FILES_DIR = Path.home() / ".cache" / "mavat-watch" / "files"
ACTION_LABEL_HE = {"NEW": "חדש", "UPDATED": "עודכן", "REMOVED": "הוסר"}
ACTION_COLOR = {"NEW": "#16a34a", "UPDATED": "#ea580c", "REMOVED": "#dc2626"}
ACTION_BG = {"NEW": "#f0fdf4", "UPDATED": "#fff7ed", "REMOVED": "#fef2f2"}
MAX_ATTACH_BYTES = 20 * 1024 * 1024

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
USE_DB = bool(DATABASE_URL)

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


def init_db():
    if not USE_DB:
        return
    import psycopg
    with psycopg.connect(DATABASE_URL) as conn:
        conn.execute(SCHEMA_DDL)
        conn.commit()
    print(f"[db] connected, schema ready", flush=True)


init_db()


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


def url_id(url):
    match = re.search(r"/SV4/\d+/(\d+)(?:/(\d+))?", url)
    if match:
        return f"{match.group(1)}_{match.group(2) or 'default'}"
    return re.sub(r"\W+", "_", url).strip("_")[:80] or "untitled"


def plan_label(url):
    match = re.search(r"/SV4/\d+/(\d+)(?:/(\d+))?", url)
    if match:
        return f"תוכנית {match.group(1)} · סוג {match.group(2) or '—'}"
    return url


def nice_plan_label(url):
    track = load_track(url)
    if track and track.get("plan_number"):
        return f"תוכנית {track['plan_number']}"
    return plan_label(url)


def state_file(url):
    return TRACKS_DIR / f"{url_id(url)}.json"


def history_file(url):
    return TRACKS_DIR / f"{url_id(url)}.history.jsonl"


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


def capture(url):
    rows = {section: [] for section in SECTIONS}
    plan_number = ""
    plan_title = ""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_context(
            locale="he-IL", viewport={"width": 1500, "height": 1100},
        ).new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(6_000)
        try:
            info = page.evaluate(EXTRACT_PLAN_INFO_JS)
            plan_number = info.get("number", "") or ""
            plan_title = info.get("title", "") or ""
        except Exception:
            pass
        for label in SECTIONS:
            try:
                section = open_section(page, label)
                panel = section.locator(".uk-accordion-content").first
                expand_nested_accordions(panel, page)
                page.wait_for_timeout(800)
                raw_rows = panel.evaluate(EXTRACT_ROWS_JS)
                rows[label] = [
                    Row(section=label, **{k: r[k] for k in ("category", "name", "scope", "edit_date")})
                    for r in raw_rows if r["name"]
                ]
            except (PWTimeout, Exception):
                rows[label] = []
        browser.close()
    return {"rows": rows, "plan_number": plan_number, "plan_title": plan_title}


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
    if USE_DB:
        import psycopg
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
    path = state_file(url)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return None


def save_track(url, track):
    if USE_DB:
        import psycopg
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
        return
    TRACKS_DIR.mkdir(parents=True, exist_ok=True)
    state_file(url).write_text(json.dumps(track, indent=2, ensure_ascii=False), encoding="utf-8")


def list_tracks():
    if USE_DB:
        import psycopg
        with psycopg.connect(DATABASE_URL) as conn:
            rows = conn.execute(
                "SELECT url, added_at, last_check, plan_number, plan_title, rows_json FROM tracks ORDER BY added_at"
            ).fetchall()
        return [
            {"url": r[0], "added_at": r[1], "last_check": r[2],
             "plan_number": r[3], "plan_title": r[4], "rows": r[5] or {}}
            for r in rows
        ]
    if not TRACKS_DIR.exists():
        return []
    items = []
    for path in sorted(TRACKS_DIR.glob("*.json")):
        try:
            items.append(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            pass
    return items


def remove_track(url):
    if USE_DB:
        import psycopg
        with psycopg.connect(DATABASE_URL) as conn:
            conn.execute("DELETE FROM history WHERE url = %s", (url,))
            cur = conn.execute("DELETE FROM tracks WHERE url = %s", (url,))
            removed = cur.rowcount > 0
            conn.commit()
        return removed
    path = state_file(url)
    if not path.exists():
        return False
    path.unlink()
    history_path = history_file(url)
    if history_path.exists():
        history_path.unlink()
    return True


def append_history(url, timestamp, changes):
    if USE_DB:
        import psycopg
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
        return
    TRACKS_DIR.mkdir(parents=True, exist_ok=True)
    with history_file(url).open("a", encoding="utf-8") as f:
        for change in changes:
            f.write(json.dumps({"ts": timestamp, **asdict(change)}, ensure_ascii=False) + "\n")


def load_history(url, limit=20):
    if USE_DB:
        import psycopg
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
    path = history_file(url)
    if not path.exists():
        return []
    lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    return [json.loads(l) for l in lines[-limit:][::-1]]


def history_count(url):
    if USE_DB:
        import psycopg
        with psycopg.connect(DATABASE_URL) as conn:
            n = conn.execute("SELECT COUNT(*) FROM history WHERE url = %s", (url,)).fetchone()[0]
        return n
    path = history_file(url)
    if not path.exists():
        return 0
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())


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
                target = out_dir / f"{stamp}_{suggested}"
                download.save_as(target)
                size = target.stat().st_size
                entry["path"] = str(target)
                entry["filename"] = suggested
                entry["size"] = size
                print(f"[download]   ✓ saved {suggested} ({size} bytes) -> {target}", flush=True)
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
    <div style="font-size:12px;color:#6b7280;direction:rtl;text-align:right">{nice_plan_label(url)}</div>
    <div style="font-size:20px;font-weight:700;margin:4px 0 18px;direction:rtl;text-align:right">זוהו {len(changes)} שינויים</div>
    {''.join(cards)}
    <a href="{url}" style="display:inline-block;margin-top:14px;padding:10px 16px;background:#2563eb;color:#fff;text-decoration:none;font-size:14px;font-weight:500;border-radius:6px;direction:rtl">פתח את עמוד התכנית</a>
  </div>
</body>
</html>"""


def build_email_plain(changes, url):
    lines = [f"זוהו {len(changes)} שינויים בתכנית {nice_plan_label(url)}", ""]
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
    if len(changes) == 1:
        c = changes[0]
        return f"[מבאת] {ACTION_LABEL_HE[c.action]}: {c.name[:60]}"
    return f"[מבאת] {len(changes)} שינויים · {nice_plan_label(url)}"


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
        path = f.get("path")
        if not path:
            print(f"[email]   skip (no path): {f.get('name')} — {f.get('error', 'unknown')}", flush=True)
            continue
        size = f.get("size", 0)
        if size > MAX_ATTACH_BYTES:
            print(f"[email]   skip (too large, {size} bytes > {MAX_ATTACH_BYTES}): {f['filename']}", flush=True)
            continue
        mime, _ = mimetypes.guess_type(f["filename"])
        maintype, subtype = (mime.split("/", 1) if mime else ("application", "octet-stream"))
        with open(path, "rb") as fh:
            message.add_attachment(fh.read(), maintype=maintype, subtype=subtype, filename=f["filename"])
        attached += 1
        print(f"[email]   attached {f['filename']} ({size} bytes, {maintype}/{subtype})", flush=True)
    print(f"[email] {attached} attachment(s) added to message", flush=True)

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as smtp:
            smtp.login(user, password)
            smtp.send_message(message)
        attached = sum(1 for f in files if f.get("path") and f.get("size", 0) <= MAX_ATTACH_BYTES)
        suffix = f" ({attached} קבצים מצורפים)" if attached else ""
        return f"המייל נשלח אל {recipient}{suffix}"
    except Exception as exc:
        return f"שליחת המייל נכשלה: {type(exc).__name__}: {exc}"


def add_track(url):
    if load_track(url):
        return {"status": "exists", "url": url}
    timestamp = datetime.now().isoformat(timespec="seconds")
    snapshot = capture(url)
    rows = snapshot["rows"]
    save_track(url, {
        "url": url,
        "added_at": timestamp,
        "last_check": timestamp,
        "plan_number": snapshot["plan_number"],
        "plan_title": snapshot["plan_title"],
        "rows": rows_to_json(rows),
    })
    return {"status": "added", "url": url, "total_rows": sum(len(v) for v in rows.values())}


def check_track(url, send_emails=True):
    track = load_track(url)
    if not track:
        return {"status": "not_tracked", "url": url}
    timestamp = datetime.now().isoformat(timespec="seconds")
    snapshot = capture(url)
    current_rows = snapshot["rows"]
    previous_rows = rows_from_json(track.get("rows", {}))
    changes = diff_all(previous_rows, current_rows)

    track["last_check"] = timestamp
    track["plan_number"] = snapshot["plan_number"] or track.get("plan_number", "")
    track["plan_title"] = snapshot["plan_title"] or track.get("plan_title", "")
    track["rows"] = rows_to_json(current_rows)
    save_track(url, track)

    email_status = ""
    files = []
    if changes:
        append_history(url, timestamp, changes)
        if send_emails:
            files = download_changed_files(url, changes)
            email_status = send_email(changes, url, files=files)

    return {
        "status": "checked",
        "url": url,
        "total_rows": sum(len(rows) for rows in current_rows.values()),
        "changes": changes,
        "email_status": email_status,
        "files": files,
    }


def simulate_track(url, fake_date="01/01/1900"):
    track = load_track(url)
    if not track:
        return False
    rows = rows_from_json(track.get("rows", {}))
    for section in SECTIONS:
        section_rows = rows.get(section, [])
        if section_rows:
            section_rows[0] = Row(**{**asdict(section_rows[0]), "edit_date": fake_date})
            track["rows"] = rows_to_json(rows)
            save_track(url, track)
            return True
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
            print(f"- {nice_plan_label(track['url'])}  ({track['url']})")
            print(f"    added {track['added_at']}, last check {track['last_check']}")
        return 0

    if args.cmd == "add":
        result = add_track(args.url)
        if result["status"] == "exists":
            print(f"already tracking {args.url}")
        else:
            print(f"added {nice_plan_label(args.url)} with {result['total_rows']} docs as baseline")
        return 0

    if args.cmd == "remove":
        if remove_track(args.url):
            print(f"removed {nice_plan_label(args.url)}")
            return 0
        print(f"not tracked: {args.url}")
        return 1

    if args.cmd == "simulate":
        if simulate_track(args.url):
            print(f"tampered baseline for {nice_plan_label(args.url)}; next check will report changes")
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
    label = nice_plan_label(result["url"])
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
