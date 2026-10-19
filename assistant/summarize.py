"""Short summaries written by the local model (Ollama)."""
import ollama

MODEL = "qwen3.5:4b"

PROMPT = """You write one-line summaries for a personal morning news briefing.
Rules:
- Use ONLY facts stated in the title and text below. Never add names, numbers or details.
- Write exactly ONE sentence in English, at most 30 words.
- If the text is German, translate it.
- No introduction, no quotes, no markdown.

Title: {title}
Text: {text}"""

def summarize(article):
    text = article.get("summary") or ""
    if len(text) < 40:
        text = "(no text available - summarize the title only)"
    r = ollama.chat(
        model=MODEL,
        messages=[{"role": "user", "content": PROMPT.format(title=article["title"], text=text)}],
        think=False,
        keep_alive="10m",
        options={"temperature": 0.2, "num_predict": 80, "num_ctx": 2048},
    )
    line = r.message.content.strip().split("\n")[0].strip().strip('"')
    if not line:
        raise RuntimeError("model returned an empty summary")
    return line
