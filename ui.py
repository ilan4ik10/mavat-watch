#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["playwright>=1.49", "flask>=3.0"]
# ///
from __future__ import annotations

import threading
import time
from datetime import datetime

from flask import Flask, render_template_string, request

from mavat_watch import (
    add_track,
    check_track,
    history_count,
    list_tracks,
    load_history,
    load_track,
    plan_label,
    remove_track,
    simulate_track,
    url_id,
)

CHECK_INTERVAL_SECONDS = 60

app = Flask(__name__)
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


def humanize(iso_ts):
    then = datetime.fromisoformat(iso_ts)
    delta = datetime.now() - then
    seconds = delta.total_seconds()
    if seconds < 60:
        return "ממש עכשיו"
    if seconds < 3600:
        return f"לפני {int(seconds // 60)} דק'"
    if seconds < 86400:
        return f"לפני {int(seconds // 3600)} שעות"
    return f"לפני {int(seconds // 86400)} ימים"


def nice_ts(iso_ts):
    try:
        return datetime.fromisoformat(iso_ts).strftime("%d/%m/%Y %H:%M")
    except Exception:
        return iso_ts


ACTION_LABEL_HE = {"NEW": "חדש", "UPDATED": "עודכן", "REMOVED": "הוסר"}

app.jinja_env.filters["humanize"] = humanize
app.jinja_env.filters["nice_ts"] = nice_ts
app.jinja_env.filters["action_he"] = lambda a: ACTION_LABEL_HE.get(a, a)


PAGE = """<!doctype html>
<html lang="he" dir="rtl">
<head>
<meta charset="utf-8">
<title>מעקב מבאת</title>
<style>
  :root {
    --bg: #f7f8fa; --card: #ffffff; --text: #111827; --muted: #6b7280;
    --border: #e5e7eb; --accent: #2563eb; --accent-hover: #1d4ed8;
    --danger: #dc2626; --good: #16a34a; --warn: #ea580c;
  }
  * { box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         background: var(--bg); color: var(--text); margin: 0; padding: 2.5em 1em;
         min-height: 100vh; }
  main { max-width: 760px; margin: 0 auto; }
  h1 { margin: 0 0 0.2em; font-size: 28px; font-weight: 700; }
  .subtitle { color: var(--muted); margin: 0 0 2em; }

  .add-card { background: var(--card); border: 1px solid var(--border);
              border-radius: 10px; padding: 1.25em; margin-bottom: 2em; }
  .add-card form { display: flex; gap: 0.5em; }
  .add-card input { flex: 1; padding: 0.7em 0.9em; font-size: 14px;
                    border: 1px solid var(--border); border-radius: 7px; outline: none; }
  .add-card input:focus { border-color: var(--accent); }
  .add-card .hint { font-size: 13px; color: var(--muted); margin: 0.6em 0 0; }

  button { padding: 0.7em 1.1em; font-size: 14px; font-weight: 500;
           border: 0; border-radius: 7px; cursor: pointer; }
  button.primary { background: var(--accent); color: white; }
  button.primary:hover { background: var(--accent-hover); }
  button.ghost { background: transparent; color: var(--muted); border: 1px solid var(--border); }
  button.ghost:hover { background: #f3f4f6; }
  button.danger { background: transparent; color: var(--danger); border: 1px solid var(--border); }
  button.danger:hover { background: #fef2f2; border-color: var(--danger); }

  .section-title { font-size: 12px; font-weight: 600; text-transform: uppercase;
                   letter-spacing: 0.8px; color: var(--muted);
                   margin: 0 0 0.8em; }

  .track { background: var(--card); border: 1px solid var(--border);
           border-radius: 10px; padding: 1.25em; margin-bottom: 0.8em; }
  .track.highlight { border-color: var(--accent); box-shadow: 0 0 0 3px rgba(37,99,235,0.1); }
  .track h2 { margin: 0 0 0.2em; font-size: 17px; font-weight: 600; }
  .track .plan-title { color: #374151; font-size: 13px; margin: 0 0 0.4em;
                       line-height: 1.4; }
  .track .url { color: var(--muted); font-size: 13px; word-break: break-all;
                text-decoration: none; }
  .track .url:hover { color: var(--accent); }
  .meta { display: flex; flex-wrap: wrap; gap: 1.2em; margin: 0.8em 0;
          font-size: 13px; color: var(--muted); }
  .meta strong { color: var(--text); font-weight: 500; }
  .actions { display: flex; gap: 0.4em; flex-wrap: wrap; }
  .actions form { margin: 0; }

  .empty { text-align: center; color: var(--muted); padding: 3em 1em;
           background: var(--card); border: 1px dashed var(--border);
           border-radius: 10px; }

  .result { margin-top: 1em; padding: 0.9em 1em; border-radius: 7px; font-size: 14px; }
  .result.info { background: #eff6ff; color: #1e3a8a; }
  .result.ok { background: #f0fdf4; color: #14532d; }
  .result.warn { background: #fff7ed; color: #7c2d12; }

  .changes { margin-top: 0.7em; }
  .change { padding: 0.6em 0.8em; margin: 0.3em 0; border-radius: 6px;
            font-size: 13px; border-left: 3px solid var(--border); background: #fafafa; }
  .change.NEW { border-color: var(--good); background: #f0fdf4; }
  .change.UPDATED { border-color: var(--warn); background: #fff7ed; }
  .change.REMOVED { border-color: var(--danger); background: #fef2f2; }
  .change .tag { font-weight: 700; font-size: 11px; letter-spacing: 0.5px; }
  .change .name { font-weight: 600; }
  .change .diff { color: var(--muted); font-size: 12px; }

  details { margin-top: 0.8em; font-size: 13px; }
  details summary { color: var(--muted); cursor: pointer; }
  details summary:hover { color: var(--accent); }
  details ul { list-style: none; padding: 0; margin: 0.6em 0 0; }
  details li { padding: 0.4em 0; border-bottom: 1px solid var(--border);
               color: var(--muted); }
  details li:last-child { border: 0; }
  details li b { color: var(--text); }

  #spinner { display: none; padding: 1em 0; text-align: center; color: var(--muted);
             font-size: 15px; margin-bottom: 0.5em; }
  #spinner-text::after { content: ''; display: inline-block; min-width: 1.5em;
                         text-align: start; animation: dots 1.4s steps(1, end) infinite; }
  @keyframes dots {
    0%   { content: ''; }
    25%  { content: '.'; }
    50%  { content: '..'; }
    75%  { content: '...'; }
  }
</style>
</head>
<body>
<main>
  <h1>מעקב תכניות מבאת</h1>
  <p class="subtitle">קבלו התראה במייל כשמסמך חדש מתווסף, מוסר, או מתעדכן בתכניות שאתם עוקבים אחריהן. בדיקה אוטומטית כל דקה.</p>

  <div id="spinner"><span id="spinner-text">טוען</span></div>
  <script>
    async function submitForm(form, spinnerText, confirmText) {
      if (confirmText && !confirm(confirmText)) return false;
      document.getElementById('spinner-text').textContent = spinnerText;
      document.getElementById('spinner').style.display = 'block';
      try {
        await fetch(form.action, { method: 'POST', body: new FormData(form) });
      } catch (e) {
        alert('שגיאה: ' + e.message);
      }
      location.reload();
      return false;
    }
  </script>

  <div class="add-card">
    <p class="section-title">הוספת תכנית למעקב</p>
    <form method="post" action="/add" onsubmit="submitForm(this, 'מוסיף את התוכנית שלך למעקב ברגעים אלה'); return false">
      <button class="primary" type="submit">+ הוסף</button>
      <input type="url" name="url" required dir="ltr"
             placeholder="https://mavat.iplan.gov.il/SV4/1/3005115162/310"
             pattern="https://mavat\\.iplan\\.gov\\.il/.*">
    </form>
    <p class="hint">המצב הנוכחי של התכנית נשמר כבסיס. כל שינוי מהרגע הזה ואילך יישלח אליכם במייל.</p>
  </div>

  <p class="section-title">תכניות במעקב ({{ tracks|length }})</p>

  {% if not tracks %}
    <div class="empty">אין עדיין תכניות במעקב. הוסיפו אחת למעלה.</div>
  {% endif %}

  {% for t in tracks %}
    <div class="track">
      <h2>{{ t.label }}</h2>
      {% if t.title %}<div class="plan-title">{{ t.title }}</div>{% endif %}
      <a class="url" href="{{ t.url }}" target="_blank" dir="ltr">{{ t.url }}</a>

      <div class="meta">
        <span>נוספה <strong>{{ t.added_at | humanize }}</strong></span>
        <span>בדיקה אחרונה <strong>{{ t.last_check | humanize }}</strong></span>
        <span><strong>{{ t.total_rows }}</strong> מסמכים</span>
        <span><strong>{{ t.history_count }}</strong> שינויים תועדו</span>
      </div>

      <div class="actions">
        <form method="post" action="/check/{{ t.id }}"
              onsubmit="submitForm(this, 'בודק את התכנית ברגעים אלה'); return false">
          <button class="primary" type="submit">בדוק עכשיו</button>
        </form>
        <form method="post" action="/simulate/{{ t.id }}"
              onsubmit="submitForm(this, 'מבצע סימולציה'); return false">
          <button class="ghost" type="submit" title="שינוי הבסיס באופן מלאכותי כדי שהבדיקה הבאה תדווח על שינוי לצורך הדגמה">סימולציה</button>
        </form>
        <form method="post" action="/remove/{{ t.id }}"
              onsubmit="submitForm(this, 'מסיר את התכנית מהמעקב', 'להפסיק את המעקב אחר {{ t.label }}?'); return false">
          <button class="danger" type="submit">הסר</button>
        </form>
      </div>

      {% if t.history %}
        <details>
          <summary>יומן שינויים אחרונים ({{ t.history|length }})</summary>
          <ul>
            {% for h in t.history %}
              <li><b>{{ h.action | action_he }}</b> · {{ h.name }} <span style="float:left">{{ h.ts | nice_ts }}</span></li>
            {% endfor %}
          </ul>
        </details>
      {% endif %}
    </div>
  {% endfor %}
</main>
</body></html>
"""


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


def render():
    return render_template_string(PAGE, tracks=build_tracks())


@app.get("/")
def index():
    return render()


@app.post("/add")
def add():
    url = request.form["url"].strip()
    with check_lock:
        add_track(url)
    return ("", 204)


@app.post("/check/<track_id>")
def check(track_id):
    track = find_track_by_id(track_id)
    if track:
        with check_lock:
            check_track(track["url"])
    return ("", 204)


@app.post("/simulate/<track_id>")
def simulate(track_id):
    track = find_track_by_id(track_id)
    if track:
        simulate_track(track["url"])
    return ("", 204)


@app.post("/remove/<track_id>")
def remove(track_id):
    track = find_track_by_id(track_id)
    if track:
        remove_track(track["url"])
    return ("", 204)


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5050))
    host = "0.0.0.0" if "PORT" in os.environ else "127.0.0.1"
    app.run(host=host, port=port, debug=False)
