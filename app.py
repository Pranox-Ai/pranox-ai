from flask import Flask, render_template, request, redirect, url_for, session, jsonify, send_file, send_from_directory, Response, stream_with_context
import os
from dotenv import load_dotenv
from auth import oauth, init_oauth
from groq import Groq
from io import BytesIO
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet
import sqlite3
import requests
import re
import json
import time

# FILE SUPPORT
import pdfplumber
from PIL import Image
import pytesseract

load_dotenv()
print("CLIENT ID:", os.getenv("GOOGLE_CLIENT_ID"))
print("CLIENT SECRET:", os.getenv("GOOGLE_CLIENT_SECRET"))
app = Flask(__name__)
app.config.update(
    SESSION_COOKIE_SAMESITE="Lax",   # or "None" if using HTTPS
    SESSION_COOKIE_SECURE=False      # True only for HTTPS
)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret-key")

init_oauth(app)
client = Groq(api_key=os.getenv("GROQ_API_KEY"))
CLOUDFLARE_API_TOKEN  = os.getenv("CLOUDFLARE_API_TOKEN")
CLOUDFLARE_ACCOUNT_ID = os.getenv("CLOUDFLARE_ACCOUNT_ID")

# ═══════════════════════════════════════════
#  DATABASE
# ═══════════════════════════════════════════
def get_db():
    conn = sqlite3.connect("pranox.db", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    db = get_db()
    db.execute("""
        CREATE TABLE IF NOT EXISTS chats(
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_email  TEXT,
            role        TEXT,
            message     TEXT,
            created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS user_memory(
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_email  TEXT,
            key         TEXT,
            value       TEXT
        )
    """)
    db.commit()
    db.close()

init_db()

# ═══════════════════════════════════════════
#  MEMORY
# ═══════════════════════════════════════════
def save_memory(user_email, key, value):
    db = get_db()
    db.execute("DELETE FROM user_memory WHERE user_email=? AND key=?", (user_email, key))
    db.execute("INSERT INTO user_memory(user_email,key,value) VALUES (?,?,?)", (user_email, key, value))
    db.commit()
    db.close()

def get_memory(user_email):
    db = get_db()
    rows = db.execute(
        "SELECT key,value FROM user_memory WHERE user_email=?", (user_email,)
    ).fetchall()
    db.close()
    return "\n".join([f"{r['key']}: {r['value']}" for r in rows])

# ═══════════════════════════════════════════
#  INTERNET SEARCH
# ═══════════════════════════════════════════
def search_internet(query):
    try:
        api_key = os.getenv("SERPER_API_KEY")
        if not api_key:
            return ""
        res = requests.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            json={"q": query, "num": 5},
            timeout=5
        )
        data = res.json()
        results = []
        if "organic" in data:
            for r in data["organic"][:5]:
                snippet = r.get("snippet", "")
                results.append(f"{r['title']}: {r['link']}\n  {snippet}")
        return "\n\n".join(results)
    except Exception:
        return ""

# ═══════════════════════════════════════════
#  UTILS
# ═══════════════════════════════════════════
def safe_trim(text, limit=6000):
    return text[:limit] if len(text) > limit else text

def clean_text(text):
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()

# ═══════════════════════════════════════════
#  AI RUNNER
# ═══════════════════════════════════════════
MODELS = [
    "llama-3.3-70b-versatile",   # best quality, try first
    "llama-3.1-8b-instant",       # fast fallback
]

def run_ai(messages, model_index=0, max_tokens=1200):
    for i in range(model_index, len(MODELS)):
        try:
            completion = client.chat.completions.create(
                model=MODELS[i],
                messages=messages,
                temperature=0.7,
                max_tokens=max_tokens,
            )
            return completion.choices[0].message.content.strip()
        except Exception as e:
            print(f"AI ERROR (model {MODELS[i]}):", e)
            if i == len(MODELS) - 1:
                return None
    return None

def is_bad_response(reply):
    if not reply:
        return True
    bad_patterns = [
        "here's the corrected code",
        "flask application",
        "example of how you could",
        "missing code",
    ]
    reply_lower = reply.lower()
    return any(p in reply_lower for p in bad_patterns)

# ═══════════════════════════════════════════
#  SYSTEM PROMPT BUILDER
# ═══════════════════════════════════════════
def build_system_prompt(memory: str) -> str:
    return f"""You are Pranox AI — a next-generation AI assistant, highly intelligent, friendly, and precise.

========================
🔹 IDENTITY
========================
Founder of Pranox AI:
Chetansaipranav R

Created in:
January 2026

If user asks:
- who created you
- who is your founder
- tell me about pranox

Always answer clearly:
"Chetansaipranav R is the founder of Pranox AI. He created it in January 2026."

========================
🔹 PRANOX LINKS (STRICT CONTROL)
========================

Instagram: https://www.instagram.com/pranoxgroups?utm_source=ig_web_button_share_sheet&igsh=ZDNlZDc0MzIxNw==

X: https://x.com/Pranoxgroups

Linkedin:
https://www.linkedin.com/in/chetansaipranav-r-a6b18333b?utm_source=share&utm_campaign=share_via&utm_content=profile&utm_medium=android_app


RULES:
- ONLY share this Instagram link and Linkedin link and X link when user asks specifically about:
  - Pranox AI
  - your social media
  - your official pages
  - your contact or links

- DO NOT share this link when:
  - user asks about other companies (ChatGPT, IPL, Google, etc.)
  - user asks general knowledge questions
  - user asks unrelated topics

- NEVER mix Pranox links with other topics

Examples:
❌ Wrong:
User: "official site of ChatGPT"
→ Do NOT include Pranox Instagram and Linkedin

✅ Correct:
User: "Pranox social media"
→ Share Instagram

========================
🔹 EMAIL RULE (STRICT)
========================
Email: pranoxoffical@gmail.com

- ONLY provide this email when user explicitly asks:
  - "your email"
  - "contact pranox"
  - "how to contact you"

- DO NOT include email in:
  - general answers
  - topic explanations (like IPL, tech, etc.)
  - unrelated queries

- Never add email automatically in any response
- Only share when user directly inquires about contact information

========================
🔹 THINKING (IMPORTANT)
========================
- Understand the question deeply
- Break into logical steps internally
- Do NOT show reasoning
- Give only final clean answer
- Give answer from analysing previous conversation, not just current question

========================
🔹 BEHAVIOR
========================
- Talk like ChatGPT
- Talk to user in a friendly way
- Friendly and natural
- Not robotic
- Smart and helpful

========================
🔹 GREETING RULE (IMPORTANT)
========================
- If user says greetings like:
  "hi","Hi","Hello", "hello", "hey", "hii", "good morning","good afternoon", "good evening"

Then:
- Respond with a friendly greeting
- If user's name exists → include it

Examples:
- "Hi! How can I help you today?"
- "Hello Pranav! What can I do for you?"
- "Hey there! Need any help?"

Rules:
- Never say bye for greetings
- Never give weird or unrelated responses
- Even if user repeats greetings, respond politely
- Keep it short and natural

========================
🔹 PERSONALIZATION (IMPORTANT)
========================
- If user's name is available in memory:
  Use it naturally in responses

Examples:
- "Hi Pranav! How can I help you today?"
- "Hey Pranav, what would you like to do?"

Rules:
- Do NOT overuse the name
- Use mainly in greetings or first line
- Keep it natural and friendly

- If user tells their name:
  Respond like:
  "Nice to meet you <name>!"

  ========================
🔹 LINKS (IMPORTANT)
========================
- For every informative answer:
  Provide 1-3 useful reference links

- Links must be:
  - Relevant
  - Helpful
  - From trusted sources

- Format:
  🔗 Useful links:
  - Title: URL

- Do NOT add links if not needed (like greetings)

========================
🔹 RELEVANCE RULE (CRITICAL)
========================
- Only include information that is directly related to the user's question
- Do NOT add extra links, emails, or promotions unless explicitly asked

========================
🔹 CONTEXT
========================
- Use previous conversation
- Maintain continuity
- Do not repeat same answer unnecessarily

========================
🔹 RESPONSE STYLE
========================
- Simple question → short answer
- Complex question → detailed explanation
- Always structured
- Use bullet points ONLY when helpful
- Always ask follow-up questions

========================
🔹 FORMAT (STRICT)
========================
1. Start with clear explanation
2. Use bullets when needed
3. Keep spacing clean
4. Avoid large messy paragraphs

========================
🔹 CODE RULES
========================
- Always give proper code blocks
- Clean indentation
- Separate explanation and code

========================
🔹 INTERNET USAGE
========================
- Use search results ONLY if relevant
- Prefer latest information when needed
- Ignore irrelevant search data

========================
🔹 FOLLOW-UP
========================
- Suggest 1-2 useful follow-up questions (only if relevant)

========================
🔹 IMPORTANT RULES
========================
- Never give messy output
- NEVER include Pranox links unless user explicitly asks about Pranox
- Never show error messages to user
- If you can't generate answer to the previous question, then don't break the flow and continue to answer the next question in the conversation. Always keep the conversation going.
- Keep answers clean and readable
- Just give the answer what user exactly wants in a detailed , structured and clear way
- Never mix Pranox-related links with unrelated topics
- Always keep links relevant to user query
- If user asks about latest news or information, browse the internet and give the response based on that.

========================
🔹 MEMORY
========================
{memory if memory else "No memory stored yet."}
"""

# ═══════════════════════════════════════════
#  ROUTES
# ═══════════════════════════════════════════
@app.route("/")
def landing():
    return render_template("landing.html")

@app.route("/login")
def login():
    redirect_uri = "http://127.0.0.1:8000/authorize"
    return oauth.google.authorize_redirect(redirect_uri)

@app.route("/authorize")
def authorize():
    try:
        token = oauth.google.authorize_access_token()

        user = oauth.google.get(
            "https://openidconnect.googleapis.com/v1/userinfo"
        ).json()

        session["user"] = user

        return redirect("/dashboard")

    except Exception as e:
        print("GOOGLE LOGIN ERROR:", e)
        return "Login failed. Check console.", 500

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")

@app.route("/dashboard")
def dashboard():
    return render_template("dashboard.html", user=session.get("user"))

@app.route("/chat")
def chat():
    return render_template("chat.html")

# ═══════════════════════════════════════════
#  EMAIL
# ═══════════════════════════════════════════
@app.route("/email", methods=["GET", "POST"])
def email():
    if "user" not in session:
        return render_template("login_required.html")
    output = ""
    if request.method == "POST":
        topic = request.form.get("topic", "")
        tone  = request.form.get("tone", "professional")
        output = run_ai([
            {"role": "system", "content": "Write a professional email with clear paragraphs. No markdown."},
            {"role": "user",   "content": f"Write a {tone} email about: {topic}"}
        ]) or "Couldn't generate email. Please try again."
        output = clean_text(re.sub(r"[*#_`]", "", output))
    return render_template("email.html", email=output)

# ═══════════════════════════════════════════
#  RESUME
# ═══════════════════════════════════════════
@app.route("/resume", methods=["GET", "POST"])
def resume():
    if "user" not in session:
        return render_template("login_required.html")
    output = ""
    if request.method == "POST":
        name       = request.form.get("name", "").strip()
        role       = request.form.get("role", "").strip()
        skills     = request.form.get("skills", "").strip()
        experience = request.form.get("experience", "").strip()
        education  = request.form.get("education", "").strip()

        if not all([name, role, skills, experience, education]):
            return render_template("resume.html", resume="⚠️ Please fill all fields!")

        prompt = f"""Create a professional resume for:
Name: {name}
Target Role: {role}
Skills: {skills}
Work Experience: {experience}
Education: {education}

Format it cleanly with sections: Summary, Skills, Experience, Education."""

        output = run_ai([
            {"role": "system", "content": "You are an expert resume writer. Format cleanly. No markdown symbols."},
            {"role": "user",   "content": prompt}
        ]) or "Couldn't generate resume. Please try again."
        output = clean_text(re.sub(r"[*#_`]", "", output))
    return render_template("resume.html", resume=output)

# ═══════════════════════════════════════════
#  MAIN CHAT API
# ═══════════════════════════════════════════
@app.route("/api/chat", methods=["POST"])
def api_chat():
    data         = request.get_json(force=True)
    user_message = safe_trim(data.get("message", "").strip())

    if not user_message:
        return jsonify({"reply": "Please send a message."}), 400

    user_email = session["user"]["email"] if "user" in session else "guest"
    db = get_db()

    try:
        # Store user message
        db.execute(
            "INSERT INTO chats(user_email,role,message) VALUES (?,?,?)",
            (user_email, "user", user_message)
        )
        db.commit()

        # Memory ops
        memory = get_memory(user_email)
        name_match = re.search(r"my name is ([a-zA-Z ]+)", user_message, re.IGNORECASE)
        if name_match:
            save_memory(user_email, "name", name_match.group(1).strip().title())
            memory = get_memory(user_email)

        # Extract other quick facts
        age_match = re.search(r"i(?:'m| am) (\d+) years? old", user_message, re.IGNORECASE)
        if age_match:
            save_memory(user_email, "age", age_match.group(1))
            memory = get_memory(user_email)

        # History (last 20 turns)
        history = db.execute(
            "SELECT role,message FROM chats WHERE user_email=? ORDER BY id DESC LIMIT 20",
            (user_email,)
        ).fetchall()
        history = [h for h in history if "couldn't fully process" not in h["message"].lower()]

        # Internet search
        needs_search = any(kw in user_message.lower() for kw in [
            "latest", "news", "today", "current", "2024", "2025", "2026",
            "price", "weather", "stock", "who won", "what happened"
        ])
        search_results = search_internet(user_message) if needs_search else ""

        # Build message list
        msgs = [{"role": "system", "content": build_system_prompt(memory)}]

        for h in reversed(history):
            role = h["role"] if h["role"] in ("user", "assistant") else "user"
            msgs.append({"role": role, "content": h["message"]})

        msgs.append({"role": "user", "content": user_message})

        if search_results:
            msgs.append({
                "role": "system",
                "content": f"[Live web search results for context — use if relevant]\n{search_results}"
            })

        # Generate reply
        reply = run_ai(msgs)

        if not reply:
            reply = "I ran into an issue generating a response. Please try again."

        if is_bad_response(reply):
            links = search_internet(user_message)
            reply = (
                f"Here are some helpful resources on that:\n\n🔗 {links}"
                if links else
                "I couldn't process that fully. Could you rephrase your question?"
            )

        reply = re.sub(r"\n{3,}", "\n\n", reply).strip()

        # Add guest nudge once per session
        if "user" not in session and "login_nudge" not in session:
            reply += "\n\n💡 *[Login to save your chat history and get personalized responses]*"
            session["login_nudge"] = True

        # Store AI reply
        if "couldn't fully process" not in reply.lower():
            db.execute(
                "INSERT INTO chats(user_email,role,message) VALUES (?,?,?)",
                (user_email, "assistant", reply)
            )
            db.commit()

        return jsonify({"reply": reply})

    except Exception as e:
        print("CHAT ERROR:", e)
        return jsonify({"reply": "An unexpected error occurred. Please try again."}), 500

    finally:
        db.close()

# ═══════════════════════════════════════════
#  FILE UPLOAD
# ═══════════════════════════════════════════
@app.route("/api/upload", methods=["POST"])
def upload():
    if "file" not in request.files:
        return jsonify({"reply": "No file received. Please try again."})

    file     = request.files["file"]
    filename = file.filename.lower()
    text     = ""

    try:
        if filename.endswith(".pdf"):
            with pdfplumber.open(file) as pdf:
                for page in pdf.pages:
                    text += (page.extract_text() or "") + "\n"
        elif filename.endswith((".png", ".jpg", ".jpeg", ".webp")):
            img  = Image.open(file)
            text = pytesseract.image_to_string(img)
        elif filename.endswith(".txt"):
            text = file.read().decode("utf-8", errors="ignore")
        else:
            return jsonify({"reply": "Unsupported file type. Please upload PDF, image (PNG/JPG), or TXT."})

        if not text.strip():
            return jsonify({"reply": "Could not extract text from this file. Please try a different file."})

        text  = safe_trim(text, limit=5000)
        reply = run_ai([
            {
                "role": "system",
                "content": "Analyze the provided document. Give a clear summary, key points as bullet list, and any important details. Be thorough but concise."
            },
            {"role": "user", "content": f"Please analyze this document:\n\n{text}"}
        ], max_tokens=1500) or "Couldn't analyze the file. Please try again."

        return jsonify({"reply": reply})

    except Exception as e:
        print("FILE ERROR:", e)
        return jsonify({"reply": "File processing failed. Please try a different file."})

# ═══════════════════════════════════════════
#  PDF DOWNLOAD (Resume)
# ═══════════════════════════════════════════
@app.route("/download_resume", methods=["POST"])
def download_resume():
    buffer = BytesIO()
    p = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    y = height - 50

    p.setFont("Helvetica-Bold", 14)
    p.drawString(50, y, "Resume")
    y -= 30
    p.setFont("Helvetica", 11)

    for line in request.form["resume"].split("\n"):
        if y < 60:
            p.showPage()
            y = height - 50
            p.setFont("Helvetica", 11)
        line = line.strip()
        if line:
            p.drawString(50, y, line[:100])  # prevent overflow
            y -= 18
        else:
            y -= 8

    p.save()
    buffer.seek(0)
    return send_file(buffer, as_attachment=True, download_name="pranox_resume.pdf", mimetype="application/pdf")

# ═══════════════════════════════════════════
#  IMAGE GENERATION
# ═══════════════════════════════════════════
@app.route("/api/image", methods=["POST"])
def generate_image():
    data   = request.get_json(force=True)
    prompt = data.get("prompt", "").strip()

    if not prompt:
        return jsonify({"reply": "⚠️ Please provide an image description."}), 400

    if not CLOUDFLARE_API_TOKEN or not CLOUDFLARE_ACCOUNT_ID:
        return jsonify({"reply": "⚠️ Image generation is not configured on this server."}), 500

    # Enhance the prompt for better quality
    enhanced = f"ultra realistic, 4k, highly detailed, sharp focus, professional photography, {prompt}"

    url = (
        f"https://api.cloudflare.com/client/v4/accounts/"
        f"{CLOUDFLARE_ACCOUNT_ID}/ai/run/"
        f"@cf/stabilityai/stable-diffusion-xl-base-1.0"
    )

    try:
        response = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {CLOUDFLARE_API_TOKEN}",
                "Content-Type":  "application/json"
            },
            json={"prompt": enhanced, "num_steps": 20},
            timeout=45
        )

        if response.status_code == 200:
            return response.content, 200, {
                "Content-Type": "image/png",
                "Cache-Control": "no-cache"
            }
        else:
            print("CF ERROR:", response.status_code, response.text[:200])
            return jsonify({"reply": "Image generation failed. Please try a different description."}), 500

    except requests.Timeout:
        return jsonify({"reply": "Image generation timed out. Please try again."}), 504
    except Exception as e:
        print("IMAGE ERROR:", e)
        return jsonify({"reply": "Server error during image generation."}), 500

# ═══════════════════════════════════════════
#  STATIC PAGES
# ═══════════════════════════════════════════
@app.route("/privacy")
def privacy():
    return render_template("privacy.html")

@app.route("/terms")
def terms():
    return render_template("terms.html")

@app.route("/sitemap.xml")
def sitemap():
    return send_from_directory("static", "sitemap.xml")

@app.route("/robots.txt")
def robots():
    return send_from_directory("static", "robots.txt")

# ═══════════════════════════════════════════
#  RUN
# ═══════════════════════════════════════════
if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=8000)
