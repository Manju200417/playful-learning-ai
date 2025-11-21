import os
import sqlite3
from datetime import datetime
from flask import Flask, request, jsonify, g, render_template
from werkzeug.utils import secure_filename
import requests

# -------------------------
# CONFIG
# -------------------------
app = Flask(__name__, template_folder="templates", static_folder="static")
DB_PATH = "resources.db"


GROQ_API_KEY = "Your Groq API Key Here"


# -------------------------
# DB HELPERS
# -------------------------
def get_db():
    db = getattr(g, "_database", None)
    if db is None:
        db = g._database = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row
    return db


def init_tables():
    db = get_db()
    cur = db.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS resources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT,
            content TEXT,
            created_at TEXT
        )
    """)

    db.commit()


# Run DB init (Flask 3 safe)
with app.app_context():
    init_tables()


@app.teardown_appcontext
def close_db(exc):
    db = getattr(g, "_database", None)
    if db:
        db.close()


# -------------------------
# GROQ CHAT FUNCTION
# -------------------------
def groq_chat(messages, model="openai/gpt-oss-120b"):
    url = "https://api.groq.com/openai/v1/chat/completions"

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }

    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.7
    }

    r = requests.post(url, json=payload, headers=headers)
    data = r.json()

    try:
        return data["choices"][0]["message"]["content"]
    except:
        return f"[Groq Error] {data}"


# -------------------------
# FRONTEND ROUTE
# -------------------------
@app.route("/")
def home():
    return render_template("index.html")   # loads your full frontend


# -------------------------
# API: UPLOAD RESOURCE
# -------------------------
@app.route("/upload-resource", methods=["POST"])
def upload_resource():
    content = ""
    filename = ""

    if "file" in request.files and request.files["file"].filename:
        f = request.files["file"]
        filename = secure_filename(f.filename)

        if filename.endswith(".txt"):
            content = f.read().decode("utf-8", errors="ignore")

        elif filename.endswith(".pdf"):
            try:
                from PyPDF2 import PdfReader
                reader = PdfReader(f)
                text_parts = []
                for page in reader.pages:
                    txt = page.extract_text()
                    if txt:
                        text_parts.append(txt)
                content = "\n".join(text_parts)

                if not content.strip():
                    content = "PDF extraction returned empty text."

            except Exception as e:
                content = f"PDF extraction failed: {e}"

        else:
            return jsonify({"error": "Only PDF or TXT allowed"}), 400

    else:
        text = request.form.get("text") or (request.get_json() or {}).get("text")
        if not text:
            return jsonify({"error": "No text or file provided"}), 400
        filename = "input-text"
        content = text

    # store in DB
    db = get_db()
    cur = db.cursor()

    now = datetime.utcnow().isoformat()
    cur.execute(
        "INSERT INTO resources (filename, content, created_at) VALUES (?,?,?)",
        (filename, content, now)
    )
    db.commit()

    return jsonify({
        "resource_id": cur.lastrowid,
        "filename": filename,
        "length": len(content)
    })



# -------------------------
# API: QUIZ MODE
# -------------------------
@app.route("/generate-quiz", methods=["POST"])
def generate_quiz():
    data = request.get_json()
    rid = data.get("resource_id")

    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT content FROM resources WHERE id=?", (rid,))
    row = cur.fetchone()

    if not row:
        return jsonify({"error": "Resource not found"}), 404

    content = row["content"]

    prompt = f"""
You are an AI that generates quizzes. 
Return ONLY a JSON array. NO markdown. NO explanation. NO extra text.

Format example:
[
  {{
    "question": "What is ...?",
    "choices": ["A","B","C","D"],
    "answer": 0,
    "hint": "Explanation..."
  }}
]

Now generate 5 MCQ questions based ONLY on the text below.

TEXT:
{content[:3500]}

Remember:
- DO NOT add ```json
- DO NOT add comments
- DO NOT add text before or after the JSON
- ONLY return valid JSON
"""

    raw = groq_chat([{"role": "user", "content": prompt}], model="openai/gpt-oss-120b")

    # Clean any accidental markdown
    cleaned = raw.strip()
    cleaned = cleaned.replace("```json", "").replace("```", "").strip()

    return jsonify({"questions": cleaned})


# -------------------------
# STORY MODE
# -------------------------
@app.route("/generate-story-mode", methods=["POST"])
def story_mode():
    data = request.get_json()
    rid = data.get("resource_id")

    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT content FROM resources WHERE id=?", (rid,))
    row = cur.fetchone()

    content = row["content"]

    prompt = f"""
Convert this topic into a playful children's story that teaches the concept.

{content[:2000]}
"""

    story = groq_chat([{"role": "user", "content": prompt}])
    return jsonify({"story": story})


# -------------------------
# GAME MODE
# -------------------------
@app.route("/generate-game-mode", methods=["POST"])
def game_mode():
    data = request.get_json()
    rid = data.get("resource_id")

    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT content FROM resources WHERE id=?", (rid,))
    row = cur.fetchone()

    content = row["content"]

    prompt = f"""
Make a mini adventure game based on this content.

Return JSON:
{{
  "scenario": "...",
  "choices": [
    {{"text": "...", "result": "...", "xp": 5}},
    {{"text": "...", "result": "...", "mistake": true}},
    {{"text": "...", "result": "...", "badge": 1}}
  ]
}}
TEXT:
{content[:2000]}
"""

    game = groq_chat([{"role": "user", "content": prompt}])
    return jsonify({"game": game})


# -------------------------
# CHAT MODE (RAG)
# -------------------------
@app.route("/chat", methods=["POST"])
def chat_mode():
    data = request.get_json()
    rid = data.get("resource_id")
    question = data.get("question", "").strip()

    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT content FROM resources WHERE id=?", (rid,))
    row = cur.fetchone()

    content = row["content"]

    # ------------------------------
    # 1. If question is generic → explain whole document
    # ------------------------------
    generic_questions = ["explain", "what is this", "tell me", "teach me", "summary", "summarize"]
    if question.lower() in generic_questions or len(question.split()) <= 2:
        prompt = f"""
You are a teacher. Explain the following document in simple words so the user understands clearly.

DOCUMENT:
{content[:4000]}
"""
        answer = groq_chat([{"role": "user", "content": prompt}])
        return jsonify({"answer": answer})

    # ------------------------------
    # 2. Normal RAG-style question
    # ------------------------------
    prompt = f"""
Answer the user's question using the provided document. 
If answer is not found, explain which part of the document is relevant and give a helpful response anyway.

DOCUMENT:
{content[:4000]}

QUESTION:
{question}

Give a clear, helpful explanation.
"""

    answer = groq_chat([{"role": "user", "content": prompt}])
    return jsonify({"answer": answer})


# ---------------------------------------------------------
# RUN APP
# ---------------------------------------------------------
if __name__ == "__main__":
    app.run(debug=True)
