from flask import Flask, render_template, request, redirect, url_for, session, jsonify, send_file, send_from_directory
import os
from dotenv import load_dotenv
from authlib.integrations.flask_client import OAuth
from groq import Groq
from io import BytesIO
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
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

app = Flask(__name__)
app.config.update(
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("FLASK_ENV") == "production",
    SESSION_COOKIE_HTTPONLY=True,
)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret-key-change-me")

# ═══════════════════════════════════════════
#  OAUTH SETUP — works on localhost AND Render
# ═══════════════════════════════════════════
oauth = OAuth(app)
oauth.register(
    name="google",
    client_id=os.getenv("GOOGLE_CLIENT_ID"),
    client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={
        "scope": "openid email profile",
        "prompt": "select_account",
    },
)

client = Groq(api_key=os.getenv("GROQ_API_KEY"))
CLOUDFLARE_API_TOKEN  = os.getenv("CLOUDFLARE_API_TOKEN", "")
CLOUDFLARE_ACCOUNT_ID = os.getenv("CLOUDFLARE_ACCOUNT_ID", "")

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
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
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
#  SYSTEM PROMPT
# ═══════════════════════════════════════════
def build_system_prompt(memory: str) -> str:
    return f"""You are Pranox AI — a next-generation AI assistant, highly intelligent, friendly, and precise.

========================
IDENTITY
========================
Founder: Chetansaipranav R
Created: January 2026

If asked who created you or about the founder, always say:
"Chetansaipranav R is the founder of Pranox AI. He created me in January 2026."

========================
PRANOX LINKS (STRICT)
========================
Instagram: https://www.instagram.com/pranoxgroups
X: https://x.com/Pranoxgroups
LinkedIn: https://www.linkedin.com/in/chetansaipranav-r-a6b18333b

ONLY share these when user asks about Pranox AI social media or official pages.
NEVER mix Pranox links with other topics.

========================
EMAIL (STRICT)
========================
Email: pranoxoffical@gmail.com
ONLY provide when user explicitly asks for contact/email info.

========================
BEHAVIOR
========================
- Friendly, smart, helpful — like ChatGPT
- Use user's name from memory naturally in greetings
- Never robotic
- Keep answers clean and structured
- Use bullet points only when helpful
- For greetings: respond warmly and briefly

========================
RESPONSE FORMAT
========================
1. Clear explanation first
2. Bullets when needed
3. Clean spacing
4. Code in proper code blocks

========================
INTERNET USAGE
========================
Use search results only if relevant. Prefer latest info when needed.

========================
MEMORY
========================
{memory if memory else "No memory stored yet."}
"""

# ═══════════════════════════════════════════
#  HELPER: get the correct redirect URI
# ═══════════════════════════════════════════
def get_redirect_uri():
    """
    Automatically use HTTPS on Render, HTTP on localhost.
    Set RENDER_EXTERNAL_URL in your Render environment variables.
    """
    render_url = os.getenv("RENDER_EXTERNAL_URL")
    if render_url:
        # Render provides the full URL like https://myapp.onrender.com
        return f"{render_url.rstrip('/')}/authorize"
    # Local dev
    return "http://127.0.0.1:8000/authorize"

# ═══════════════════════════════════════════
#  ROUTES
# ═══════════════════════════════════════════
@app.route("/")
def landing():
    return render_template("landing.html")

@app.route("/login")
def login():
    redirect_uri = get_redirect_uri()
    print("REDIRECT URI:", redirect_uri)
    return oauth.google.authorize_redirect(redirect_uri)

@app.route("/authorize")
def authorize():
    try:
        token = oauth.google.authorize_access_token()
        user = oauth.google.userinfo()
        session["user"] = {
            "email": user.get("email"),
            "name": user.get("name"),
            "picture": user.get("picture"),
        }
        return redirect("/dashboard")
    except Exception as e:
        print("GOOGLE LOGIN ERROR:", e)
        return redirect("/?error=login_failed")

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")

@app.route("/dashboard")
def dashboard():
    if "user" not in session:
        return redirect("/login")
    return render_template("dashboard.html", user=session.get("user"))

@app.route("/chat")
def chat():
    return render_template("chat.html", user=session.get("user"))

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
            return render_template("resume.html", resume="Please fill all fields!")

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
        db.execute(
            "INSERT INTO chats(user_email,role,message) VALUES (?,?,?)",
            (user_email, "user", user_message)
        )
        db.commit()

        memory = get_memory(user_email)
        name_match = re.search(r"my name is ([a-zA-Z ]+)", user_message, re.IGNORECASE)
        if name_match:
            save_memory(user_email, "name", name_match.group(1).strip().title())
            memory = get_memory(user_email)

        age_match = re.search(r"i(?:'m| am) (\d+) years? old", user_message, re.IGNORECASE)
        if age_match:
            save_memory(user_email, "age", age_match.group(1))
            memory = get_memory(user_email)

        history = db.execute(
            "SELECT role,message FROM chats WHERE user_email=? ORDER BY id DESC LIMIT 20",
            (user_email,)
        ).fetchall()
        history = [h for h in history if "couldn't fully process" not in h["message"].lower()]

        needs_search = any(kw in user_message.lower() for kw in [
            "latest", "news", "today", "current", "2024", "2025", "2026",
            "price", "weather", "stock", "who won", "what happened"
        ])
        search_results = search_internet(user_message) if needs_search else ""

        msgs = [{"role": "system", "content": build_system_prompt(memory)}]

        for h in reversed(history):
            role = h["role"] if h["role"] in ("user", "assistant") else "user"
            msgs.append({"role": role, "content": h["message"]})

        msgs.append({"role": "user", "content": user_message})

        if search_results:
            msgs.append({
                "role": "system",
                "content": f"[Live web search results for context]\n{search_results}"
            })

        reply = run_ai(msgs)

        if not reply:
            reply = "I ran into an issue generating a response. Please try again."

        if is_bad_response(reply):
            links = search_internet(user_message)
            reply = (
                f"Here are some helpful resources:\n\n{links}"
                if links else
                "I couldn't process that. Could you rephrase your question?"
            )

        reply = re.sub(r"\n{3,}", "\n\n", reply).strip()

        if "user" not in session and "login_nudge" not in session:
            reply += "\n\n💡 *Login to save your chat history and get personalized responses*"
            session["login_nudge"] = True

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
#  FILE UPLOAD — enhanced file support
# ═══════════════════════════════════════════
@app.route("/api/upload", methods=["POST"])
def upload():
    if "file" not in request.files:
        return jsonify({"reply": "No file received. Please try again."})

    file     = request.files["file"]
    filename = file.filename.lower()
    text     = ""

    try:
        # PDF
        if filename.endswith(".pdf"):
            try:
                with pdfplumber.open(file) as pdf:
                    for page in pdf.pages:
                        extracted = page.extract_text()
                        if extracted:
                            text += extracted + "\n"
               
            except Exception as e:
                print("PDF error:", e)

        # Images — OCR
        elif filename.endswith((".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".gif")):
            try:
                img = Image.open(file)
                # Convert to RGB if needed
                if img.mode not in ("RGB", "L"):
                    img = img.convert("RGB")
                text = pytesseract.image_to_string(img)
            except Exception as e:
                print("Image OCR error:", e)

        # Plain text
        elif filename.endswith(".txt"):
            text = file.read().decode("utf-8", errors="ignore")

        # CSV
        elif filename.endswith(".csv"):
            text = file.read().decode("utf-8", errors="ignore")
            text = "CSV Data:\n" + text

        # JSON
        elif filename.endswith(".json"):
            raw = file.read().decode("utf-8", errors="ignore")
            try:
                parsed = json.loads(raw)
                text = "JSON Data:\n" + json.dumps(parsed, indent=2)
            except Exception:
                text = raw

        # Python / code files
        elif filename.endswith((".py", ".js", ".ts", ".html", ".css", ".java", ".cpp", ".c", ".md")):
            text = file.read().decode("utf-8", errors="ignore")

        else:
            return jsonify({"reply": "Unsupported file type. Supported: PDF, images (PNG/JPG/WEBP), TXT, CSV, JSON, and code files."})

        if not text.strip():
            return jsonify({"reply": "Could not extract text from this file. The file may be empty, password-protected, or in an unsupported format."})

        text  = safe_trim(text, limit=5000)
        user_msg = request.form.get("message", "").strip()

        prompt = f"Please analyze this document and provide:\n1. A clear summary\n2. Key points\n3. Any important details\n\nUser's question: {user_msg}\n\nDocument content:\n{text}" if user_msg else f"Please analyze this document:\n\n{text}"

        reply = run_ai([
            {
                "role": "system",
                "content": "Analyze the provided document. Give a clear summary, key points as bullet list, and any important details. Be thorough but concise."
            },
            {"role": "user", "content": prompt}
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
            p.drawString(50, y, line[:100])
            y -= 18
        else:
            y -= 8

    p.save()
    buffer.seek(0)
    return send_file(buffer, as_attachment=True, download_name="pranox_resume.pdf", mimetype="application/pdf")

# ═══════════════════════════════════════════
#  IMAGE GENERATION — Cloudflare Workers AI
# ═══════════════════════════════════════════
@app.route("/api/image", methods=["POST"])
def generate_image():
    data   = request.get_json(force=True)
    prompt = data.get("prompt", "").strip()

    if not prompt:
        return jsonify({"reply": "Please provide an image description."}), 400

    token     = CLOUDFLARE_API_TOKEN.strip()
    account   = CLOUDFLARE_ACCOUNT_ID.strip()

    if not token or not account:
        print("CF env missing — TOKEN:", bool(token), "ACCOUNT:", bool(account))
        return jsonify({"reply": "Image generation is not configured. Please set CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID in your environment variables."}), 500

    enhanced = f"ultra realistic, 4k, highly detailed, sharp focus, professional photography, {prompt}"

    url = (
        f"https://api.cloudflare.com/client/v4/accounts/"
        f"{account}/ai/run/"
        f"@cf/stabilityai/stable-diffusion-xl-base-1.0"
    )

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type":  "application/json",
    }

    print(f"CF image request → account={account[:6]}*** url={url}")

    try:
        response = requests.post(
            url,
            headers=headers,
            json={"prompt": enhanced, "num_steps": 20},
            timeout=60,
        )

        print("CF response status:", response.status_code)

        if response.status_code == 200:
            content_type = response.headers.get("Content-Type", "")
            # Cloudflare returns raw image bytes
            if "image" in content_type or len(response.content) > 1000:
                return response.content, 200, {
                    "Content-Type": "image/png",
                    "Cache-Control": "no-cache",
                }
            else:
                # Might be a JSON error response
                print("CF unexpected content:", response.text[:300])
                return jsonify({"reply": "Image generation returned unexpected data. Please try again."}), 500

        elif response.status_code == 401:
            print("CF 401 — invalid token")
            return jsonify({"reply": "Image generation auth failed. Please check your Cloudflare API token."}), 500

        elif response.status_code == 403:
            print("CF 403 — forbidden, check account ID or token permissions")
            return jsonify({"reply": "Image generation permission denied. Verify your Cloudflare account ID and token permissions."}), 500

        else:
            print("CF ERROR:", response.status_code, response.text[:300])
            return jsonify({"reply": f"Image generation failed (error {response.status_code}). Please try a different description."}), 500

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
    port = int(os.getenv("PORT", 8000))
    app.run(debug=False, host="0.0.0.0", port=port)
