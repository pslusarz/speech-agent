"""Synthesize a few sentences with Kokoro and print the word timings it returns.

    uv run python scripts/kokoro_smoke.py
"""

import time

from kokoro import KPipeline

TEXT = (
    "When Zarathustra was thirty years old, he left his home and the lake of his home, "
    "and went into the mountains. Thou great star! What would be thy happiness if thou "
    "hadst not those for whom thou shinest!"
)

t = time.time()
pipeline = KPipeline(lang_code="a", repo_id="hexgrad/Kokoro-82M")
print("load", round(time.time() - t, 1), "s")
t, audio = time.time(), 0.0
for r in pipeline(TEXT, voice="af_heart"):
    audio += len(r.audio) / 24000
    print(repr(r.graphemes))
    print([(tk.text, tk.whitespace, tk.start_ts, tk.end_ts) for tk in r.tokens])
print("synth", round(time.time() - t, 2), "s for", round(audio, 2), "s of audio")
