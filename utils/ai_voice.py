import asyncio
import re
import uuid
import whisper
from gtts import gTTS

import edge_tts

model = whisper.load_model("base")

_INVISIBLE_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060\ufeff\u00ad]")

DEFAULT_EDGE_VOICES = {
    "en": "en-US-AriaNeural",
    "es": "es-ES-ElviraNeural",
    "fr": "fr-FR-DeniseNeural",
    "pt": "pt-BR-FranciscaNeural",
    "it": "it-IT-ElsaNeural",
}

_tts_voice_catalog = None


def clean_spoken_text(text):
    """Normaliza texto de STT/tradução para checagem e TTS."""
    if text is None:
        return ""
    text = str(text)
    text = _INVISIBLE_RE.sub("", text)
    text = " ".join(text.split())
    return text.strip()


def speech_to_text(audio_path):
    result = model.transcribe(audio_path)
    return result["text"]


def _load_voice_catalog():
    global _tts_voice_catalog
    if _tts_voice_catalog is None:
        _tts_voice_catalog = asyncio.run(edge_tts.list_voices())
    return _tts_voice_catalog


def pick_edge_voice(target_lang, user_voice_id):
    """
    Escolhe o ShortName Edge-TTS.
    Se user_voice_id for valido e o locale bater com target_lang (en/es/fr/pt/it), usa-o.
    Caso contrário, voz neural padrão para aquele idioma.
    """
    lang = (target_lang or "en").lower().split("-")[0]
    user_voice_id = (user_voice_id or "").strip()
    if user_voice_id:
        for v in _load_voice_catalog():
            if v.get("ShortName") != user_voice_id:
                continue
            loc = (v.get("Locale") or "").strip()
            loc_lang = loc.split("-")[0].lower() if loc else ""
            if loc_lang == lang:
                return user_voice_id
            return DEFAULT_EDGE_VOICES.get(lang, DEFAULT_EDGE_VOICES["en"])
    return DEFAULT_EDGE_VOICES.get(lang, DEFAULT_EDGE_VOICES["en"])


async def _edge_tts_save(text, voice_id, path):
    com = edge_tts.Communicate(text, voice_id)
    await com.save(path)


def text_to_speech(text, lang, tts_voice=None):
    text = clean_spoken_text(text)
    if not text:
        return None

    voice_id = pick_edge_voice(lang, tts_voice)
    path = f"response_{lang}_{uuid.uuid4().hex[:12]}.mp3"

    try:
        asyncio.run(_edge_tts_save(text, voice_id, path))
        return path
    except Exception:
        pass

    path_gt = f"response_{lang}_{uuid.uuid4().hex[:12]}_gt.mp3"
    try:
        tts = gTTS(text=text, lang=lang)
        tts.save(path_gt)
        return path_gt
    except Exception:
        return None


def list_neural_voices_for_ui():
    """Vozes neurais Edge para EN/ES/FR/PT/IT (lista para o front)."""
    allowed = frozenset({"en", "es", "fr", "pt", "it"})
    out = []
    try:
        for v in _load_voice_catalog():
            sn = v.get("ShortName") or ""
            if "Neural" not in sn:
                continue
            loc = (v.get("Locale") or "").strip()
            loc_lang = loc.split("-")[0].lower() if loc else ""
            if loc_lang not in allowed:
                continue
            out.append(
                {
                    "id": sn,
                    "label": (v.get("FriendlyName") or v.get("Name") or sn).strip(),
                    "locale": loc,
                    "gender": (v.get("Gender") or "").strip(),
                }
            )
    except Exception:
        return []
    out.sort(key=lambda x: (x["locale"], x["gender"], x["label"]))
    return out
