"""Record the README demo: read, cut in by voice, answer, carry on. Makes docs/demo.gif.

    DEMO=1 LOG_FILE=logs/demo.log uv run uvicorn main.app:app --port 8001 &
    uv run --with playwright --with imageio-ffmpeg python .claude/skills/recording-demos/record_demo.py

Headless Chromium drives the app in demo mode (scripted replies, real Kokoro and
Parakeet). Its microphone is replaced by a question spoken by Kokoro, so no one's
voice is recorded. A GIF has no sound, so captions say what is being heard.
"""

import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# this file lives in .claude/skills/recording-demos/
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

URL = "http://127.0.0.1:8001"
SIZE = dict(width=1280, height=760)
QUESTION = "Who is he talking to?"
OUT = ROOT / "docs" / "demo.gif"
# a 43 s take played at 1.45x is about 30 s; at this rate and width it is about 2.6 MB
SPEED, FPS, WIDTH = 1.45, 10, 860

# the page's microphone, replaced by the question. It speaks when the driver says so,
# once the page shows it is listening: headless Chromium is slow to open a microphone
FAKE_MIC = """
navigator.mediaDevices.getUserMedia = async () => {
  const ctx = new AudioContext();
  const data = await (await fetch('/demo-question.wav')).arrayBuffer();
  const src = ctx.createBufferSource();
  src.buffer = await ctx.decodeAudioData(data);
  const dest = ctx.createMediaStreamDestination();
  src.connect(dest);
  window.__speak = () => { src.start(); return src.buffer.duration; };
  return dest.stream;
};
"""

CAPTION = """
const c = document.createElement('div');
c.id = 'caption';
c.style.cssText = 'position:fixed;left:50%;bottom:18px;transform:translateX(-50%);' +
  'background:rgba(17,17,17,.86);color:#fff;font:600 19px/1.35 system-ui,sans-serif;' +
  'padding:10px 18px;border-radius:10px;max-width:1100px;text-align:center;z-index:99';
document.body.append(c);
const hide = document.createElement('style');
hide.textContent = '.debug, #speak-note { display:none !important }';
document.head.append(hide);
"""


def question_wav() -> bytes:
    from main.player.tts import Kokoro

    return Kokoro(voice="am_michael").speak(QUESTION).wav


def record(video_dir: Path, question: bytes) -> Path:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
        ctx = browser.new_context(viewport=SIZE, record_video_dir=str(video_dir), record_video_size=SIZE)
        ctx.add_init_script(FAKE_MIC)
        page = ctx.new_page()
        page.route("**/demo-question.wav", lambda r: r.fulfill(body=question, content_type="audio/wav"))
        page.goto(URL)
        page.evaluate(CAPTION)

        def caption(text):
            page.evaluate("t => document.getElementById('caption').textContent = t", text)

        def say(text):
            page.fill("input[name=msg]", text)
            page.click("form button:has-text('Send')")

        caption("An agent reads Nietzsche aloud. Each word lights up as it is heard.")
        time.sleep(2.0)
        say("Please read me the prologue.")
        page.wait_for_selector("#text span.lit", timeout=60_000)
        time.sleep(9.0)

        caption(f'Hold to speak, and ask: "{QUESTION}" - the reading stops at once.')
        box = page.locator("#speak").bounding_box()
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        page.mouse.down()
        page.wait_for_selector("#speak.recording", timeout=15_000)
        seconds = page.evaluate("() => window.__speak()")
        time.sleep(seconds + 0.5)
        page.mouse.up()
        page.wait_for_selector(f".row.user:has-text('{QUESTION[:-1]}')", timeout=30_000)

        caption("The agent answers aloud. The chat shows only what has been spoken so far.")
        page.wait_for_selector(".row.agent.speaking", timeout=30_000)
        page.wait_for_function(
            "() => !document.querySelector('.row.agent.speaking') && "
            "[...document.querySelectorAll('.row.agent')].some(r => r.textContent.includes('carry on'))",
            timeout=60_000,
        )
        time.sleep(0.8)

        caption('"Please carry on." The reading resumes at the sentence where the question was asked.')
        say("Please carry on.")
        page.wait_for_function("() => !document.getElementById('audio').paused", timeout=30_000)
        time.sleep(6.0)
        time.sleep(2.0)
        video = Path(page.video.path())
        ctx.close()
        browser.close()
        return video


def cut(video: Path, out: Path, speed: float = 1.0, fps: int = 12, width: int = 900,
        dither: str = "none"):
    import imageio_ffmpeg

    ff = imageio_ffmpeg.get_ffmpeg_exe()
    palette = out.with_suffix(".palette.png")
    # the recording is lossy video: its shimmer makes every frame look new to the GIF
    # encoder, so it is smoothed out first (6.5 MB without this, 2.6 MB with it)
    base = f"setpts=PTS/{speed},hqdn3d=3:3:8:8,fps={fps},scale={width}:-1:flags=lanczos"
    subprocess.run([ff, "-y", "-loglevel", "error", "-i", str(video),
                    "-vf", f"{base},palettegen=stats_mode=diff", str(palette)], check=True)
    subprocess.run([ff, "-y", "-loglevel", "error", "-i", str(video), "-i", str(palette),
                    "-lavfi", f"{base}[x];[x][1:v]paletteuse=dither={dither}:diff_mode=rectangle",
                    str(out)], check=True)
    palette.unlink()
    # a working file to check the arc, not for the repo
    sheet = ROOT / "logs" / (out.stem + "-sheet.png")
    subprocess.run([ff, "-y", "-loglevel", "error", "-i", str(out),
                    "-vf", "fps=1/3,scale=440:-1,tile=4x3", "-frames:v", "1", str(sheet)], check=True)
    return sheet


def main():
    if "--cut-only" in sys.argv:
        raw = ROOT / "logs" / "demo-raw.webm"
        sheet = cut(raw, OUT, speed=SPEED, fps=FPS, width=WIDTH)
        print(f"{OUT} {OUT.stat().st_size / 1e6:.1f} MB; contact sheet {sheet}")
        return
    question = question_wav()
    with tempfile.TemporaryDirectory() as d:
        video = record(Path(d), question)
        raw = ROOT / "logs" / "demo-raw.webm"
        shutil.copy(video, raw)
    sheet = cut(raw, OUT, speed=SPEED, fps=FPS, width=WIDTH)
    print(f"{OUT} {OUT.stat().st_size / 1e6:.1f} MB; contact sheet {sheet}; raw video {raw}")


if __name__ == "__main__":
    main()
