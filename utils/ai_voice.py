import asyncio
import logging
import os
import re
import uuid
import whisper
from gtts import gTTS

import edge_tts

logger = logging.getLogger(__name__)

model = whisper.load_model("base")

_INVISIBLE_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060\ufeff\u00ad]")

DEFAULT_EDGE_VOICES = {
    "en": "en-US-AriaNeural",
    "es": "es-ES-ElviraNeural",
    "fr": "fr-FR-DeniseNeural",
    "pt": "pt-BR-FranciscaNeural",
    "it": "it-IT-ElsaNeural",
}

# Preset "Starke+Edson": vozes neurais masculinas alinhadas por idioma (TTS Edge).
# Coloque Starke+Edson.mp3 em voices/ apenas como referência; a tradução usa síntese abaixo.
STARKE_EDSON_VOICE_ID = "custom:starke-edson"
STARKE_EDSON_EDGE_VOICES = {
    "en": "en-US-GuyNeural",
    "es": "es-ES-AlvaroNeural",
    "fr": "fr-FR-HenriNeural",
    "pt": "pt-BR-AntonioNeural",
    "it": "it-IT-DiegoNeural",
}

# Preset "Starke+voz+02": segunda voz personalizada (TTS Edge).
# Coloque Starke+voz+02.mp3 em voices/ apenas como referência; a tradução usa síntese abaixo.
STARKE_VOZ_02_VOICE_ID = "custom:starke-voz-02"
STARKE_VOZ_02_EDGE_VOICES = {
    "en": "en-US-SteffanNeural",
    "es": "es-ES-AlvaroNeural",
    "fr": "fr-FR-RemyMultilingualNeural",
    "pt": "pt-BR-AntonioNeural",
    "it": "it-IT-GiuseppeMultilingualNeural",
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
        _tts_voice_catalog = _run_edge_async(edge_tts.list_voices())
    return _tts_voice_catalog


def pick_edge_voice(target_lang, user_voice_id):
    """
    Escolhe o ShortName Edge-TTS.
    Se user_voice_id for valido e o locale bater com target_lang (en/es/fr/pt/it), usa-o.
    Caso contrário, voz neural padrão para aquele idioma.
    """
    lang = (target_lang or "en").lower().split("-")[0]
    user_voice_id = (user_voice_id or "").strip()
    if user_voice_id == STARKE_EDSON_VOICE_ID:
        return STARKE_EDSON_EDGE_VOICES.get(lang, STARKE_EDSON_EDGE_VOICES["en"])
    if user_voice_id == STARKE_VOZ_02_VOICE_ID:
        return STARKE_VOZ_02_EDGE_VOICES.get(lang, STARKE_VOZ_02_EDGE_VOICES["en"])
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


def format_edge_tts_modulation(rate_pct=0, pitch_hz=0, volume_pct=0):
    """Converte sliders numéricos para strings Edge-TTS (rate %, pitch Hz, volume %)."""
    r = int(round(max(-50.0, min(100.0, float(rate_pct)))))
    p = int(round(max(-50.0, min(50.0, float(pitch_hz)))))
    v = int(round(max(-50.0, min(50.0, float(volume_pct)))))
    return (f"{r:+d}%", f"{p:+d}Hz", f"{v:+d}%")


async def _edge_tts_save(text, voice_id, path, rate="+0%", pitch="+0Hz", volume="+0%"):
    com = edge_tts.Communicate(text, voice_id, rate=rate, pitch=pitch, volume=volume)
    await com.save(path)


def _run_edge_async(coro):
    """
    Executa corrotina Edge-TTS fora de um loop ja ativo (threads do Socket.IO / Windows).
    asyncio.run() pode falhar em alguns contextos de worker; loop novo e mais estavel.
    """
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            pass
        loop.close()
        asyncio.set_event_loop(None)


def text_to_speech(text, lang, tts_voice=None, rate="+0%", pitch="+0Hz", volume="+0%"):
    text = clean_spoken_text(text)
    if not text:
        return None

    voice_id = pick_edge_voice(lang, tts_voice)
    path = f"response_{lang}_{uuid.uuid4().hex[:12]}.mp3"

    def try_edge(r, p, v):
        _run_edge_async(_edge_tts_save(text, voice_id, path, rate=r, pitch=p, volume=v))
        return path

    try:
        return try_edge(rate, pitch, volume)
    except Exception as exc1:
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError:
            pass
        logger.warning("Edge-TTS com modulacao falhou: %s", exc1)
        if (rate, pitch, volume) != ("+0%", "+0Hz", "+0%"):
            try:
                return try_edge("+0%", "+0Hz", "+0%")
            except Exception as exc2:
                try:
                    if os.path.isfile(path):
                        os.remove(path)
                except OSError:
                    pass
                logger.warning("Edge-TTS sem modulacao falhou: %s", exc2)
        else:
            logger.warning("Edge-TTS falhou: %s", exc1)

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
    out = [
        {
            "id": STARKE_EDSON_VOICE_ID,
            "label": "Starke+Edson",
            "locale": "Custom",
            "gender": "Preset",
        }
        ,
        {
            "id": STARKE_VOZ_02_VOICE_ID,
            "label": "Starke+voz+02",
            "locale": "Custom",
            "gender": "Preset",
        }
    ]
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
        logger.exception("Falha ao listar vozes Edge; mantem preset Starke+Edson.")
        return out
    out.sort(key=lambda x: (x["locale"], x["gender"], x["label"]))
    return out
