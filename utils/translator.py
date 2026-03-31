from deep_translator import GoogleTranslator


def translate_text(text, target):
    try:
        return GoogleTranslator(source="auto", target=target).translate(text)
    except Exception:
        return text
