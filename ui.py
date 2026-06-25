#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["playwright>=1.49", "flask>=3.0", "psycopg[binary]>=3.1", "pymupdf>=1.27"]
# ///
from __future__ import annotations

import threading
import time
from pathlib import Path

import psycopg
from flask import Flask, jsonify, request, send_from_directory

from mavat_watch import (
    DATABASE_URL,
    add_track,
    check_track,
    list_tracks,
    remove_track,
    simulate_pdf_change,
    simulate_track,
    url_id,
)

CHECK_INTERVAL_SECONDS = 60
FRONTEND_DIST = Path(__file__).parent / "frontend" / "dist"

app = Flask(__name__, static_folder=None)

# Per-plan locks: serialize operations on the SAME plan so the background
# checker and an API check/add never double-process one plan (which would cause
# duplicate history rows and duplicate emails), while letting different plans
# and API calls run without blocking on each other. The single browser worker
# already serializes the actual scraping, so this lock only guards each plan's
# load -> diff -> save -> notify read-modify-write.
_plan_locks: dict[str, threading.Lock] = {}
_plan_locks_guard = threading.Lock()


def _plan_lock(url: str) -> threading.Lock:
    with _plan_locks_guard:
        lock = _plan_locks.get(url)
        if lock is None:
            lock = threading.Lock()
            _plan_locks[url] = lock
        return lock


def background_checker():
    while True:
        time.sleep(CHECK_INTERVAL_SECONDS)
        for track in list_tracks():
            try:
                with _plan_lock(track["url"]):
                    check_track(track["url"], send_emails=True)
            except Exception as exc:  # noqa: BLE001
                print(f"[background] {track['url']}: {type(exc).__name__}: {exc}", flush=True)


threading.Thread(target=background_checker, daemon=True).start()


def build_tracks():
    with psycopg.connect(DATABASE_URL) as conn:
        tracks = conn.execute(
            "SELECT url, added_at, last_check, plan_number, plan_title, rows_json"
            " FROM tracks ORDER BY added_at"
        ).fetchall()
        if not tracks:
            return []
        urls = [t[0] for t in tracks]
        history_rows = conn.execute(
            "SELECT url, ts, section, action, name, category, scope, edit_date,"
            " prev_scope, prev_edit_date FROM history WHERE url = ANY(%s) ORDER BY id DESC",
            (urls,),
        ).fetchall()
        counts = dict(conn.execute(
            "SELECT url, COUNT(*) FROM history WHERE url = ANY(%s) GROUP BY url",
            (urls,),
        ).fetchall())

    history_by_url: dict[str, list] = {}
    for h in history_rows:
        bucket = history_by_url.setdefault(h[0], [])
        if len(bucket) < 10:
            bucket.append({
                "ts": h[1], "section": h[2], "action": h[3], "name": h[4],
                "category": h[5], "scope": h[6], "edit_date": h[7],
                "prev_scope": h[8], "prev_edit_date": h[9],
            })

    return [
        {
            "id": url_id(url),
            "url": url,
            "label": f"תוכנית {plan_number}" if plan_number else "תוכנית (טוען…)",
            "title": plan_title or "",
            "added_at": added_at,
            "last_check": last_check,
            "total_rows": sum(len(rows) for rows in (rows_json or {}).values()),
            "history": history_by_url.get(url, []),
            "history_count": counts.get(url, 0),
        }
        for url, added_at, last_check, plan_number, plan_title, rows_json in tracks
    ]


def find_track_by_id(track_id):
    for track in list_tracks():
        if url_id(track["url"]) == track_id:
            return track
    return None


@app.get("/api/tracks")
def api_list_tracks():
    return jsonify(build_tracks())


@app.post("/api/tracks")
def api_add_track():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "url required"}), 400
    with _plan_lock(url):
        add_track(url)
    return jsonify(build_tracks()), 201


@app.post("/api/tracks/<track_id>/check")
def api_check_track(track_id):
    track = find_track_by_id(track_id)
    if not track:
        return jsonify({"error": "not found"}), 404
    with _plan_lock(track["url"]):
        check_track(track["url"])
    return jsonify(build_tracks())


@app.post("/api/tracks/<track_id>/simulate")
def api_simulate_track(track_id):
    track = find_track_by_id(track_id)
    if not track:
        return jsonify({"error": "not found"}), 404
    simulate_track(track["url"])
    return jsonify(build_tracks())


@app.post("/api/tracks/<track_id>/simulate-pdf")
def api_simulate_pdf(track_id):
    track = find_track_by_id(track_id)
    if not track:
        return jsonify({"error": "not found"}), 404
    if not simulate_pdf_change(track["url"]):
        return jsonify({
            "error": "אין קובץ PDF שמור — הריצו 'בדוק עכשיו' לאחר סימולציה רגילה כדי להוריד קובץ תחילה.",
        }), 409
    return jsonify(build_tracks())


@app.delete("/api/tracks/<track_id>")
def api_remove_track(track_id):
    track = find_track_by_id(track_id)
    if not track:
        return jsonify({"error": "not found"}), 404
    remove_track(track["url"])
    return jsonify(build_tracks())


@app.get("/")
@app.get("/<path:path>")
def serve_spa(path: str = "index.html"):
    requested = FRONTEND_DIST / path
    if requested.is_file():
        return send_from_directory(FRONTEND_DIST, path)
    index = FRONTEND_DIST / "index.html"
    if index.is_file():
        return send_from_directory(FRONTEND_DIST, "index.html")
    return (
        "Frontend not built. Run: cd frontend && npm install && npm run build",
        503,
    )


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5050))
    host = "0.0.0.0" if "PORT" in os.environ else "127.0.0.1"
    app.run(host=host, port=port, debug=False)
