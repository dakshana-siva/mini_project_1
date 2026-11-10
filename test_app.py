"""
Automatic tests for the Adaptive Test & Personalized Learning Platform.

Run:  python -m unittest test_app.py -v

The tests use a temporary database, so the real adaptive_learning.db is
never touched.
"""
import os
import re
import sqlite3
import tempfile
import unittest

TEMP_DIR = tempfile.mkdtemp()
os.environ["ADAPTIVE_DB"] = os.path.join(TEMP_DIR, "test.db")

from app import app, get_db, init_db  # noqa: E402  (must come after ADAPTIVE_DB is set)
from questions import STREAMS, SUBJECTS  # noqa: E402

app.config["TESTING"] = True


def correct_answers(question_ids):
    """Build a form that answers every question correctly."""
    with app.app_context():
        db = get_db()
        form = {}
        for qid in question_ids:
            row = db.execute("SELECT correct_option FROM questions WHERE id = ?", (qid,)).fetchone()
            form["q%d" % qid] = str(row["correct_option"])
        return form


class QuestionBankTests(unittest.TestCase):
    def test_question_bank_structure(self):
        self.assertEqual(len(SUBJECTS), 5)
        total, all_texts = 0, set()
        for key, subject in SUBJECTS.items():
            self.assertEqual(len(subject["topics"]), 5, key)
            for topic, questions in subject["topics"].items():
                self.assertEqual(len(questions), 30, topic)
                levels = [q[0] for q in questions]
                self.assertEqual((levels.count("Easy"), levels.count("Medium"), levels.count("Hard")),
                                 (10, 10, 10), topic)
                # Batches are Easy, then Medium, then Hard
                self.assertEqual(levels, ["Easy"] * 10 + ["Medium"] * 10 + ["Hard"] * 10, topic)
                for difficulty, text, options, answer, explanation in questions:
                    self.assertEqual(len(options), 4, text)
                    self.assertEqual(len(set(options)), 4, "duplicate option: " + text)
                    self.assertEqual(options.count(answer), 1, text)
                    self.assertTrue(explanation.strip(), text)
                    self.assertNotIn(text, all_texts, "duplicate question: " + text)
                    all_texts.add(text)
                total += len(questions)
        self.assertEqual(total, 750)
        self.assertEqual(len(all_texts), 750)

    def test_streams(self):
        for stream in STREAMS.values():
            self.assertEqual(len(stream["subjects"]), 4)
            for key in stream["subjects"]:
                self.assertIn(key, SUBJECTS)

    def test_questions_loaded_into_database(self):
        with app.app_context():
            db = get_db()
            self.assertEqual(db.execute("SELECT COUNT(*) FROM questions").fetchone()[0], 750)
            self.assertEqual(db.execute("SELECT COUNT(DISTINCT topic) FROM questions").fetchone()[0], 25)
        init_db()      # running again must not add duplicates
        with app.app_context():
            self.assertEqual(get_db().execute("SELECT COUNT(*) FROM questions").fetchone()[0], 750)


class UpgradeTests(unittest.TestCase):
    def test_old_database_is_upgraded_without_losing_data(self):
        """A database from the 250-question version keeps its users, attempts and answers."""
        old_path = os.path.join(TEMP_DIR, "old.db")
        db = sqlite3.connect(old_path)
        db.executescript("""
            CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, full_name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL, stream TEXT NOT NULL,
                created_at TEXT NOT NULL);
            CREATE TABLE questions (id INTEGER PRIMARY KEY, subject TEXT NOT NULL, topic TEXT NOT NULL,
                difficulty TEXT NOT NULL, question TEXT NOT NULL, option_a TEXT NOT NULL,
                option_b TEXT NOT NULL, option_c TEXT NOT NULL, option_d TEXT NOT NULL,
                correct_option INTEGER NOT NULL, explanation TEXT NOT NULL);
            CREATE TABLE attempts (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
                kind TEXT NOT NULL, subject TEXT NOT NULL, topic TEXT, score INTEGER NOT NULL,
                total INTEGER NOT NULL, percentage INTEGER NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE answers (id INTEGER PRIMARY KEY AUTOINCREMENT, attempt_id INTEGER NOT NULL,
                question_id INTEGER NOT NULL, selected_option INTEGER, is_correct INTEGER NOT NULL);
            INSERT INTO users VALUES (1, 'Old Student', 'old@example.com', 'x', 'science', '2026-11-01 10:00');
            INSERT INTO questions VALUES (1, 'mathematics', 'Sets, Relations and Functions', 'Easy',
                'If A = {1, 2, 3} and B = {3, 4}, what is A ∪ B?', '{3}', '{1, 2, 3, 4}', '{1, 2}', '{4}', 1, 'old');
            INSERT INTO attempts VALUES (1, 1, 'topic_test', 'mathematics', 'Sets, Relations and Functions',
                1, 1, 100, '2026-11-01 10:05');
            INSERT INTO answers VALUES (1, 1, 1, 1, 1);
        """)
        db.commit()
        db.close()
        app.config["DATABASE"] = old_path
        try:
            init_db()
            with app.app_context():
                db = get_db()
                self.assertEqual(db.execute("SELECT COUNT(*) FROM users").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM answers").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM questions").fetchone()[0], 750)
                # The old question kept id 1, so the old answer still points to it
                row = db.execute("SELECT question FROM questions WHERE id = 1").fetchone()
                self.assertIn("A ∪ B", row["question"])
                columns = [r["name"] for r in db.execute("PRAGMA table_info(attempts)")]
                self.assertIn("batch", columns)
        finally:
            app.config["DATABASE"] = os.environ["ADAPTIVE_DB"]


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    # ---------------- helpers ----------------
    def signup(self, name, email, stream, password="secret123"):
        return self.client.post("/signup.html", data={
            "full_name": name, "email": email, "stream": stream,
            "password": password, "confirm_password": password}, follow_redirects=True)

    def login(self, email, password="secret123"):
        return self.client.post("/login.html", data={"email": email, "password": password},
                                follow_redirects=True)

    def question_ids(self, html):
        match = re.search(r'name="question_ids" value="([\d,]+)"', html)
        self.assertIsNotNone(match, "question form not found")
        return [int(x) for x in match.group(1).split(",")]

    def submit(self, kind, subject, topic, html, wrong=0, batch=None):
        ids = self.question_ids(html)
        form = correct_answers(ids)
        # Answer the first `wrong` questions incorrectly
        for qid in ids[:wrong]:
            form["q%d" % qid] = str((int(form["q%d" % qid]) + 1) % 4)
        form.update({"kind": kind, "subject": subject, "topic": topic or "",
                     "question_ids": ",".join(map(str, ids)), "batch": batch or ""})
        return self.client.post("/results.html", data=form, follow_redirects=True)

    def practise_all_batches(self, subject, topic):
        """Submit batches 1, 2 and 3 of a topic. Returns the 30 question ids seen."""
        seen = []
        for batch in (1, 2, 3):
            page = self.client.get("/practice.html?subject=%s&topic=%s" % (subject, topic)).get_data(as_text=True)
            self.assertIn("Batch %d of 3" % batch, page)       # continues with the next batch
            seen += self.question_ids(page)
            self.submit("practice", subject, topic, page, batch=batch)
        return seen

    # ---------------- the full workflow ----------------
    def test_full_student_workflow(self):
        # 1. Register a Science Stream student
        page = self.signup("Priya Kumar", "priya@example.com", "science").get_data(as_text=True)
        self.assertIn("Welcome back, Priya", page)
        self.client.get("/logout")

        # 2. Login
        page = self.login("priya@example.com").get_data(as_text=True)
        self.assertIn("Welcome back, Priya", page)

        # 3. Dashboard shows the 4 Science stream subjects
        page = self.client.get("/dashboard.html").get_data(as_text=True)
        for name in ("Mathematics", "Science", "English", "General Aptitude"):
            self.assertIn(name, page)
        self.assertNotIn(">Computer Science</h3>", page)
        self.assertNotIn("Gemini", page)

        # 4. Topic list and an empty Results Portal
        page = self.client.get("/practice.html?subject=mathematics").get_data(as_text=True)
        self.assertIn("Sets, Relations and Functions", page)
        self.assertIn("Start Batch 1", page)
        self.assertIn("No results yet", self.client.get("/results.html").get_data(as_text=True))

        # The topic test and batch 2 are locked before any practice
        page = self.client.get("/online-test.html?kind=topic_test&subject=mathematics&topic=1",
                               follow_redirects=True).get_data(as_text=True)
        self.assertIn("Complete all three practice batches", page)
        page = self.client.get("/practice.html?subject=mathematics&topic=1&batch=2",
                               follow_redirects=True).get_data(as_text=True)
        self.assertIn("Finish Batch 1 first", page)

        # 5. Practice batch 1: 10 unique Easy questions, 7/10
        page = self.client.get("/practice.html?subject=mathematics&topic=1").get_data(as_text=True)
        self.assertIn("Batch 1 of 3", page)
        ids1 = self.question_ids(page)
        self.assertEqual(len(ids1), 10)
        self.assertEqual(len(set(ids1)), 10)
        result = self.submit("practice", "mathematics", "1", page, wrong=3, batch=1).get_data(as_text=True)
        self.assertIn("7 / 10", result)
        self.assertIn("Good", result)
        self.assertIn("Practice – Batch 1 (Easy)", result)
        self.assertIn("Answers and Explanations", result)
        self.assertIn("Continue &middot; Batch 2", result)

        # Batches 2 and 3 continue automatically; no question repeats across the 30
        ids = list(ids1)
        for batch in (2, 3):
            page = self.client.get("/practice.html?subject=mathematics&topic=1").get_data(as_text=True)
            self.assertIn("Batch %d of 3" % batch, page)
            batch_ids = self.question_ids(page)
            self.assertEqual(len(set(batch_ids)), 10)
            self.assertIn("Q%d" % ((batch - 1) * 10 + 1), page)      # Q11 / Q21
            ids += batch_ids
            self.submit("practice", "mathematics", "1", page, batch=batch)
        self.assertEqual(len(set(ids)), 30)

        # 6-7. Topic test is now unlocked: submit it and view the result (2/10 -> Need Practice)
        page = self.client.get("/online-test.html?kind=topic_test&subject=mathematics&topic=1").get_data(as_text=True)
        test_ids = self.question_ids(page)
        self.assertEqual(len(set(test_ids)), 10)
        self.assertTrue(set(test_ids) <= set(ids))
        result = self.submit("topic_test", "mathematics", "1", page, wrong=8).get_data(as_text=True)
        self.assertIn("20%", result)
        self.assertIn("Need Practice", result)

        # 8. Progress and recommendations
        page = self.client.get("/progress.html").get_data(as_text=True)
        self.assertIn("Test History", page)
        self.assertIn("Need Practice", page)
        page = self.client.get("/recommendations.html").get_data(as_text=True)
        self.assertIn("Sets, Relations and Functions", page)
        self.assertIn("Revise this topic", page)
        page = self.client.get("/mistakes.html").get_data(as_text=True)
        self.assertIn("8 mistakes", page)

        # Final test is locked until all 5 topic tests are done
        page = self.client.get("/online-test.html?kind=final_test&subject=mathematics", follow_redirects=True)
        self.assertIn("Complete all five topic tests", page.get_data(as_text=True))

        # 9. Practise and test topics 2-5, retake topic 1, then take the final test
        for topic in range(2, 6):
            self.practise_all_batches("mathematics", str(topic))
        for topic in range(1, 6):
            page = self.client.get("/online-test.html?kind=topic_test&subject=mathematics&topic=%d" % topic)
            self.submit("topic_test", "mathematics", str(topic), page.get_data(as_text=True))
        page = self.client.get("/take-test.html?subject=mathematics").get_data(as_text=True)
        self.assertIn("Start Final Test", page)
        page = self.client.get("/online-test.html?kind=final_test&subject=mathematics").get_data(as_text=True)
        ids = self.question_ids(page)
        self.assertEqual(len(ids), 15)
        self.assertEqual(len(set(ids)), 15)
        result = self.submit("final_test", "mathematics", None, page).get_data(as_text=True)
        self.assertIn("Final Subject Test", result)
        self.assertIn("100%", result)
        page = self.client.get("/recommendations.html").get_data(as_text=True)
        self.assertIn("No weak topics right now", page)
        page = self.client.get("/progress.html").get_data(as_text=True)
        self.assertIn("5/5 topics", page)

        # 10. Results Portal: complete history, newest first, nothing overwritten
        # 15 practice batches + 6 topic tests (topic 1 twice) + 1 final test = 22 attempts
        page = self.client.get("/results.html").get_data(as_text=True)
        self.assertIn("All results <span>22</span>", page)
        self.assertIn("Practice <span>15</span>", page)
        self.assertIn("Topic Tests <span>6</span>", page)
        self.assertIn("Final Tests <span>1</span>", page)
        self.assertIn("Showing 1&ndash;10 of 22 results", page)
        first = page.index("result-item")
        self.assertIn("Final Subject Test", page[first:first + 600])     # newest attempt first
        self.assertIn("#22", page)
        page3 = self.client.get("/results.html?page=3").get_data(as_text=True)
        self.assertIn("#1<", page3)
        self.assertIn("Practice – Batch 1 (Easy)", page3)               # the very first attempt
        topic_tests = self.client.get("/results.html?type=topic_test").get_data(as_text=True)
        self.assertEqual(topic_tests.count('class="result-item"'), 6)
        self.assertIn("20%", topic_tests)        # the first topic 1 attempt is still there
        attempt_ids = [int(x) for x in re.findall(r'results.html\?attempt=(\d+)', topic_tests)]
        self.assertEqual(attempt_ids, sorted(attempt_ids, reverse=True))
        old = self.client.get("/results.html?attempt=%d" % attempt_ids[-1]).get_data(as_text=True)
        self.assertIn("2 / 10", old)
        self.assertIn("Answers and Explanations", old)

        # 11. Profile and settings
        page = self.client.post("/profile.html", data={"full_name": "Priya K", "email": "priya.k@example.com"},
                                follow_redirects=True).get_data(as_text=True)
        self.assertIn("Your profile has been updated", page)
        self.assertIn("priya.k@example.com", page)
        page = self.client.post("/settings.html", data={"current_password": "wrong", "new_password": "newpass1",
                                                        "confirm_password": "newpass1"}).get_data(as_text=True)
        self.assertIn("current password is incorrect", page)
        page = self.client.post("/settings.html", data={"current_password": "secret123", "new_password": "newpass1",
                                                        "confirm_password": "newpass1"},
                                follow_redirects=True).get_data(as_text=True)
        self.assertIn("Your password has been changed", page)

        # 12. Logout and login again with the new details
        self.client.get("/logout")
        self.assertEqual(self.client.get("/dashboard.html").status_code, 302)
        self.assertIn("Incorrect email or password", self.login("priya.k@example.com").get_data(as_text=True))
        page = self.login("priya.k@example.com", "newpass1").get_data(as_text=True)
        self.assertIn("Welcome back, Priya", page)
        priya_attempt = re.search(r'results.html\?attempt=(\d+)', page).group(1)
        self.client.get("/logout")

        # 13. A Computer Science student sees only their own data
        page = self.signup("Arun Raj", "arun@example.com", "computer_science").get_data(as_text=True)
        self.assertIn("Computer Science", page)
        self.assertIn("You have not attempted anything yet", page)
        self.assertEqual(self.client.get("/results.html?attempt=" + priya_attempt).status_code, 404)
        self.assertEqual(self.client.get("/mistakes.html?attempt=" + priya_attempt).status_code, 404)
        self.assertIn("No results yet", self.client.get("/results.html").get_data(as_text=True))
        # Science subject is not part of the CS stream
        self.assertEqual(self.client.get("/practice.html?subject=science&topic=1").status_code, 404)
        page = self.client.get("/practice.html?subject=computer_science&topic=2").get_data(as_text=True)
        self.assertIn("Python Basics", page)
        self.submit("practice", "computer_science", "2", page, batch=1)
        page = self.client.get("/progress.html").get_data(as_text=True)
        self.assertIn("1 attempt", page)
        self.assertNotIn("Final Subject Test", page)
        page = self.client.get("/results.html").get_data(as_text=True)
        self.assertIn("All results <span>1</span>", page)
        self.assertIn("Python Basics", page)
        self.assertNotIn("Sets, Relations and Functions", page)

    def test_every_page_loads(self):
        self.signup("Page Tester", "pages@example.com", "computer_science")
        self.practise_all_batches("english", "3")
        for page in ("index.html", "dashboard.html", "profile.html", "take-test.html", "results.html",
                     "results.html?type=practice", "results.html?page=99", "recommendations.html",
                     "practice.html", "practice.html?subject=english&topic=3&batch=3", "progress.html",
                     "mistakes.html", "settings.html", "online-test.html?kind=topic_test&subject=english&topic=3"):
            response = self.client.get("/" + page)
            self.assertEqual(response.status_code, 200, page)
            self.assertNotIn("Gemini", response.get_data(as_text=True))
        self.client.get("/logout")
        for page in ("login.html", "signup.html", "index.html"):
            self.assertEqual(self.client.get("/" + page).status_code, 200, page)
        response = self.client.get("/css/style.css")
        self.assertEqual(response.status_code, 200)
        response.close()

    def test_signup_validation_and_login_required(self):
        self.assertEqual(self.client.get("/progress.html").status_code, 302)
        self.assertEqual(self.client.get("/results.html").status_code, 302)
        self.signup("Dup User", "dup@example.com", "science")
        self.client.get("/logout")
        page = self.signup("Dup User", "dup@example.com", "science").get_data(as_text=True)
        self.assertIn("already exists", page)
        page = self.signup("No Stream", "nostream@example.com", "arts").get_data(as_text=True)
        self.assertIn("choose your stream", page)

    def test_tampered_submission_rejected(self):
        self.signup("Tamper Test", "tamper@example.com", "science")
        # Topic test cannot be submitted before the practice batches are done
        response = self.client.post("/results.html", data={
            "kind": "topic_test", "subject": "mathematics", "topic": "1",
            "question_ids": ",".join(str(i) for i in range(1, 11))})
        self.assertEqual(response.status_code, 403)
        # Batch 2 cannot be submitted before batch 1
        page = self.client.get("/practice.html?subject=mathematics&topic=1").get_data(as_text=True)
        batch1 = self.question_ids(page)
        response = self.client.post("/results.html", data={
            "kind": "practice", "subject": "mathematics", "topic": "1", "batch": "2",
            "question_ids": ",".join(map(str, batch1))})
        self.assertEqual(response.status_code, 403)
        # Batch 1 with questions from another topic is rejected
        response = self.client.post("/results.html", data={
            "kind": "practice", "subject": "mathematics", "topic": "1", "batch": "1",
            "question_ids": ",".join(str(i) for i in range(11, 21))})
        self.assertEqual(response.status_code, 400)
        # Final test cannot be submitted while locked
        response = self.client.post("/results.html", data={
            "kind": "final_test", "subject": "mathematics", "topic": "",
            "question_ids": ",".join(str(i) for i in range(1, 16))})
        self.assertEqual(response.status_code, 403)
        # Nothing was saved by the rejected submissions
        self.assertIn("No results yet", self.client.get("/results.html").get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
