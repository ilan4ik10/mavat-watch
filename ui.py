#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["playwright>=1.49", "flask>=3.0", "psycopg[binary]>=3.1", "pymupdf>=1.27"]
# ///
from __future__ import annotations

import threading
import time
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

from mavat_watch import (
    add_track,
    check_track,
    history_count,
    list_tracks,
    load_history,
    plan_label,
    remove_track,
    simulate_pdf_change,
    simulate_track,
    url_id,
)

CHECK_INTERVAL_SECONDS = 60
FRONTEND_DIST = Path(__file__).parent / "frontend" / "dist"

app = Flask(__name__, static_folder=None)
check_lock = threading.Lock()


def background_checker():
    while True:
        time.sleep(CHECK_INTERVAL_SECONDS)
        with check_lock:
            for track in list_tracks():
                try:
                    check_track(track["url"], send_emails=True)
                except Exception as exc:  # noqa: BLE001
                    print(f"[background] {track['url']}: {type(exc).__name__}: {exc}", flush=True)


threading.Thread(target=background_checker, daemon=True).start()


def build_tracks():
    out = []
    for track in list_tracks():
        url = track["url"]
        out.append({
            "id": url_id(url),
            "url": url,
            "label": plan_label(url),
            "title": track.get("plan_title", ""),
            "added_at": track.get("added_at", track.get("last_check", "")),
            "last_check": track.get("last_check", ""),
            "total_rows": sum(len(rows) for rows in track.get("rows", {}).values()),
            "history": load_history(url, limit=10),
            "history_count": history_count(url),
        })
    return out


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
    with check_lock:
        add_track(url)
    return jsonify(build_tracks()), 201


@app.post("/api/tracks/<track_id>/check")
def api_check_track(track_id):
    track = find_track_by_id(track_id)
    if not track:
        return jsonify({"error": "not found"}), 404
    with check_lock:
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
