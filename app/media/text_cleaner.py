import re

# Basic emoji removal (covers most emoji ranges)
_EMOJI_RE = re.compile(
    "["
    "\U0001F600-\U0001F64F"  # emoticons
    "\U0001F300-\U0001F5FF"  # symbols & pictographs
    "\U0001F680-\U0001F6FF"  # transport & map
    "\U0001F700-\U0001F77F"  # alchemical symbols
    "\U0001F780-\U0001F7FF"  # geometric shapes extended
    "\U0001F800-\U0001F8FF"  # arrows
    "\U0001F900-\U0001F9FF"  # supplemental symbols
    "\U0001FA00-\U0001FA6F"  # chess, etc.
    "\U0001FA70-\U0001FAFF"  # symbols & pictographs extended-A
    "\U00002700-\U000027BF"  # dingbats
    "\U00002600-\U000026FF"  # misc symbols
    "]+",
    flags=re.UNICODE,
)

def clean_for_tts(text: str) -> str:
    """Remove emojis/markdown/special chars so TTS sounds clean."""
    if not text:
        return ""

    text = _EMOJI_RE.sub("", text)

    # Remove markdown headings and emphasis
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)      # headings
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)              # bold
    text = re.sub(r"\*(.*?)\*", r"\1", text)                    # italics
    text = re.sub(r"`{1,3}(.*?)`{1,3}", r"\1", text, flags=re.DOTALL)  # code

    # Replace bullets and fancy chars with simple punctuation
    text = text.replace("•", "- ")
    text = text.replace("→", " to ")
    text = text.replace("✅", "")
    text = text.replace("❌", "")
    text = text.replace("—", "-")
    text = text.replace("–", "-")

    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text
