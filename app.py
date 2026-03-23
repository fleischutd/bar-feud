import os
import io
import base64
import socket
import sqlite3
import json
import secrets
from datetime import datetime
from collections import Counter
from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify, g
import qrcode

app = Flask(__name__)
app.secret_key = secrets.token_hex(32)

DB_PATH = os.path.join(os.path.dirname(__file__), "barfeud.db")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "barfeud2026")


# ── Database ─────────────────────────────────────────────────────────────────

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS questions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT NOT NULL,
            week_label TEXT,
            active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS responses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            question_id INTEGER NOT NULL,
            answer TEXT NOT NULL,
            respondent TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (question_id) REFERENCES questions(id)
        );
        CREATE TABLE IF NOT EXISTS consolidated (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            question_id INTEGER NOT NULL,
            display_answer TEXT NOT NULL,
            count INTEGER DEFAULT 0,
            points INTEGER DEFAULT 0,
            rank INTEGER DEFAULT 0,
            FOREIGN KEY (question_id) REFERENCES questions(id)
        );
    """)
    db.close()


init_db()


# ── Fuzzy consolidation ──────────────────────────────────────────────────────

def consolidate_answers(question_id):
    """Group similar free-text answers using fuzzy matching."""
    try:
        from thefuzz import fuzz
    except ImportError:
        fuzz = None

    db = get_db()
    rows = db.execute(
        "SELECT answer FROM responses WHERE question_id = ?", (question_id,)
    ).fetchall()

    raw = [r["answer"].strip() for r in rows if r["answer"].strip()]
    if not raw:
        return []

    THRESHOLD = 75  # similarity threshold
    groups = []  # list of {"label": str, "members": [str]}

    for answer in raw:
        matched = False
        for group in groups:
            if fuzz:
                score = fuzz.token_sort_ratio(answer.lower(), group["label"].lower())
            else:
                score = 100 if answer.lower() == group["label"].lower() else 0
            if score >= THRESHOLD:
                group["members"].append(answer)
                matched = True
                break
        if not matched:
            groups.append({"label": answer, "members": [answer]})

    # Pick the most common phrasing as display label
    for group in groups:
        counter = Counter(m.lower() for m in group["members"])
        most_common = counter.most_common(1)[0][0]
        # Find original-case version
        for m in group["members"]:
            if m.lower() == most_common:
                group["label"] = m.title()
                break

    # Sort by count descending
    groups.sort(key=lambda g: len(g["members"]), reverse=True)

    # Assign points (Family Feud style: proportional to responses)
    total = len(raw)
    results = []
    for rank, group in enumerate(groups, 1):
        count = len(group["members"])
        points = round((count / total) * 100) if total else 0
        results.append({
            "display_answer": group["label"],
            "count": count,
            "points": points,
            "rank": rank,
        })

    return results


# ── Auth helper ───────────────────────────────────────────────────────────────

def admin_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("admin_login"))
        return f(*args, **kwargs)
    return decorated


def get_local_ip():
    """Get the machine's local network IP."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "localhost"


def make_qr_data_uri(url):
    """Generate a QR code as a base64 data URI."""
    qr = qrcode.QRCode(version=1, box_size=10, border=2)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#0c2340", back_color="#faf6eb")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    return f"data:image/png;base64,{b64}"


# ── Client routes ─────────────────────────────────────────────────────────────

@app.route("/")
def survey():
    db = get_db()
    questions = db.execute(
        "SELECT * FROM questions WHERE active = 1 ORDER BY id"
    ).fetchall()
    submitted = session.get("submitted_questions", [])
    return render_template("survey.html", questions=questions, submitted=submitted)


@app.route("/submit", methods=["POST"])
def submit():
    db = get_db()
    respondent = request.form.get("respondent", "Anonymous")
    submitted = session.get("submitted_questions", [])

    questions = db.execute("SELECT id FROM questions WHERE active = 1").fetchall()
    for q in questions:
        answer = request.form.get(f"answer_{q['id']}", "").strip()
        if answer and q["id"] not in submitted:
            db.execute(
                "INSERT INTO responses (question_id, answer, respondent) VALUES (?, ?, ?)",
                (q["id"], answer, respondent),
            )
            submitted.append(q["id"])

    db.commit()
    session["submitted_questions"] = submitted
    flash("Thanks! Your answers are in. Good luck!", "success")
    return redirect(url_for("survey"))


# ── Admin routes ──────────────────────────────────────────────────────────────

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        if request.form.get("password") == ADMIN_PASSWORD:
            session["is_admin"] = True
            return redirect(url_for("admin_dashboard"))
        flash("Wrong password", "error")
    return render_template("admin_login.html")


@app.route("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    return redirect(url_for("survey"))


@app.route("/admin")
@admin_required
def admin_dashboard():
    db = get_db()
    questions = db.execute("SELECT * FROM questions ORDER BY active DESC, id DESC").fetchall()
    stats = {}
    for q in questions:
        count = db.execute(
            "SELECT COUNT(*) as c FROM responses WHERE question_id = ?", (q["id"],)
        ).fetchone()["c"]
        stats[q["id"]] = count
    local_ip = get_local_ip()
    port = request.host.split(":")[-1] if ":" in request.host else "5050"
    survey_url = f"http://{local_ip}:{port}"
    qr_data = make_qr_data_uri(survey_url)
    return render_template(
        "admin_dashboard.html", questions=questions, stats=stats,
        survey_url=survey_url, qr_data=qr_data,
    )


@app.route("/admin/questions/add", methods=["POST"])
@admin_required
def add_question():
    text = request.form.get("text", "").strip()
    week = request.form.get("week_label", "").strip()
    if text:
        db = get_db()
        db.execute(
            "INSERT INTO questions (text, week_label) VALUES (?, ?)",
            (text, week or None),
        )
        db.commit()
        flash("Question added!", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/questions/<int:qid>/toggle")
@admin_required
def toggle_question(qid):
    db = get_db()
    db.execute("UPDATE questions SET active = CASE WHEN active=1 THEN 0 ELSE 1 END WHERE id = ?", (qid,))
    db.commit()
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/questions/<int:qid>/delete", methods=["POST"])
@admin_required
def delete_question(qid):
    db = get_db()
    db.execute("DELETE FROM responses WHERE question_id = ?", (qid,))
    db.execute("DELETE FROM consolidated WHERE question_id = ?", (qid,))
    db.execute("DELETE FROM questions WHERE id = ?", (qid,))
    db.commit()
    flash("Question deleted.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/questions/<int:qid>/responses")
@admin_required
def view_responses(qid):
    db = get_db()
    question = db.execute("SELECT * FROM questions WHERE id = ?", (qid,)).fetchone()
    raw = db.execute(
        "SELECT * FROM responses WHERE question_id = ? ORDER BY created_at DESC", (qid,)
    ).fetchall()
    consolidated = consolidate_answers(qid)
    return render_template(
        "admin_responses.html", question=question, raw=raw, consolidated=consolidated
    )


@app.route("/admin/questions/<int:qid>/save-board", methods=["POST"])
@admin_required
def save_board(qid):
    """Save the consolidated answers as the official game board."""
    db = get_db()
    db.execute("DELETE FROM consolidated WHERE question_id = ?", (qid,))

    consolidated = consolidate_answers(qid)
    for item in consolidated[:8]:  # Top 8 answers max
        db.execute(
            "INSERT INTO consolidated (question_id, display_answer, count, points, rank) VALUES (?, ?, ?, ?, ?)",
            (qid, item["display_answer"], item["count"], item["points"], item["rank"]),
        )
    db.commit()
    flash("Game board saved!", "success")
    return redirect(url_for("view_responses", qid=qid))


@app.route("/admin/questions/<int:qid>/edit-answer", methods=["POST"])
@admin_required
def edit_answer(qid):
    """Let admin rename a consolidated answer's display text."""
    data = request.get_json()
    old_label = data.get("old_label")
    new_label = data.get("new_label", "").strip()
    if old_label and new_label:
        db = get_db()
        db.execute(
            "UPDATE consolidated SET display_answer = ? WHERE question_id = ? AND display_answer = ?",
            (new_label, qid, old_label),
        )
        db.commit()
        return jsonify({"ok": True})
    return jsonify({"ok": False}), 400


@app.route("/admin/gameboard")
@admin_required
def gameboard():
    db = get_db()
    questions = db.execute(
        "SELECT q.*, COUNT(c.id) as board_count FROM questions q "
        "LEFT JOIN consolidated c ON c.question_id = q.id "
        "GROUP BY q.id HAVING board_count > 0 ORDER BY q.id DESC"
    ).fetchall()
    boards = {}
    for q in questions:
        boards[q["id"]] = db.execute(
            "SELECT * FROM consolidated WHERE question_id = ? ORDER BY rank", (q["id"],)
        ).fetchall()
    return render_template("admin_gameboard.html", questions=questions, boards=boards)


@app.route("/admin/reset-week", methods=["POST"])
@admin_required
def reset_week():
    """Deactivate all current questions and clear submitted sessions for a new week."""
    db = get_db()
    db.execute("UPDATE questions SET active = 0")
    db.commit()
    session.pop("submitted_questions", None)
    flash("All questions deactivated. Ready for a new week!", "success")
    return redirect(url_for("admin_dashboard"))


# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    app.run(debug=True, host="0.0.0.0", port=port)
