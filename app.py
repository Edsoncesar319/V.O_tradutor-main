import base64
import os
from datetime import timedelta

from flask import Flask, jsonify, render_template, request, redirect, session, url_for
from flask_socketio import SocketIO, emit
import sqlite3

from functools import wraps
from werkzeug.security import check_password_hash, generate_password_hash

from utils.ai_voice import (
    clean_spoken_text,
    list_neural_voices_for_ui,
    speech_to_text,
    text_to_speech,
)
from utils.translator import translate_text

app = Flask(__name__)
app.config["SECRET_KEY"] = "secret"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=14)
# threading: evita eventlet, que quebra no Python 3.13 (ssl.wrap_socket removido).
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")


@app.after_request
def _cors_all(resp):
    """Permite abrir o front no Live Server (:5500) com API no Flask (:5000)."""
    resp.headers.setdefault("Access-Control-Allow-Origin", "*")
    resp.headers.setdefault("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    resp.headers.setdefault("Access-Control-Allow-Headers", "Content-Type")
    return resp

DB = "database/chat.db"

OUTPUT_LANGS = ("en", "es", "fr", "pt", "it")


def _resolve_output_langs(payload):
    """'all' -> todas as linguas configuradas; caso contrario uma chave em OUTPUT_LANGS."""
    if not payload:
        return list(OUTPUT_LANGS)
    v = str(payload.get("output_lang", "all")).strip().lower()
    if v in ("all", "*", ""):
        return list(OUTPUT_LANGS)
    if v in OUTPUT_LANGS:
        return [v]
    return list(OUTPUT_LANGS)


def _parse_tts_voice(payload):
    if not payload:
        return None
    vid = str(payload.get("tts_voice") or "").strip()
    return vid or None


def init_db():
    conn = sqlite3.connect(DB)
    cursor = conn.cursor()
    cursor.execute(
        """
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT,
        message TEXT
    )
    """
    )
    cursor.execute(
        """
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """
    )
    conn.commit()
    conn.close()


def _get_user_by_id(user_id):
    if not user_id:
        return None
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT id, username, password_hash FROM users WHERE id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def _get_user_by_username(username):
    if not username:
        return None
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT id, username, password_hash FROM users WHERE username = ?", (username,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def current_user():
    return _get_user_by_id(session.get("user_id"))


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login"))
        return fn(*args, **kwargs)

    return wrapper


def _broadcast_translation(username, text, output_langs=None, tts_voice=None):
    """text já limpo e não vazio: traduz, TTS, grava DB e envia a todos."""
    languages = output_langs if output_langs else list(OUTPUT_LANGS)
    translations = {}
    audio_responses = {}

    for lang in languages:
        translated = translate_text(text, lang)
        translations[lang] = translated
        audio_file = text_to_speech(translated, lang, tts_voice)
        if not audio_file:
            continue
        with open(audio_file, "rb") as f:
            audio_responses[lang] = base64.b64encode(f.read()).decode("utf-8")
        try:
            os.remove(audio_file)
        except OSError:
            pass

    conn = sqlite3.connect(DB)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO messages (username, message) VALUES (?, ?)",
        (username, text),
    )
    conn.commit()
    conn.close()

    emit(
        "receive_translation",
        {
            "username": username,
            "original": text,
            "translations": translations,
            "audio": audio_responses,
            "output_langs": languages,
        },
        broadcast=True,
    )


@app.route("/")
def index():
    if not session.get("user_id"):
        return redirect(url_for("login"))
    return render_template("index.html", username=session.get("username", ""))


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("user_id"):
        return redirect(url_for("index"))

    error = ""
    username = (request.args.get("username") or "").strip()
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        remember_me = (request.form.get("remember_me") or "") == "on"

        user = _get_user_by_username(username)
        if not user or not check_password_hash(user["password_hash"], password):
            error = "Usuário ou senha inválidos."
        else:
            session.permanent = remember_me
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            return redirect(url_for("index"))

    return render_template("login.html", error=error, username=username)


@app.route("/register", methods=["GET", "POST"])
def register():
    if session.get("user_id"):
        return redirect(url_for("index"))

    error = ""
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""

        if len(username) < 3:
            error = "Usuário precisa ter pelo menos 3 caracteres."
        elif len(password) < 6:
            error = "Senha precisa ter pelo menos 6 caracteres."
        elif _get_user_by_username(username):
            error = "Esse usuário já existe."
        else:
            conn = sqlite3.connect(DB)
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO users (username, password_hash) VALUES (?, ?)",
                (username, generate_password_hash(password)),
            )
            conn.commit()
            conn.close()
            return redirect(url_for("login", username=username))

    return render_template("register.html", error=error)


@app.route("/logout", methods=["GET", "POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/account", methods=["GET", "POST"])
@login_required
def account():
    error = ""
    user = current_user()
    if not user:
        session.clear()
        return redirect(url_for("login"))

    if request.method == "POST":
        action = (request.form.get("_action") or "").strip()

        if action == "delete":
            # confere senha antes de apagar (reduz risco)
            password = request.form.get("password") or ""
            if not check_password_hash(user["password_hash"], password):
                error = "Senha incorreta."
            else:
                conn = sqlite3.connect(DB)
                cur = conn.cursor()
                cur.execute("DELETE FROM users WHERE id = ?", (user["id"],))
                conn.commit()
                conn.close()
                session.clear()
                return redirect(url_for("register"))

        else:
            # update
            new_username = (request.form.get("username") or "").strip()
            new_password = request.form.get("password") or ""

            if not new_username or len(new_username) < 3:
                error = "Novo usuário precisa ter pelo menos 3 caracteres."
            else:
                existing = _get_user_by_username(new_username)
                if existing and existing["id"] != user["id"]:
                    error = "Esse usuário já existe."
                else:
                    conn = sqlite3.connect(DB)
                    cur = conn.cursor()
                    if new_password:
                        if len(new_password) < 6:
                            error = "Senha precisa ter pelo menos 6 caracteres."
                        else:
                            cur.execute(
                                "UPDATE users SET username = ?, password_hash = ? WHERE id = ?",
                                (new_username, generate_password_hash(new_password), user["id"]),
                            )
                            conn.commit()
                            conn.close()
                            session["username"] = new_username
                            return redirect(url_for("account"))
                    else:
                        cur.execute(
                            "UPDATE users SET username = ? WHERE id = ?",
                            (new_username, user["id"]),
                        )
                        conn.commit()
                        conn.close()
                        session["username"] = new_username
                        return redirect(url_for("account"))

    return render_template("account.html", error=error, username=user["username"])


@app.route("/api/tts-voices")
def api_tts_voices():
    """Vozes neurais Edge-TTS (EN/ES/FR/PT/IT) para o seletor do front."""
    return jsonify({"voices": list_neural_voices_for_ui()})


@socketio.on("text_message")
def handle_text_message(data):
    user = current_user()
    if not user:
        emit(
            "transcription",
            {"text": "", "error": "Faça login para usar o app."},
            to=request.sid,
        )
        return

    username = user["username"]
    text = clean_spoken_text(data.get("text", ""))

    if not text:
        emit(
            "transcription",
            {"text": "", "error": "Digite uma mensagem antes de enviar."},
            to=request.sid,
        )
        return

    emit(
        "transcription",
        {"text": text},
        to=request.sid,
    )

    try:
        _broadcast_translation(
            username,
            text,
            _resolve_output_langs(data),
            _parse_tts_voice(data),
        )
    except Exception as exc:
        emit(
            "transcription",
            {"text": "", "error": str(exc)},
            to=request.sid,
        )


@socketio.on("voice_message")
def handle_voice(data):
    user = current_user()
    if not user:
        emit(
            "transcription",
            {"text": "", "error": "Faça login para usar o app."},
            to=request.sid,
        )
        return

    username = user["username"]
    audio_base64 = data["audio"]
    audio_bytes = base64.b64decode(audio_base64)

    # Browser MediaRecorder typically emits WebM/Opus; Whisper accepts this format.
    audio_path = "temp_audio.webm"
    with open(audio_path, "wb") as f:
        f.write(audio_bytes)

    try:
        try:
            text = clean_spoken_text(speech_to_text(audio_path))

            emit(
                "transcription",
                {"text": text},
                to=request.sid,
            )

            out_langs = _resolve_output_langs(data)
            translations = {lang: "" for lang in out_langs}

            if not text:
                emit(
                    "receive_translation",
                    {
                        "username": username,
                        "original": "",
                        "translations": translations,
                        "audio": {},
                        "output_langs": out_langs,
                    },
                    broadcast=True,
                )
                return

            _broadcast_translation(username, text, out_langs, _parse_tts_voice(data))
        except Exception as exc:
            emit(
                "transcription",
                {"text": "", "error": str(exc)},
                to=request.sid,
            )
    finally:
        try:
            os.remove(audio_path)
        except OSError:
            pass


if __name__ == "__main__":
    os.makedirs("database", exist_ok=True)
    init_db()
    socketio.run(app, host="0.0.0.0", port=5000, debug=True)
