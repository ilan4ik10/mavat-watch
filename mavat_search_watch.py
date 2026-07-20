#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["playwright>=1.49"]
# ///
"""Independent flow: track גוש/חלקה advanced-search results on Mavat and email
when a new plan number appears. Deliberately separate from mavat_watch.py
(own tables, own background loop, own email) — the two flows share only the
Postgres connection, Gmail SMTP mechanics, and the Chromium browser lock; a
failure in one flow's logic cannot affect the other.
"""
from __future__ import annotations

import argparse
import json
import os
import smtplib
import time
from datetime import datetime, timezone
from email.message import EmailMessage

import psycopg
from playwright.sync_api import sync_playwright

# Shared with mavat_watch.py: Render's instance can only afford one Chromium
# at a time. Two independent per-flow locks let both flows' browsers run
# concurrently, which starved each other for CPU/memory badly enough that
# this flow's page loads timed out (spinner never cleared within 15s).
# Importing the same lock object serializes Chromium use server-wide while
# leaving every other part of each flow (tables, checkers, email) untouched.
from mavat_watch import _browser_lock

SEARCH_URL = "https://mavat.iplan.gov.il/SV3?searchEntity=0&searchType=0&entityType=0&searchMethod=2"
DATABASE_URL = os.environ["DATABASE_URL"]

SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS tracked_searches (
    id              BIGSERIAL PRIMARY KEY,
    gush            TEXT NOT NULL,
    parcel          TEXT NOT NULL DEFAULT '',
    label           TEXT NOT NULL DEFAULT '',
    added_at        TEXT NOT NULL,
    last_check      TEXT NOT NULL,
    known_plan_ids  JSONB NOT NULL DEFAULT '[]'::jsonb,
    plan_count      INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS search_history (
    id              BIGSERIAL PRIMARY KEY,
    search_id       BIGINT NOT NULL,
    ts              TEXT NOT NULL,
    plan_id         TEXT NOT NULL,
    plan_number     TEXT NOT NULL DEFAULT '',
    plan_name       TEXT NOT NULL DEFAULT '',
    auth_name       TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_search_history_search_id ON search_history (search_id, id DESC);
"""

with psycopg.connect(DATABASE_URL) as _conn:
    _conn.execute(SCHEMA_DDL)
    _conn.commit()


def _search_response(page):
    return page.expect_response(
        lambda r: r.url.endswith("/rest/api/sv3/Search"), timeout=30_000
    )


def _extract_plans(page, gush, parcel):
    # The search UI shows results 20 at a time behind a "הצג עוד" (show more)
    # button rather than a single page; a search matching >20 plans silently
    # truncates to the first 20 unless we click through every page. Missing
    # that caused both an undercount and false NEW-plan detections (plans
    # shifting across the page-20 boundary as sort order changed looked like
    # new arrivals). Collect every page's response before reading results.
    #
    # Each click is paired with expect_response instead of a blind sleep:
    # under load the response can take longer than a fixed wait, and a blind
    # sleep that fires too early silently drops that page's rows with no
    # error — which is exactly what caused some searches to keep landing on
    # an incomplete count. Waiting on the actual response removes that race.
    responses = []

    page.goto(SEARCH_URL, wait_until="load", timeout=60_000)
    page.wait_for_timeout(1_500)

    page.click('button:has-text("תכניות (0)")', timeout=15_000)
    page.wait_for_timeout(800)

    block_input = page.get_by_label("גוש", exact=True).locator("visible=true")
    block_input.wait_for(state="visible", timeout=15_000)
    block_input.fill(str(gush))
    page.wait_for_timeout(300)

    if parcel:
        parcel_input = page.get_by_label("חלקה", exact=True).locator("visible=true")
        parcel_input.wait_for(state="visible", timeout=5_000)
        parcel_input.fill(str(parcel))
        page.wait_for_timeout(300)

    search_btn = page.locator('button[aria-label="חיפוש"]').locator("visible=true")
    with _search_response(page) as resp_info:
        search_btn.first.click(timeout=15_000)
    try:
        responses.append(resp_info.value.json())
    except Exception:
        pass
    page.wait_for_timeout(500)

    for _ in range(50):  # hard cap so a stuck button can't loop forever
        show_more = page.locator('button:has-text("הצג עוד"), a:has-text("הצג עוד")').locator("visible=true")
        if show_more.count() == 0:
            break
        try:
            show_more.first.scroll_into_view_if_needed(timeout=5_000)
            with _search_response(page) as resp_info:
                show_more.first.click(timeout=5_000)
            responses.append(resp_info.value.json())
        except Exception:
            break
        page.wait_for_timeout(500)

    plans = []
    seen_ids = set()
    for entry in responses:
        result_list = entry if isinstance(entry, list) else [entry]
        for item in result_list:
            result = (item or {}).get("result") or {}
            if result.get("searchEntity") != 1:
                continue
            for row in result.get("dtResults") or []:
                plan_id = row.get("PLAN_ID")
                if plan_id is None:
                    continue
                plan_id = str(int(plan_id))
                if plan_id in seen_ids:
                    continue
                seen_ids.add(plan_id)
                plans.append({
                    "plan_id": plan_id,
                    "plan_number": row.get("ENTITY_NUMBER") or "",
                    "plan_name": row.get("ENTITY_NAME") or "",
                    "auth_name": row.get("AUTH_NAME") or "",
                    "status": row.get("INTERNET_SHORT_STATUS") or row.get("UNIFIED_STATUS_DESC") or "",
                })
    return plans


def search_plans(gush, parcel=""):
    with _browser_lock:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                ctx = browser.new_context(locale="he-IL", viewport={"width": 1400, "height": 1000})
                page = ctx.new_page()
                return _extract_plans(page, gush, parcel)
            finally:
                try:
                    browser.close()
                except Exception:
                    pass


def load_search_track(search_id):
    with psycopg.connect(DATABASE_URL) as conn:
        row = conn.execute(
            "SELECT id, gush, parcel, label, added_at, last_check, known_plan_ids, plan_count"
            " FROM tracked_searches WHERE id = %s",
            (search_id,),
        ).fetchone()
    if not row:
        return None
    return {
        "id": row[0], "gush": row[1], "parcel": row[2], "label": row[3],
        "added_at": row[4], "last_check": row[5],
        "known_plan_ids": row[6] or [], "plan_count": row[7],
    }


def list_search_tracks():
    with psycopg.connect(DATABASE_URL) as conn:
        rows = conn.execute(
            "SELECT id, gush, parcel, label, added_at, last_check, known_plan_ids, plan_count"
            " FROM tracked_searches ORDER BY added_at"
        ).fetchall()
    return [
        {
            "id": r[0], "gush": r[1], "parcel": r[2], "label": r[3],
            "added_at": r[4], "last_check": r[5],
            "known_plan_ids": r[6] or [], "plan_count": r[7],
        }
        for r in rows
    ]


def add_search_track(gush, parcel="", label=""):
    gush = str(gush).strip()
    parcel = str(parcel or "").strip()
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    plans = search_plans(gush, parcel)
    plan_ids = [p["plan_id"] for p in plans]
    with psycopg.connect(DATABASE_URL) as conn:
        row = conn.execute(
            """
            INSERT INTO tracked_searches (gush, parcel, label, added_at, last_check, known_plan_ids, plan_count)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s)
            RETURNING id
            """,
            (gush, parcel, label, timestamp, timestamp, json.dumps(plan_ids), len(plan_ids)),
        ).fetchone()
        conn.commit()
    return {"id": row[0], "gush": gush, "parcel": parcel, "label": label, "plan_count": len(plan_ids)}


def rebase_search_track(search_id):
    """Reset a tracked search's known-plans baseline to whatever the search
    currently returns, with no history rows and no email. For adopting a
    capture-logic fix (e.g. the pagination fix) without every plan the old
    capture had missed firing a false 'new plan' alert."""
    track = load_search_track(search_id)
    if not track:
        return None
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    plans = search_plans(track["gush"], track["parcel"])
    plan_ids = [p["plan_id"] for p in plans]
    with psycopg.connect(DATABASE_URL) as conn:
        conn.execute(
            "UPDATE tracked_searches SET last_check = %s, known_plan_ids = %s::jsonb, plan_count = %s WHERE id = %s",
            (timestamp, json.dumps(plan_ids), len(plan_ids), search_id),
        )
        conn.commit()
    return {"id": search_id, "plan_count": len(plan_ids)}


def remove_search_track(search_id):
    with psycopg.connect(DATABASE_URL) as conn:
        conn.execute("DELETE FROM search_history WHERE search_id = %s", (search_id,))
        cur = conn.execute("DELETE FROM tracked_searches WHERE id = %s", (search_id,))
        removed = cur.rowcount > 0
        conn.commit()
    return removed


def load_search_history(search_id, limit=20):
    with psycopg.connect(DATABASE_URL) as conn:
        rows = conn.execute(
            """
            SELECT ts, plan_id, plan_number, plan_name, auth_name, status
            FROM search_history WHERE search_id = %s ORDER BY id DESC LIMIT %s
            """,
            (search_id, limit),
        ).fetchall()
    return [
        {"ts": r[0], "plan_id": r[1], "plan_number": r[2], "plan_name": r[3],
         "auth_name": r[4], "status": r[5]}
        for r in rows
    ]


def track_label(track):
    if track.get("label"):
        return track["label"]
    if track.get("parcel"):
        return f"גוש {track['gush']} חלקה {track['parcel']}"
    return f"גוש {track['gush']}"


def build_email_subject(track, new_plans):
    label = track_label(track)
    if len(new_plans) == 1:
        return f"[מבאת] {label} · תוכנית חדשה: {new_plans[0]['plan_number']}"
    return f"[מבאת] {label} · {len(new_plans)} תוכניות חדשות"


def build_email_plain(track, new_plans):
    lines = [f"נמצאו {len(new_plans)} תוכניות חדשות עבור {track_label(track)}", ""]
    for p in new_plans:
        lines.append(f"• {p['plan_number']} — {p['plan_name']}")
        if p.get("auth_name"):
            lines.append(f"    סמכות: {p['auth_name']}")
        if p.get("status"):
            lines.append(f"    סטטוס: {p['status']}")
    lines.append("")
    lines.append(SEARCH_URL)
    return "\n".join(lines)


def build_email_html(track, new_plans):
    cards = []
    for p in new_plans:
        cards.append(f"""
<div dir="rtl" style="background:#f0fdf4;border-right:3px solid #16a34a;padding:14px 16px;margin-bottom:10px;border-radius:6px;direction:rtl;text-align:right">
  <div style="display:inline-block;background:#16a34a;color:#fff;font-size:10px;font-weight:700;padding:3px 10px;border-radius:10px;letter-spacing:0.5px">חדש</div>
  <div style="font-weight:600;color:#111827;font-size:15px;margin-top:8px;direction:rtl;text-align:right">{p['plan_number']} — {p['plan_name']}</div>
  <div style="font-size:12px;color:#6b7280;margin-top:2px;direction:rtl;text-align:right">{p.get('auth_name', '')}</div>
  <div style="font-size:13px;color:#374151;margin-top:8px;direction:rtl;text-align:right">{p.get('status', '')}</div>
</div>""")
    return f"""<!doctype html>
<html lang="he" dir="rtl">
<body style="margin:0;padding:24px;background:#f7f8fa;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#111827;direction:rtl;text-align:right">
  <div dir="rtl" style="max-width:560px;margin:0 auto;background:#fff;border-radius:12px;padding:24px;border:1px solid #e5e7eb;direction:rtl;text-align:right;unicode-bidi:isolate">
    <div style="font-size:22px;font-weight:700;color:#111827;direction:rtl;text-align:right">{track_label(track)}</div>
    <div style="font-size:14px;color:#6b7280;margin:4px 0 18px;direction:rtl;text-align:right">נמצאו {len(new_plans)} תוכניות חדשות</div>
    {''.join(cards)}
    <a href="{SEARCH_URL}" style="display:inline-block;margin-top:14px;padding:10px 16px;background:#2563eb;color:#fff;text-decoration:none;font-size:14px;font-weight:500;border-radius:6px;direction:rtl">פתח את חיפוש התכניות</a>
  </div>
</body>
</html>"""


def send_email(track, new_plans):
    user = os.environ.get("MAVAT_GMAIL_USER")
    password = os.environ.get("MAVAT_GMAIL_PASS")
    recipient = os.environ.get("MAVAT_SEARCH_NOTIFY_TO") or os.environ.get("MAVAT_NOTIFY_TO", user)
    if not user or not password:
        return "מייל לא נשלח: לא הוגדרו MAVAT_GMAIL_USER / MAVAT_GMAIL_PASS"

    message = EmailMessage()
    message["Subject"] = build_email_subject(track, new_plans)
    message["From"] = user
    message["To"] = recipient
    message.set_content(build_email_plain(track, new_plans))
    message.add_alternative(build_email_html(track, new_plans), subtype="html")

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as smtp:
            smtp.login(user, password)
            smtp.send_message(message)
        return f"המייל נשלח אל {recipient}"
    except Exception as exc:
        return f"שליחת המייל נכשלה: {type(exc).__name__}: {exc}"


def check_search_track(search_id, send_emails=True):
    track = load_search_track(search_id)
    if not track:
        return {"status": "not_tracked", "id": search_id}

    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    plans = search_plans(track["gush"], track["parcel"])
    known_ids = set(track.get("known_plan_ids") or [])
    new_plans = [p for p in plans if p["plan_id"] not in known_ids]
    all_ids = [p["plan_id"] for p in plans]

    email_status = ""
    if new_plans:
        print(f"[search-check] {track_label(track)}: {len(new_plans)} new plan(s)", flush=True)
        with psycopg.connect(DATABASE_URL) as conn:
            for p in new_plans:
                conn.execute(
                    """
                    INSERT INTO search_history (search_id, ts, plan_id, plan_number, plan_name, auth_name, status)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (search_id, timestamp, p["plan_id"], p["plan_number"], p["plan_name"],
                     p["auth_name"], p["status"]),
                )
            conn.commit()
        if send_emails:
            email_status = send_email(track, new_plans)
            print(f"[search-check] {email_status}", flush=True)

    with psycopg.connect(DATABASE_URL) as conn:
        conn.execute(
            "UPDATE tracked_searches SET last_check = %s, known_plan_ids = %s::jsonb, plan_count = %s WHERE id = %s",
            (timestamp, json.dumps(all_ids), len(all_ids), search_id),
        )
        conn.commit()

    return {
        "status": "checked", "id": search_id, "plan_count": len(all_ids),
        "new_plans": new_plans, "email_status": email_status,
    }


def check_all(send_emails=True):
    return [check_search_track(t["id"], send_emails=send_emails) for t in list_search_tracks()]


def main():
    parser = argparse.ArgumentParser(description="Watch Mavat block/parcel searches for new plans.")
    subparsers = parser.add_subparsers(dest="cmd")
    subparsers.add_parser("list")
    add_cmd = subparsers.add_parser("add")
    add_cmd.add_argument("gush")
    add_cmd.add_argument("--parcel", default="")
    add_cmd.add_argument("--label", default="")
    remove_cmd = subparsers.add_parser("remove"); remove_cmd.add_argument("id", type=int)
    check_cmd = subparsers.add_parser("check"); check_cmd.add_argument("id", type=int)
    check_cmd.add_argument("--no-email", action="store_true")
    check_all_cmd = subparsers.add_parser("check-all"); check_all_cmd.add_argument("--no-email", action="store_true")
    args = parser.parse_args()

    if not args.cmd:
        args.cmd = "check-all"
        args.no_email = False

    if args.cmd == "list":
        tracks = list_search_tracks()
        if not tracks:
            print("no tracked searches")
            return 0
        for t in tracks:
            print(f"- {track_label(t)}  ({t['plan_count']} plans, id={t['id']})")
        return 0

    if args.cmd == "add":
        result = add_search_track(args.gush, args.parcel, args.label)
        print(f"added {track_label(result)} with {result['plan_count']} plans as baseline (id={result['id']})")
        return 0

    if args.cmd == "remove":
        if remove_search_track(args.id):
            print(f"removed tracked search {args.id}")
            return 0
        print(f"not tracked: {args.id}")
        return 1

    if args.cmd == "check":
        result = check_search_track(args.id, send_emails=not args.no_email)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "check-all":
        for result in check_all(send_emails=not args.no_email):
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
