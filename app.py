"""
Adaptive Test & Personalized Learning Platform
------------------------------------------------
A simple college mini-project built with Flask + SQLite.

Main ideas
* Students sign up with a stream (Science or Computer Science).
* Each subject has 5 topics and each topic has 30 questions (questions.py):
  10 Easy, 10 Medium and 10 Hard.
* Practice  : three batches of 10 questions per topic -
              Batch 1 = Easy, Batch 2 = Medium, Batch 3 = Hard.
              A batch unlocks after the previous one is submitted, so no
              question repeats until all 30 have been attempted.
* Topic test: 10 random questions (3 Easy, 4 Medium, 3 Hard) - unlocked
              after all three practice batches of the topic are done.
* Final test: 15 questions (3 from each topic) - unlocked after all
              5 topic tests of that subject are taken.
* Every submission is saved as a new attempt, so the Results page shows
  the complete history.
* Progress, weak topics and recommendations are calculated from the
  saved attempts using simple percentages.

Run:  python app.py   then open  http://127.0.0.1:5000
"""

import os
import random
import secrets
import sqlite3
from datetime import datetime
from functools import wraps

from flask import (Flask, abort, flash, g, redirect, render_template, request,
                   session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

from questions import STREAMS, SUBJECTS

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# The HTML pages live in the project folder and the stylesheet in css/.
app = Flask(__name__, template_folder=BASE_DIR,
            static_folder=os.path.join(BASE_DIR, "css"), static_url_path="/css")
# test_app.py sets ADAPTIVE_DB so the tests use a temporary database.
app.config["DATABASE"] = os.environ.get("ADAPTIVE_DB", os.path.join(BASE_DIR, "adaptive_learning.db"))

TOPIC_TEST_SIZE = 10          # 3 Easy + 4 Medium + 3 Hard
FINAL_TEST_SIZE = 15          # 3 questions from each of the 5 topics
BATCH_SIZE = 10
BATCHES = {1: "Easy", 2: "Medium", 3: "Hard"}      # practice batch -> difficulty
RESULTS_PER_PAGE = 10
KIND_NAMES = {"practice": "Practice", "topic_test": "Topic Test", "final_test": "Final Test"}


# ---------------------------------------------------------------------------
# Secret key: created once and saved in secret_key.txt, so login sessions
# keep working after the server restarts. The file is not shared (.gitignore).
# ---------------------------------------------------------------------------
def load_secret_key():
    key_file = os.path.join(BASE_DIR, "secret_key.txt")
    if not os.path.exists(key_file):
        with open(key_file, "w") as f:
            f.write(secrets.token_hex(32))
    with open(key_file) as f:
        return f.read().strip()


app.secret_key = load_secret_key()


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name     TEXT NOT NULL,
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    stream        TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS questions (
    id             INTEGER PRIMARY KEY,
    subject        TEXT NOT NULL,
    topic          TEXT NOT NULL,
    difficulty     TEXT NOT NULL,
    question       TEXT NOT NULL,
    option_a       TEXT NOT NULL,
    option_b       TEXT NOT NULL,
    option_c       TEXT NOT NULL,
    option_d       TEXT NOT NULL,
    correct_option INTEGER NOT NULL,   -- 0 = A, 1 = B, 2 = C, 3 = D
    explanation    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS attempts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind       TEXT NOT NULL,          -- practice / topic_test / final_test
    subject    TEXT NOT NULL,
    topic      TEXT,                   -- empty for a final test
    score      INTEGER NOT NULL,
    total      INTEGER NOT NULL,
    percentage INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    batch      INTEGER                 -- practice batch 1-3 (empty for tests)
);

CREATE TABLE IF NOT EXISTS answers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id      INTEGER NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
    question_id     INTEGER NOT NULL REFERENCES questions(id),
    selected_option INTEGER,           -- empty if not answered
    is_correct      INTEGER NOT NULL
);
"""


def get_db():
    """Open one database connection per request."""
    if "db" not in g:
        g.db = sqlite3.connect(app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exception):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    """Create the tables and copy any new questions from questions.py.

    Safe for an existing database: nothing is deleted. Questions that are
    already stored keep their id (so old answers still point to them) and
    only the missing questions are added with new ids.
    """
    with app.app_context():
        db = get_db()
        db.executescript(SCHEMA)
        # Databases created before practice batches existed: add the new column.
        columns = [row["name"] for row in db.execute("PRAGMA table_info(attempts)")]
        if "batch" not in columns:
            db.execute("ALTER TABLE attempts ADD COLUMN batch INTEGER")
        stored = {(row["subject"], row["topic"], row["question"])
                  for row in db.execute("SELECT subject, topic, question FROM questions")}
        next_id = db.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM questions").fetchone()[0]
        for subject_key, subject in SUBJECTS.items():
            for topic, questions in subject["topics"].items():
                for difficulty, text, options, answer, explanation in questions:
                    if (subject_key, topic, text) in stored:
                        continue
                    db.execute(
                        "INSERT INTO questions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (next_id, subject_key, topic, difficulty, text, *options,
                         options.index(answer), explanation))
                    next_id += 1
        db.commit()


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def now_text():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def score_category(percentage):
    """Return (label, colour) for a score."""
    if percentage is None:
        return ("Not attempted", "gray")
    if percentage < 40:
        return ("Need Practice", "red")
    if percentage < 70:
        return ("Average", "amber")
    return ("Good", "green")


def level(percentage):
    """Colour used by progress bars and pills."""
    return score_category(percentage)[1]


def topic_names(subject_key):
    return list(SUBJECTS[subject_key]["topics"].keys())


def current_user():
    if "user_id" not in session:
        return None
    if "user" not in g:
        g.user = get_db().execute("SELECT * FROM users WHERE id = ?",
                                  (session["user_id"],)).fetchone()
    return g.user


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if current_user() is None:
            session.clear()
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


def my_subjects():
    """Subject keys for the logged-in student's stream."""
    return STREAMS[current_user()["stream"]]["subjects"]


def check_subject_topic(subject_key, topic_no=None):
    """Validate the subject (and topic number 1-5) from the URL or form."""
    if subject_key not in my_subjects():
        abort(404)
    if topic_no is None:
        return None
    try:
        return topic_names(subject_key)[int(topic_no) - 1]
    except (ValueError, IndexError):
        abort(404)


@app.context_processor
def template_helpers():
    """Values every template can use."""
    user = current_user()
    info = {}
    if user:
        parts = user["full_name"].split()
        info = {
            "full_name": user["full_name"],
            "first_name": parts[0],
            "initials": "".join(p[0] for p in parts[:2]).upper(),
            "email": user["email"],
            "stream": STREAMS[user["stream"]]["name"],
        }
    return {"current_user": info, "level": level, "category": score_category,
            "current_subjects": STREAMS[user["stream"]]["subjects"] if user else [],
            "SUBJECTS": SUBJECTS, "KIND_NAMES": KIND_NAMES, "BATCHES": BATCHES}


# ---------------------------------------------------------------------------
# Progress calculations (simple percentages, no machine learning)
# ---------------------------------------------------------------------------
def get_attempts(user_id, kind=None, subject=None):
    sql = "SELECT * FROM attempts WHERE user_id = ?"
    params = [user_id]
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    if subject:
        sql += " AND subject = ?"
        params.append(subject)
    return get_db().execute(sql + " ORDER BY id DESC", params).fetchall()


def subject_progress(user_id, subject_key):
    """Topic-by-topic status for one subject."""
    attempts = get_attempts(user_id, subject=subject_key)
    topics = []
    for number, name in enumerate(topic_names(subject_key), start=1):
        practice = [a for a in attempts if a["kind"] == "practice" and a["topic"] == name]
        tests = [a for a in attempts if a["kind"] == "topic_test" and a["topic"] == name]
        latest = tests[0]["percentage"] if tests else None      # newest first
        batches_done = sorted({a["batch"] for a in practice if a["batch"]})
        practice_complete = len(batches_done) == len(BATCHES)
        # Next batch to practise: the first one not done yet (all done -> start again at 1)
        next_batch = next((b for b in BATCHES if b not in batches_done), 1)
        batch_scores = {}
        for a in reversed(practice):              # oldest first, so the newest score wins
            if a["batch"]:
                batch_scores[a["batch"]] = a["percentage"]
        topics.append({
            "number": number,
            "name": name,
            "practice_done": len(practice) > 0,
            "practice_best": max((a["percentage"] for a in practice), default=None),
            "batches_done": batches_done,
            "batch_scores": batch_scores,
            "practice_complete": practice_complete,
            "next_batch": next_batch,
            # Students who already took this topic test (before batches existed) keep access.
            "test_unlocked": practice_complete or len(tests) > 0,
            "test_count": len(tests),
            "latest": latest,
            "best": max((a["percentage"] for a in tests), default=None),
            "category": score_category(latest)[0],
        })
    finals = [a for a in attempts if a["kind"] == "final_test"]
    completed = sum(1 for t in topics if t["test_count"] > 0)
    return {
        "key": subject_key,
        "name": SUBJECTS[subject_key]["name"],
        "icon": SUBJECTS[subject_key]["icon"],
        "topics": topics,
        "completed": completed,
        "practised": sum(1 for t in topics if t["practice_done"]),
        "percent": completed * 100 // len(topics),
        "final_unlocked": completed == len(topics),
        "final_latest": finals[0]["percentage"] if finals else None,
        "final_best": max((a["percentage"] for a in finals), default=None),
        "final_count": len(finals),
    }


def all_progress(user_id):
    return [subject_progress(user_id, key) for key in my_subjects()]


def weak_topics(progress):
    """Topics whose latest topic test score is below 70%, weakest first."""
    weak = []
    for subject in progress:
        for topic in subject["topics"]:
            if topic["latest"] is not None and topic["latest"] < 70:
                weak.append({"subject": subject, "topic": topic})
    return sorted(weak, key=lambda item: item["topic"]["latest"])


def build_recommendations(progress):
    """Turn progress into a short list of simple study suggestions."""
    recs = []
    for item in weak_topics(progress):
        topic, subject = item["topic"], item["subject"]
        if topic["latest"] < 40:
            advice = "Revise this topic, redo the practice set and then retake the topic test."
        else:
            advice = "Review your mistakes and retake the topic test to reach 70% or more."
        recs.append({"subject": subject, "topic": topic, "advice": advice})
    return recs


def overall_stats(user_id, progress):
    tests = [a for a in get_attempts(user_id) if a["kind"] != "practice"]
    total_topics = sum(len(s["topics"]) for s in progress)
    completed = sum(s["completed"] for s in progress)
    return {
        "tests_taken": len(tests),
        "practice_sessions": len(get_attempts(user_id, kind="practice")),
        "average": round(sum(a["percentage"] for a in tests) / len(tests)) if tests else None,
        "best": max((a["percentage"] for a in tests), default=None),
        "topics_completed": completed,
        "total_topics": total_topics,
        "overall_percent": completed * 100 // total_topics,
    }


# ---------------------------------------------------------------------------
# Question selection and grading
# ---------------------------------------------------------------------------
def batch_unlocked(topic_info, batch):
    """Batch 1 is always open; batch 2 needs batch 1 done, batch 3 needs batch 2 done."""
    return batch == 1 or (batch - 1) in topic_info["batches_done"]


def pick_questions(kind, subject_key, topic, batch=None):
    db = get_db()
    if kind == "practice":
        # A practice batch = the 10 questions of one difficulty, in a fixed order.
        return db.execute("SELECT * FROM questions WHERE subject = ? AND topic = ? AND difficulty = ?"
                          " ORDER BY id", (subject_key, topic, BATCHES[batch])).fetchall()
    if kind == "topic_test":
        # 3 Easy + 4 Medium + 3 Hard questions picked at random from the 30.
        chosen = []
        for difficulty, how_many in (("Easy", 3), ("Medium", 4), ("Hard", 3)):
            rows = db.execute("SELECT * FROM questions WHERE subject = ? AND topic = ? AND difficulty = ?",
                              (subject_key, topic, difficulty)).fetchall()
            chosen += random.sample(list(rows), how_many)
        random.shuffle(chosen)
        return chosen
    # Final test: one Easy, one Medium and one Hard question from every topic.
    chosen = []
    for name in topic_names(subject_key):
        for difficulty in ("Easy", "Medium", "Hard"):
            rows = db.execute("SELECT * FROM questions WHERE subject = ? AND topic = ? AND difficulty = ?",
                              (subject_key, name, difficulty)).fetchall()
            chosen.append(random.choice(rows))
    random.shuffle(chosen)
    return chosen


def question_view(row):
    """Convert a database row into a dict that the templates use."""
    return {
        "id": row["id"],
        "topic": row["topic"],
        "difficulty": row["difficulty"],
        "text": row["question"],
        "options": [row["option_a"], row["option_b"], row["option_c"], row["option_d"]],
    }


def save_attempt(user_id, kind, subject_key, topic, question_ids, form, batch=None):
    """Grade the submitted answers and store the attempt. Returns attempt id."""
    db = get_db()
    rows = []
    for qid in question_ids:
        row = db.execute("SELECT * FROM questions WHERE id = ? AND subject = ?",
                         (qid, subject_key)).fetchone()
        if row is None or (topic and row["topic"] != topic):
            abort(400)
        rows.append(row)

    results = []
    for row in rows:
        selected = form.get("q%d" % row["id"])
        selected = int(selected) if selected in ("0", "1", "2", "3") else None
        results.append((row["id"], selected, selected == row["correct_option"]))

    score = sum(1 for r in results if r[2])
    total = len(results)
    cursor = db.execute(
        "INSERT INTO attempts (user_id, kind, subject, topic, score, total, percentage, created_at, batch)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (user_id, kind, subject_key, topic, score, total, round(score * 100 / total), now_text(), batch))
    attempt_id = cursor.lastrowid
    db.executemany(
        "INSERT INTO answers (attempt_id, question_id, selected_option, is_correct) VALUES (?, ?, ?, ?)",
        [(attempt_id, qid, sel, int(ok)) for qid, sel, ok in results])
    db.commit()
    return attempt_id


def load_attempt(attempt_id):
    """Load one attempt of the logged-in student with every answer."""
    db = get_db()
    attempt = db.execute("SELECT * FROM attempts WHERE id = ? AND user_id = ?",
                         (attempt_id, session["user_id"])).fetchone()
    if attempt is None:
        abort(404)
    rows = db.execute(
        "SELECT q.*, a.selected_option, a.is_correct FROM answers a"
        " JOIN questions q ON q.id = a.question_id WHERE a.attempt_id = ? ORDER BY a.id",
        (attempt_id,)).fetchall()
    items = []
    for row in rows:
        item = question_view(row)
        item["selected"] = row["selected_option"]
        item["correct"] = row["correct_option"]
        item["is_correct"] = bool(row["is_correct"])
        item["explanation"] = row["explanation"]
        items.append(item)
    return attempt, items


def describe(attempt):
    """Readable title for an attempt, e.g. 'Mathematics · Probability'."""
    subject = SUBJECTS[attempt["subject"]]["name"]
    return "%s · %s" % (subject, attempt["topic"] or "Final Subject Test")


def test_name(attempt):
    """Name of the test, e.g. 'Practice – Batch 2 (Medium)' or 'Topic Test'."""
    if attempt["kind"] == "practice":
        if attempt["batch"]:
            return "Practice – Batch %d (%s)" % (attempt["batch"], BATCHES[attempt["batch"]])
        return "Practice Set"          # practice taken before batches existed
    if attempt["kind"] == "topic_test":
        return "Topic Test"
    return "Final Subject Test"


app.jinja_env.globals["describe"] = describe
app.jinja_env.globals["test_name"] = test_name


# ---------------------------------------------------------------------------
# Public pages: home, signup, login, logout
# ---------------------------------------------------------------------------
@app.route("/")
@app.route("/index.html")
def home():
    return render_template("index.html", streams=STREAMS)


@app.route("/signup.html", methods=["GET", "POST"])
def signup():
    form = request.form
    error = None
    if request.method == "POST":
        full_name = form.get("full_name", "").strip()
        email = form.get("email", "").strip().lower()
        password = form.get("password", "")
        stream = form.get("stream", "")
        if not full_name or not email or not password:
            error = "Please fill in all the fields."
        elif "@" not in email or "." not in email:
            error = "Please enter a valid email address."
        elif len(password) < 6:
            error = "Password must be at least 6 characters long."
        elif password != form.get("confirm_password", ""):
            error = "Passwords do not match."
        elif stream not in STREAMS:
            error = "Please choose your stream."
        elif get_db().execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone():
            error = "An account with this email already exists."
        else:
            db = get_db()
            cursor = db.execute(
                "INSERT INTO users (full_name, email, password_hash, stream, created_at) VALUES (?, ?, ?, ?, ?)",
                (full_name, email, generate_password_hash(password), stream, now_text()))
            db.commit()
            session.clear()
            session["user_id"] = cursor.lastrowid
            flash("Welcome, %s! Your account has been created." % full_name.split()[0])
            return redirect(url_for("dashboard"))
    return render_template("signup.html", error=error, form=form, streams=STREAMS)


@app.route("/login.html", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        user = get_db().execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if user and check_password_hash(user["password_hash"], request.form.get("password", "")):
            session.clear()
            session["user_id"] = user["id"]
            return redirect(url_for("dashboard"))
        error = "Incorrect email or password."
    return render_template("login.html", error=error, form=request.form)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Student pages
# ---------------------------------------------------------------------------
@app.route("/dashboard.html")
@login_required
def dashboard():
    user_id = session["user_id"]
    progress = all_progress(user_id)
    return render_template("dashboard.html", progress=progress,
                           stats=overall_stats(user_id, progress),
                           weak=weak_topics(progress)[:3],
                           recent=get_attempts(user_id)[:5])


@app.route("/practice.html")
@login_required
def practice():
    """Without a topic: list topics. With ?subject=&topic=: show the practice set."""
    subject_key = request.args.get("subject")
    topic_no = request.args.get("topic")
    if subject_key and topic_no:
        topic = check_subject_topic(subject_key, topic_no)
        info = subject_progress(session["user_id"], subject_key)["topics"][int(topic_no) - 1]
        # Without ?batch= the student continues with the next unfinished batch.
        batch = request.args.get("batch", type=int) or info["next_batch"]
        if batch not in BATCHES:
            abort(404)
        if not batch_unlocked(info, batch):
            flash("Finish Batch %d first to unlock Batch %d." % (batch - 1, batch))
            return redirect(url_for("practice", subject=subject_key))
        questions = [question_view(r) for r in pick_questions("practice", subject_key, topic, batch)]
        return render_template("practice.html", questions=questions, subject_key=subject_key,
                               topic=topic, topic_no=topic_no, batch=batch, info=info)
    return render_template("practice.html", questions=None,
                           progress=all_progress(session["user_id"]),
                           selected=subject_key if subject_key in my_subjects() else None)


@app.route("/take-test.html")
@login_required
def take_test():
    subject_key = request.args.get("subject")
    return render_template("take-test.html", progress=all_progress(session["user_id"]),
                           selected=subject_key if subject_key in my_subjects() else None)


@app.route("/online-test.html")
@login_required
def online_test():
    """Show a topic test (?kind=topic_test&subject=&topic=) or a final test."""
    kind = request.args.get("kind", "topic_test")
    subject_key = request.args.get("subject", "")
    if kind == "topic_test":
        topic_no = request.args.get("topic")
        topic = check_subject_topic(subject_key, topic_no)
        info = subject_progress(session["user_id"], subject_key)["topics"][int(topic_no) - 1]
        if not info["test_unlocked"]:
            flash('Complete all three practice batches of "%s" to unlock its topic test.' % topic)
            return redirect(url_for("practice", subject=subject_key))
    elif kind == "final_test":
        check_subject_topic(subject_key)
        topic, topic_no = None, None
        if not subject_progress(session["user_id"], subject_key)["final_unlocked"]:
            flash("Complete all five topic tests to unlock the final test.")
            return redirect(url_for("take_test", subject=subject_key))
    else:
        abort(404)
    questions = [question_view(r) for r in pick_questions(kind, subject_key, topic)]
    return render_template("online-test.html", questions=questions, kind=kind,
                           subject_key=subject_key, topic=topic, topic_no=topic_no)


@app.route("/results.html", methods=["GET", "POST"])
@login_required
def results():
    user_id = session["user_id"]
    if request.method == "POST":
        kind = request.form.get("kind")
        subject_key = request.form.get("subject", "")
        if kind not in KIND_NAMES:
            abort(400)
        topic, batch = None, None
        ids = [int(x) for x in request.form.get("question_ids", "").split(",") if x.isdigit()]
        if kind != "final_test":
            topic_no = request.form.get("topic")
            topic = check_subject_topic(subject_key, topic_no)
            info = subject_progress(user_id, subject_key)["topics"][int(topic_no) - 1]
            if kind == "practice":
                batch = request.form.get("batch", type=int)
                if batch not in BATCHES:
                    abort(400)
                if not batch_unlocked(info, batch):
                    abort(403)
                # A practice batch must contain exactly the 10 questions of that batch.
                batch_ids = {r["id"] for r in pick_questions("practice", subject_key, topic, batch)}
                if set(ids) != batch_ids:
                    abort(400)
            elif not info["test_unlocked"]:
                abort(403)
        else:
            check_subject_topic(subject_key)
            if not subject_progress(user_id, subject_key)["final_unlocked"]:
                abort(403)
        expected = {"practice": BATCH_SIZE, "topic_test": TOPIC_TEST_SIZE, "final_test": FINAL_TEST_SIZE}[kind]
        if len(ids) != expected or len(set(ids)) != len(ids):
            abort(400)
        attempt_id = save_attempt(user_id, kind, subject_key, topic, ids, request.form, batch)
        return redirect(url_for("results", attempt=attempt_id))

    attempt_id = request.args.get("attempt", type=int)
    if attempt_id is None:
        return results_history(user_id)
    attempt, items = load_attempt(attempt_id)      # 404 if it is not this student's attempt
    topic_no, topic_info = None, None
    if attempt["topic"]:
        topic_no = topic_names(attempt["subject"]).index(attempt["topic"]) + 1
        if attempt["subject"] in my_subjects():
            topic_info = subject_progress(user_id, attempt["subject"])["topics"][topic_no - 1]
    return render_template("results.html", attempt=attempt, items=items, topic_no=topic_no,
                           topic_info=topic_info)


def results_history(user_id):
    """Results Portal: every attempt of this student, newest first, 10 per page."""
    kind = request.args.get("type", "")
    if kind not in KIND_NAMES:
        kind = ""
    attempts = get_attempts(user_id, kind=kind or None)          # newest first
    pages = max((len(attempts) + RESULTS_PER_PAGE - 1) // RESULTS_PER_PAGE, 1)
    page = min(max(request.args.get("page", 1, type=int), 1), pages)
    start = (page - 1) * RESULTS_PER_PAGE
    all_attempts = get_attempts(user_id)
    counts = {k: sum(1 for a in all_attempts if a["kind"] == k) for k in KIND_NAMES}
    return render_template("results.html", attempt=None, history=attempts[start:start + RESULTS_PER_PAGE],
                           total=len(attempts), all_count=len(all_attempts), counts=counts,
                           kind=kind, page=page, pages=pages, start=start)


@app.route("/mistakes.html")
@login_required
def mistakes():
    user_id = session["user_id"]
    attempts = get_attempts(user_id)
    attempt_id = request.args.get("attempt", type=int)
    if attempt_id is None:
        if not attempts:
            return render_template("mistakes.html", attempts=[], attempt=None, wrong=[])
        attempt_id = attempts[0]["id"]
    attempt, items = load_attempt(attempt_id)     # 404 if it is not this student's attempt
    wrong = [item for item in items if not item["is_correct"]]
    return render_template("mistakes.html", attempts=attempts, attempt=attempt, wrong=wrong)


@app.route("/recommendations.html")
@login_required
def recommendations():
    progress = all_progress(session["user_id"])
    pending = [(s, t) for s in progress for t in s["topics"] if t["test_count"] == 0]
    finals = [s for s in progress if s["final_unlocked"]]
    return render_template("recommendations.html", recs=build_recommendations(progress),
                           pending=pending, finals=finals, progress=progress)


@app.route("/progress.html")
@login_required
def progress_page():
    user_id = session["user_id"]
    progress = all_progress(user_id)
    return render_template("progress.html", progress=progress,
                           stats=overall_stats(user_id, progress),
                           history=get_attempts(user_id))


@app.route("/profile.html", methods=["GET", "POST"])
@login_required
def profile():
    user_id = session["user_id"]
    error = None
    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        email = request.form.get("email", "").strip().lower()
        db = get_db()
        if not full_name or "@" not in email or "." not in email:
            error = "Please enter your name and a valid email address."
        elif db.execute("SELECT id FROM users WHERE email = ? AND id != ?", (email, user_id)).fetchone():
            error = "Another account already uses this email."
        else:
            db.execute("UPDATE users SET full_name = ?, email = ? WHERE id = ?", (full_name, email, user_id))
            db.commit()
            g.pop("user", None)
            flash("Your profile has been updated.")
            return redirect(url_for("profile"))
    progress = all_progress(user_id)
    return render_template("profile.html", error=error, stats=overall_stats(user_id, progress),
                           progress=progress, joined=current_user()["created_at"])


@app.route("/settings.html", methods=["GET", "POST"])
@login_required
def settings():
    error = None
    if request.method == "POST":
        user = current_user()
        current = request.form.get("current_password", "")
        new = request.form.get("new_password", "")
        if not check_password_hash(user["password_hash"], current):
            error = "Your current password is incorrect."
        elif len(new) < 6:
            error = "New password must be at least 6 characters long."
        elif new != request.form.get("confirm_password", ""):
            error = "New passwords do not match."
        else:
            db = get_db()
            db.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                       (generate_password_hash(new), user["id"]))
            db.commit()
            flash("Your password has been changed.")
            return redirect(url_for("settings"))
    return render_template("settings.html", error=error)


init_db()

if __name__ == "__main__":
    app.run(debug=True)
