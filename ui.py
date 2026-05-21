#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["playwright>=1.49", "flask>=3.0"]
# ///
from __future__ import annotations

from dataclasses import asdict

from flask import Flask, render_template_string, request

from mavat_watch import DEFAULT_URL, SECTIONS, load_state, run_check, simulate

app = Flask(__name__)

PAGE = """<!doctype html>
<html lang="en" dir="ltr">
<head>
<meta charset="utf-8">
<title>Mavat Watch</title>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, sans-serif;
         max-width: 820px; margin: 2em auto; padding: 0 1em; color: #1f2937; }
  h1 { margin: 0 0 0.2em; font-size: 24px; }
  p.lead { color: #6b7280; margin-top: 0; }
  form { display: flex; gap: 0.5em; margin: 1.5em 0 0.5em; }
  input[type=text] { flex: 1; padding: 0.7em; font-size: 14px;
                     border: 1px solid #d1d5db; border-radius: 6px; }
  button { padding: 0.7em 1.2em; background: #2563eb; color: white;
           border: 0; border-radius: 6px; font-size: 14px; cursor: pointer; }
  button.secondary { background: #6b7280; }
  .row-actions { display: flex; gap: 0.5em; margin: 0.5em 0 1.5em; }
  .examples { font-size: 13px; color: #6b7280; margin: 0.5em 0 1.5em; }
  .examples code { background: #f3f4f6; padding: 2px 6px; border-radius: 3px;
                   cursor: pointer; }
  .status { padding: 0.8em 1em; background: #f3f4f6; border-radius: 6px;
            margin: 1em 0; font-size: 14px; }
  .status code { background: #e5e7eb; padding: 1px 4px; border-radius: 3px; }
  .summary { padding: 1em; background: #eff6ff; border-left: 4px solid #2563eb;
             border-radius: 6px; margin: 1em 0; font-weight: 500; }
  .change { padding: 0.8em 1em; margin: 0.5em 0; border-left: 4px solid #d1d5db;
            background: #fafafa; border-radius: 0 6px 6px 0; }
  .change.NEW { border-color: #16a34a; background: #f0fdf4; }
  .change.UPDATED { border-color: #ea580c; background: #fff7ed; }
  .change.REMOVED { border-color: #dc2626; background: #fef2f2; }
  .action { font-weight: 700; font-size: 12px; letter-spacing: 0.5px; }
  .section { color: #6b7280; font-size: 13px; margin: 0.2em 0 0.3em; }
  .name { font-weight: 600; }
  .meta { font-size: 13px; color: #4b5563; margin-top: 0.3em; }
  #spinner { display: none; padding: 2em; text-align: center; color: #6b7280; }
</style>
</head>
<body>
<h1>Mavat Watch</h1>
<p class="lead">Paste a Mavat plan URL, click <em>Check now</em>. Documents under the three accordion sections are parsed, diffed against the previous check, and any change is emailed.</p>

<form method="post" action="/check" onsubmit="document.getElementById('spinner').style.display='block'">
  <input type="text" name="url" value="{{ url }}" required>
  <button type="submit">Check now</button>
</form>

<div class="row-actions">
  <form method="post" action="/simulate" style="margin:0">
    <button type="submit" class="secondary">Simulate change</button>
  </form>
</div>

<p class="examples">
  Try: <code onclick="document.querySelector('input[name=url]').value=this.innerText">https://mavat.iplan.gov.il/SV4/1/3005115162/310</code>
</p>

<div id="spinner">Checking — usually ~30 seconds...</div>

{% if last_check %}
<div class="status">
  Last check: <code>{{ last_check }}</code> · Stored URL: <code>{{ stored_url }}</code> · Rows on file: {{ total_rows }}
</div>
{% endif %}

{% if message %}<div class="summary">{{ message }}</div>{% endif %}

{% if result %}
  {% if result.first_run %}
    <div class="summary">Baseline recorded ({{ result.total_rows }} rows). The next check will diff against this.</div>
  {% elif not result.changes %}
    <div class="summary">No changes detected ({{ result.total_rows }} rows checked).</div>
  {% else %}
    <div class="summary">{{ result.changes|length }} change(s) detected</div>
    {% for c in result.changes %}
      <div class="change {{ c.action }}">
        <span class="action">{{ c.action }}</span>
        <div class="section">{{ c.section }}{% if c.category %} / {{ c.category }}{% endif %}</div>
        <div class="name">{{ c.name }}</div>
        <div class="meta">
          {% if c.action == "UPDATED" %}
            {% if c.edit_date != c.prev_edit_date %}edit_date: <s>{{ c.prev_edit_date }}</s> → <strong>{{ c.edit_date }}</strong>{% endif %}
            {% if c.scope != c.prev_scope %}<br>scope: <s>{{ c.prev_scope }}</s> → <strong>{{ c.scope }}</strong>{% endif %}
          {% elif c.action == "NEW" %}
            date: {{ c.edit_date }} · scope: {{ c.scope or '—' }}
          {% elif c.action == "REMOVED" %}
            was: {{ c.prev_edit_date }} · {{ c.prev_scope or '—' }}
          {% endif %}
        </div>
      </div>
    {% endfor %}
    {% if result.email_status %}<div class="status">{{ result.email_status }}</div>{% endif %}
  {% endif %}
{% endif %}
</body></html>
"""


def view_data(result=None, message=None, url=None):
    state = load_state()
    return {
        "url": url or state.get("url", DEFAULT_URL),
        "last_check": state.get("last_check"),
        "stored_url": state.get("url"),
        "total_rows": sum(len(rows) for rows in state.get("rows", {}).values()),
        "result": result,
        "message": message,
    }


@app.get("/")
def index():
    return render_template_string(PAGE, **view_data())


@app.post("/check")
def check():
    url = request.form["url"].strip()
    result = run_check(url)
    result_dict = {
        "first_run": result["first_run"],
        "total_rows": result["total_rows"],
        "email_status": result["email_status"],
        "changes": [asdict(c) for c in result["changes"]],
    }
    return render_template_string(PAGE, **view_data(result=result_dict, url=url))


@app.post("/simulate")
def simulate_route():
    if simulate():
        msg = "Stored state tampered. Click 'Check now' to trigger a notification."
    else:
        msg = "No baseline yet — run a check first."
    return render_template_string(PAGE, **view_data(message=msg))


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5050, debug=False)
