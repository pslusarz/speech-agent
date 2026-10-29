"""How fast and how accurate Parakeet is on the recorded voice samples.

    uv run python scripts/asr_bench.py

For each sample in samples/ (recorded in the app's Voice samples panel), and for the
model's own test clip, it transcribes with 1, 2 and 4 threads - roughly what a small
server's vCPUs allow - then again with 2 threads while Kokoro makes book audio on the
same cores, as it would on the server. Where the person typed what they said, it gives
the word error rate against that.
"""

import re
import sys
import threading
import time
from pathlib import Path

from huggingface_hub import hf_hub_download

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from main import samples  # noqa: E402
from main.asr import REPO, Parakeet, read_wav  # noqa: E402

THREADS = (1, 2, 4)
BUSY_THREADS = 2


def words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


def wer(said: str, heard: str) -> float:
    """Word error rate: word edits needed to turn what was heard into what was said."""
    a, b = words(said), words(heard)
    d = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        prev, d[0] = d[0], i
        for j, y in enumerate(b, 1):
            prev, d[j] = d[j], min(d[j] + 1, d[j - 1] + 1, prev + (x != y))
    return d[len(b)] / max(1, len(a))


def clips() -> list[tuple[str, Path, str]]:
    out = [(s.name, samples.path(s.name), s.said) for s in samples.all_samples()]
    out.append(("model test clip", Path(hf_hub_download(REPO, "test_wavs/0.wav")), ""))
    return out


def busy_with_kokoro(stop: threading.Event):
    from main.player.tts import Kokoro

    k = Kokoro()
    line = "Thou great star! What would be thy happiness if thou hadst not those for whom thou shinest!"
    while not stop.is_set():
        k.speak(line)


def main():
    todo = clips()
    print(f"{len(todo) - 1} recorded sample(s), plus the model's test clip\n")
    results: dict[str, dict] = {name: {} for name, _, _ in todo}
    for n in THREADS:
        p = Parakeet(threads=n)
        for name, path, said in todo:
            r = p.transcribe(*read_wav(path))
            results[name][n] = r
    stop = threading.Event()
    kokoro = threading.Thread(target=busy_with_kokoro, args=(stop,), daemon=True)
    kokoro.start()
    time.sleep(8)  # Kokoro loaded and generating
    p = Parakeet(threads=BUSY_THREADS)
    for name, path, said in todo:
        results[name]["busy"] = p.transcribe(*read_wav(path))
    stop.set()

    head = " | ".join(f"{n} thr" for n in THREADS)
    print(f"| clip | audio | {head} | {BUSY_THREADS} thr + Kokoro | WER | transcript |")
    print("|---" * (5 + len(THREADS)) + "|")
    for name, path, said in todo:
        r = results[name]
        best = r[THREADS[-1]]
        times = " | ".join(f"{r[n].took:.2f}s" for n in THREADS)
        score = f"{wer(said, best.text):.0%}" if said else "-"
        print(f"| {name} | {best.seconds:.1f}s | {times} | {r['busy'].took:.2f}s | {score} | {best.text} |")
        if said:
            print(f"|  said: | | | | | | {said} |")


if __name__ == "__main__":
    main()
