"""KalKiKhoj worker: fetch a queued job from Apps Script, build a professionally edited 1080p documentary
(natural Hindi narration + Pixabay stock footage matched to each part of the narration, smooth transitions,
colour grade, music bed, loudness-normalised audio), upload to YouTube, report back."""
import asyncio, base64, glob, io, json, math, os, random, re, shutil, subprocess, sys, tempfile, time, traceback

import edge_tts
import requests
from PIL import Image
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

WEBAPP = os.environ["WEBAPP_URL"]
TOKEN = os.environ["WORKER_TOKEN"]
VOICE = os.environ.get("TTS_VOICE") or "hi-IN-SwaraNeural"
PRIVACY = os.environ.get("PRIVACY") or "public"
PIXABAY_KEY = os.environ.get("PIXABAY_API_KEY", "")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL") or "gemini-3.8-flash"
GEMINI_FALLBACK_MODEL = os.environ.get("GEMINI_FALLBACK_MODEL", "")
# Thumbnail models, tried in order: Nano Banana Pro first (best quality, renders headline text), then Nano Banana.
GEMINI_IMAGE_MODELS = [m.strip() for m in (os.environ.get("GEMINI_IMAGE_MODEL")
                       or "gemini-3-pro-image-preview,gemini-2.5-flash-image").split(",") if m.strip()]
TTS_ENGINE = os.environ.get("TTS_ENGINE") or "gemini"            # "gemini" (natural narrator) or "edge"
GEMINI_TTS_MODEL = os.environ.get("GEMINI_TTS_MODEL") or "gemini-2.5-flash-preview-tts"
GEMINI_TTS_VOICE = os.environ.get("GEMINI_TTS_VOICE") or "Charon"   # calm, informative male; "Kore" = firm female
EDGE_RATE = os.environ.get("EDGE_RATE") or "-4%"                    # slightly slower sounds less robotic
SCENES_PER_TTS = 6
NARRATOR_STYLE = ("Read the following Hindi text aloud as an experienced Indian documentary narrator: warm, natural and "
                  "conversational, like a real person telling a fascinating story. Use natural pauses at commas and full "
                  "stops, gentle emphasis on important words, and a steady, unhurried pace. Read only the text below, "
                  "word for word; do not read these instructions.")
MUSIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "music")   # drop royalty-free .mp3 files here
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{}:generateContent"

W, H, FPS = 1920, 1080, 25
SCENE_WORDS = 28      # narration words per scene (~10 s of Hindi speech)
MAX_SHOT = 6.0        # seconds; longer scenes get several different visuals
TF = 12               # transition length in frames (~0.5 s)
CHUNK = 12            # shots joined per ffmpeg pass (keeps memory low)
# a calm cross-dissolve most of the time, an occasional slide or dip to black at a new scene
SCENE_TRANSITIONS = ["fade", "smoothleft", "fade", "fadeblack", "fade", "smoothright", "fade", "slideleft"]
LOOK = "eq=contrast=1.06:saturation=1.12:gamma=0.98,vignette=angle=PI/7"   # gentle cinematic grade
WORK_ENC = ["-an", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "15", "-r", str(FPS),
            "-pix_fmt", "yuv420p", "-video_track_timescale", "12800"]   # near-lossless intermediates


def log(msg):
    print(time.strftime("[%H:%M:%S UTC]"), msg, flush=True)


def api(payload, tries=1):
    payload["token"] = TOKEN
    for i in range(tries):
        try:
            r = requests.post(WEBAPP, json=payload, timeout=120)
            r.raise_for_status()
            j = r.json()
            if j.get("error"):
                raise RuntimeError("Apps Script: " + j["error"])
            return j
        except Exception as e:
            if i == tries - 1:
                raise
            log(f"API call '{payload.get('action')}' failed ({e}), retrying...")
            time.sleep(30 * (i + 1))


def run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode:
        raise RuntimeError("ffmpeg failed: " + p.stderr[-800:])


def duration(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "default=nw=1:nk=1", path], check=True, capture_output=True, text=True)
    return float(out.stdout.strip())


# ---------- Gemini (scene plan, voice, thumbnail) ----------
def gemini_call(model, body, timeout=180):
    r = requests.post(GEMINI_URL.format(model), headers={"x-goog-api-key": GEMINI_KEY}, json=body, timeout=timeout)
    if not r.ok:
        raise RuntimeError(f"Gemini {model} HTTP {r.status_code}: {r.text[:200]}")
    cand = (r.json().get("candidates") or [{}])[0]
    return (cand.get("content") or {}).get("parts") or []


def gemini_json(prompt):
    last = None
    for model in [m for m in (GEMINI_MODEL, GEMINI_FALLBACK_MODEL) if m]:
        for attempt in range(2):
            try:
                parts = gemini_call(model, {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                                            "generationConfig": {"temperature": 0.4, "responseMimeType": "application/json"}})
                text = "".join(p.get("text", "") for p in parts).strip()
                text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
                try:
                    return json.loads(text)
                except ValueError:
                    return json.loads(text[text.index("{"):text.rindex("}") + 1])
            except Exception as e:
                last = e
                log(f"  Gemini attempt failed: {str(e)[:200]}")
                time.sleep(15)
    raise RuntimeError(f"Gemini unavailable: {last}")


def gemini_image(model, prompt, path):
    parts = gemini_call(model, {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                                "generationConfig": {"responseModalities": ["TEXT", "IMAGE"],
                                                     "imageConfig": {"aspectRatio": "16:9"}}}, timeout=300)
    for p in parts:
        data = (p.get("inlineData") or p.get("inline_data") or {}).get("data")
        if data:
            save_cover(base64.b64decode(data), path, (1280, 720))
            return
    raise RuntimeError("no image in Gemini response")


# ---------- narration ----------
def split_scenes(script):
    sentences = [s.strip() for s in re.split(r"(?<=[।.!?])\s+", script) if s.strip()]
    scenes, cur = [], []
    for s in sentences:
        cur.append(s)
        if len(" ".join(cur).split()) >= SCENE_WORDS:
            scenes.append(" ".join(cur)); cur = []
    if cur:
        if scenes and len(" ".join(cur).split()) < SCENE_WORDS // 2:
            scenes[-1] += " " + " ".join(cur)
        else:
            scenes.append(" ".join(cur))
    return scenes


def tts(text, path):
    for attempt in range(3):
        try:
            asyncio.run(edge_tts.Communicate(text, VOICE, rate=EDGE_RATE).save(path))
            return
        except Exception:
            if attempt == 2:
                raise
            time.sleep(5)


def join_wavs(wavs, tmp):
    lst = os.path.join(tmp, "audio.txt")
    open(lst, "w").write("".join(f"file '{f}'\n" for f in wavs))
    out = os.path.join(tmp, "voice.wav")
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", out])
    return out


def edge_voiceover(scenes, tmp):
    """One audio file per scene, so each visual can be cut to exactly match its narration."""
    wavs, durs = [], []
    for i, text in enumerate(scenes):
        if i % 10 == 0:
            log(f"Voiceover (Edge) scene {i + 1}-{min(i + 10, len(scenes))}/{len(scenes)}")
        mp3, wav = os.path.join(tmp, f"v{i}.mp3"), os.path.join(tmp, f"v{i}.wav")
        tts(text, mp3)
        run(["ffmpeg", "-y", "-i", mp3, "-af", "apad=pad_dur=0.3", "-ar", "24000", "-ac", "1", wav])
        wavs.append(wav); durs.append(duration(wav))
    return join_wavs(wavs, tmp), durs


def gemini_voiceover(scenes, tmp):
    """Natural narrator voice from Gemini TTS. Scenes are voiced in groups (fewer requests, smoother delivery);
    each scene's share of a group's audio is estimated from its length in characters."""
    wavs, durs = [], []
    groups = [scenes[i:i + SCENES_PER_TTS] for i in range(0, len(scenes), SCENES_PER_TTS)]
    for g, group in enumerate(groups):
        log(f"Voiceover (Gemini, {GEMINI_TTS_VOICE}) part {g + 1}/{len(groups)}")
        text = " ".join(group)
        body = {"contents": [{"role": "user", "parts": [{"text": NARRATOR_STYLE + "\n\n" + text}]}],
                "generationConfig": {"responseModalities": ["AUDIO"], "speechConfig": {
                    "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": GEMINI_TTS_VOICE}}}}}
        for attempt in range(4):
            try:
                part = next(p for p in gemini_call(GEMINI_TTS_MODEL, body, timeout=300) if "inlineData" in p)
                rate = re.search(r"rate=(\d+)", part["inlineData"].get("mimeType", "")) or None
                raw, wav = os.path.join(tmp, f"g{g}.pcm"), os.path.join(tmp, f"g{g}.wav")
                open(raw, "wb").write(base64.b64decode(part["inlineData"]["data"]))
                run(["ffmpeg", "-y", "-f", "s16le", "-ar", rate.group(1) if rate else "24000", "-ac", "1", "-i", raw,
                     "-af", "apad=pad_dur=0.4", "-ar", "24000", "-ac", "1", wav])
                d = duration(wav)
                if d < len(text) / 30:   # Hindi speech is ~12-18 chars/s; much shorter means the audio was cut off
                    raise RuntimeError(f"audio too short ({d:.0f}s for {len(text)} chars)")
                break
            except Exception as e:
                if attempt == 3:
                    raise
                log(f"  Gemini TTS attempt {attempt + 1} failed: {str(e)[:200]}")
                time.sleep(20 * (attempt + 1))
        wavs.append(wav)
        total = sum(len(s) for s in group)
        durs += [d * len(s) / total for s in group]
        time.sleep(5)   # stay under the free tier's requests-per-minute limit
    return join_wavs(wavs, tmp), durs


def make_voiceover(scenes, tmp):
    if GEMINI_KEY and TTS_ENGINE != "edge":
        try:
            out, durs = gemini_voiceover(scenes, tmp)
            log(f"Voiceover ready (Gemini, {sum(durs) / 60:.1f} min, {len(scenes)} scenes)")
            return out, durs
        except Exception as e:
            # the whole video falls back, so one video never mixes two different voices
            log(f"Gemini TTS failed ({str(e)[:200]}), using the Edge voice for this video")
    out, durs = edge_voiceover(scenes, tmp)
    log(f"Voiceover ready (Edge, {sum(durs) / 60:.1f} min, {len(scenes)} scenes)")
    return out, durs


# ---------- visual plan ----------
def search_words(text, n=4):
    return " ".join(re.sub(r"[^A-Za-z0-9 ]", " ", text.split(",")[0]).split()[:n])


def plan_visuals(job, scenes):
    """Two English stock-footage searches (main + backup) for every scene, plus the thumbnail idea."""
    prompts = job.get("image_prompts") or [job.get("thumb_idea") or "india documentary"]
    fallback = [[search_words(prompts[i * len(prompts) // len(scenes)])] for i in range(len(scenes))]
    plan = {"queries": fallback, "thumbnail_prompt": job.get("thumb_idea") or job["title"],
            "thumbnail_query": fallback[0][0], "thumbnail_text": ""}
    if not GEMINI_KEY:
        log("No GEMINI_API_KEY, using the image prompts from the Sheet as search words")
        return plan
    numbered = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(scenes))
    try:
        out = gemini_json(
            "You are the editor of a professional Hindi YouTube documentary. For EACH numbered narration segment below, "
            "choose the B-roll a good editor would cut to while that line is spoken. Give two English stock-footage search "
            "queries for Pixabay, 'main | backup', each 2-4 concrete visual words (what the camera would actually show), e.g. "
            "'indian city traffic | busy street night', 'robot arm factory | assembly line machines'. The backup should be "
            "more generic so it always finds footage. No names of real people or brands, no abstract words. Keep visual "
            "continuity between neighbouring segments.\n"
            f"Also give thumbnail_prompt: a vivid English prompt for an eye-catching 16:9 YouTube thumbnail image about '{job['title']}' "
            "(describe the scene only, no text, no logos, no real people), thumbnail_text: a punchy 2-4 word Hindi headline "
            "in Devanagari for the thumbnail (curiosity-driven, not misleading), and thumbnail_query: 2-4 words to find a thumbnail photo.\n"
            f'Return ONLY JSON: {{"queries":["main | backup" x {len(scenes)}],"thumbnail_prompt":"","thumbnail_text":"","thumbnail_query":""}}\n\n'
            "SEGMENTS:\n" + numbered)
        q = [[p.strip() for p in str(x).split("|") if p.strip()] for x in (out.get("queries") or [])]
        plan["queries"] = [(q[i] if i < len(q) and q[i] else []) + fallback[i] for i in range(len(scenes))]
        plan["thumbnail_prompt"] = out.get("thumbnail_prompt") or plan["thumbnail_prompt"]
        plan["thumbnail_query"] = out.get("thumbnail_query") or plan["thumbnail_query"]
        plan["thumbnail_text"] = str(out.get("thumbnail_text") or "").strip()[:40]
        log(f"Scene plan from Gemini: {len(q)} scenes, e.g. {plan['queries'][:2]}")
    except Exception as e:
        log(f"Gemini scene plan failed ({e}), using the image prompts from the Sheet instead")
    return plan


# ---------- Pixabay ----------
PIXABAY_CACHE, USED = {}, set()


def pixabay(kind, q):
    key = (kind, q)
    if key not in PIXABAY_CACHE:
        url = "https://pixabay.com/api/videos/" if kind == "video" else "https://pixabay.com/api/"
        params = {"key": PIXABAY_KEY, "q": q[:100], "safesearch": "true", "per_page": 30}
        if kind == "photo":
            params.update(image_type="photo", orientation="horizontal", min_width=1280)
        hits = []
        for attempt in range(3):
            r = requests.get(url, params=params, timeout=60)
            if r.status_code == 429:   # 100 requests/min limit
                time.sleep(30); continue
            r.raise_for_status()
            hits = r.json().get("hits") or []
            break
        PIXABAY_CACHE[key] = hits
        time.sleep(0.7)
    return PIXABAY_CACHE[key]


def save_cover(data, path, size=(W, H)):
    w, h = size
    im = Image.open(io.BytesIO(data)).convert("RGB")
    # cover-crop to 16:9
    scale = max(w / im.width, h / im.height)
    im = im.resize((int(im.width * scale + 0.5), int(im.height * scale + 0.5)), Image.LANCZOS)
    l, t = (im.width - w) // 2, (im.height - h) // 2
    im.crop((l, t, l + w, t + h)).save(path, quality=92)


def landscape_video(hit):
    for size in ("large", "medium"):   # prefer real 1080p footage
        v = (hit.get("videos") or {}).get(size) or {}
        if v.get("url") and v.get("width", 0) >= v.get("height", 1) and v.get("width", 0) >= 1280:
            return v["url"]
    return None


def fetch_visual(queries, want_video, need_sec, n, tmp):
    """Best unused footage for this shot. Returns (kind, path) with kind 'video', 'photo' or 'card'."""
    tries = list(dict.fromkeys(queries + [" ".join(q.split()[:2]) for q in queries]))
    for q in tries:
        for kind in (("video", "photo") if want_video else ("photo", "video")):
            try:
                hits = [h for h in pixabay(kind, q) if (kind, h["id"]) not in USED]
                if kind == "video":   # clips long enough not to loop come first
                    hits.sort(key=lambda h: h.get("duration", 0) < need_sec)
                for hit in hits:
                    src = landscape_video(hit) if kind == "video" else hit.get("largeImageURL")
                    if not src:
                        continue
                    data = requests.get(src, timeout=180)
                    data.raise_for_status()
                    USED.add((kind, hit["id"]))
                    if kind == "video":
                        path = os.path.join(tmp, f"src{n}.mp4")
                        open(path, "wb").write(data.content)
                    else:
                        path = os.path.join(tmp, f"src{n}.jpg")
                        save_cover(data.content, path)
                    return kind, path, q
            except Exception as e:
                log(f"  shot {n + 1}: pixabay {kind} '{q}' failed: {e}")
    path = os.path.join(tmp, f"src{n}.jpg")
    Image.new("RGB", (W, H), (20 + n * 15 % 80, 30, 60)).save(path)   # plain fallback card
    return "card", path, ""


# ---------- editing ----------
def render_shot(kind, src, frames, n, tmp):
    out = os.path.join(tmp, f"shot{n:03d}.mp4")
    if kind == "video":
        vf = f"scale={W}:{H}:force_original_aspect_ratio=increase:flags=lanczos,crop={W}:{H},fps={FPS},setsar=1,{LOOK}"
        run(["ffmpeg", "-y", "-stream_loop", "-1", "-i", src, "-vf", vf, "-frames:v", str(frames)] + WORK_ENC + [out])
    else:
        # slow Ken Burns move (zoom in / zoom out / pan) so photos feel alive
        z, x, y = [("1+0.12*on/{N}", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"),
                   ("1.12-0.12*on/{N}", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"),
                   ("1.12", "(iw-iw/zoom)*on/{N}", "ih/2-(ih/zoom/2)")][n % 3]
        z, x = z.format(N=frames), x.format(N=frames)
        vf = (f"scale={W * 2}:{H * 2}:flags=lanczos,zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={W}x{H}:fps={FPS},"
              f"setsar=1,{LOOK}")
        run(["ffmpeg", "-y", "-i", src, "-vf", vf, "-frames:v", str(frames)] + WORK_ENC + [out])
    os.remove(src)
    return out


def xfade_join(clips, slots, transitions, out):
    """Cross-transition clips. Every clip except the very last one of the film is TF frames longer than its slot,
    so each transition starts exactly at its slot boundary and the picture never drifts from the voice."""
    if len(clips) == 1:
        shutil.move(clips[0], out)
        return
    graph, prev, offset = [], "0:v", 0
    for k in range(1, len(clips)):
        offset += slots[k - 1]
        graph.append(f"[{prev}][{k}:v]xfade=transition={transitions[k - 1]}:duration={TF / FPS:.3f}:"
                     f"offset={offset / FPS:.3f}[x{k}]")
        prev = f"x{k}"
    inputs = [a for c in clips for a in ("-i", c)]
    run(["ffmpeg", "-y"] + inputs + ["-filter_complex", ";".join(graph), "-map", f"[{prev}]"] + WORK_ENC + [out])
    for c in clips:
        os.remove(c)


def join_with_transitions(shots, slots, transitions, tmp):
    """Joins in chunks of CHUNK shots, then joins the chunks the same way, so ffmpeg never opens 70 files at once."""
    level = 0
    while len(shots) > 1:
        new_shots, new_slots, new_tr = [], [], []
        for c in range(0, len(shots), CHUNK):
            group = shots[c:c + CHUNK]
            out = os.path.join(tmp, f"join{level}_{c:03d}.mp4")
            xfade_join(group, slots[c:c + CHUNK], transitions[c:c + len(group) - 1], out)
            new_shots.append(out); new_slots.append(sum(slots[c:c + CHUNK]))
            if c + CHUNK < len(shots):
                new_tr.append(transitions[c + CHUNK - 1])
        shots, slots, transitions, level = new_shots, new_slots, new_tr, level + 1
    return shots[0]


def pick_music():
    tracks = sorted(glob.glob(os.path.join(MUSIC_DIR, "*.mp3")) + glob.glob(os.path.join(MUSIC_DIR, "*.m4a")))
    return random.choice(tracks) if tracks else None


def final_master(video, voice, tmp):
    """Final 1080p encode: fade in/out, cleaned-up voice, optional music bed that ducks under the voice,
    loudness normalised to YouTube's -14 LUFS."""
    total = duration(voice)
    vf = f"[0:v]fade=t=in:st=0:d=0.8,fade=t=out:st={max(total - 1.5, 0):.2f}:d=1.5[vv]"
    voice_fx = "highpass=f=70,lowpass=f=14000,acompressor=threshold=-20dB:ratio=3:attack=10:release=250:makeup=2"
    music = pick_music()
    inputs = ["-i", video, "-i", voice]
    if music:
        log(f"Music bed: {os.path.basename(music)}")
        inputs += ["-stream_loop", "-1", "-i", music]
        af = (f"[1:a]{voice_fx},aresample=48000,asplit=2[vo][key];"
              f"[2:a]aresample=48000,volume=0.22,afade=t=in:d=2,afade=t=out:st={max(total - 3, 0):.2f}:d=3[mu];"
              "[mu][key]sidechaincompress=threshold=0.02:ratio=10:attack=30:release=600[duck];"
              "[vo][duck]amix=inputs=2:duration=first:normalize=0,loudnorm=I=-14:TP=-1.5:LRA=11[aa]")
    else:
        log(f"No music in {MUSIC_DIR} (optional: add royalty-free .mp3 files there for a music bed)")
        af = f"[1:a]{voice_fx},aresample=48000,loudnorm=I=-14:TP=-1.5:LRA=11[aa]"
    final = os.path.join(tmp, "final.mp4")
    run(["ffmpeg", "-y"] + inputs + ["-filter_complex", vf + ";" + af, "-map", "[vv]", "-map", "[aa]",
         "-c:v", "libx264", "-preset", "medium", "-crf", "19", "-profile:v", "high", "-pix_fmt", "yuv420p",
         "-r", str(FPS), "-g", str(FPS * 2), "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
         "-t", f"{total:.3f}", "-movflags", "+faststart", final])
    return final


def build_video(durs, plan, audio, tmp):
    # slot = how many frames each shot owns on the timeline; cumulative rounding keeps picture and voice in sync
    slots, owner, t = [], [], 0.0
    for i, d in enumerate(durs):
        k = max(1, math.ceil(d / MAX_SHOT))
        start_f, end_f = round(t * FPS), round((t + d) * FPS)
        t += d
        for j in range(k):
            slots.append((end_f - start_f) * (j + 1) // k - (end_f - start_f) * j // k)
            owner.append((i, j))
    n = len(slots)
    shots, real, first_frame = [], 0, None
    for s in range(n):
        i, j = owner[s]
        frames = slots[s] + (TF if s < n - 1 else 0)
        kind, src, used_q = fetch_visual(plan["queries"][i], s % 4 != 3, frames / FPS, s, tmp)   # mostly video, every 4th a photo
        real += kind != "card"
        log(f"Shot {s + 1}/{n}: {kind:5s} '{used_q}' ({slots[s] / FPS:.1f}s)")
        shots.append(render_shot(kind, src, max(frames, TF + 1), s, tmp))
        if first_frame is None and kind != "card":
            first_frame = os.path.join(tmp, "frame.jpg")
            run(["ffmpeg", "-y", "-ss", "1", "-i", shots[-1], "-frames:v", "1", "-q:v", "3", first_frame])
    if real < 0.6 * n:
        raise RuntimeError(f"Only {real}/{n} shots found real footage (Pixabay down or key missing)")
    # transition into each shot: a scene change gets the next style from the list, shots inside a scene just dissolve
    transitions = [SCENE_TRANSITIONS[owner[s][0] % len(SCENE_TRANSITIONS)] if owner[s][1] == 0 else "fade"
                   for s in range(1, n)]
    log(f"Editing: joining {n} shots with transitions")
    edit = join_with_transitions(shots, slots, transitions, tmp)
    log("Mastering: colour, fades, audio mix, 1080p encode")
    final = final_master(edit, audio, tmp)
    log(f"Video ready ({duration(final) / 60:.1f} min, {n} shots, 1080p, {os.path.getsize(final) / 1e6:.0f} MB)")
    return final, first_frame


def make_thumbnail(plan, frame, tmp):
    """Gemini image if possible, else a Pixabay photo, else a frame from the video. Returns (path, source)."""
    path = os.path.join(tmp, "thumb.jpg")
    if GEMINI_KEY:
        for model in GEMINI_IMAGE_MODELS:
            # only the Pro model writes text reliably; the others get a clean picture
            text = plan.get("thumbnail_text") if "pro" in model else ""
            prompt = (f"Create a professional, eye-catching 16:9 YouTube thumbnail for a Hindi documentary. Scene: "
                      f"{plan['thumbnail_prompt']}. Cinematic, high contrast, vivid colours, dramatic lighting, one clear "
                      "subject, sharp focus, no logos, no watermarks, no real people. " +
                      (f'Add the Hindi headline "{text}" in large, bold, perfectly spelled Devanagari letters with a strong '
                       "outline, on one side of the image so it does not cover the subject. No other text."
                       if text else "No text at all."))
            for attempt in range(2):
                try:
                    gemini_image(model, prompt, path)
                    plan["thumbnail_prompt_used"] = prompt
                    return path, f"Gemini ({model})"
                except Exception as e:
                    log(f"Thumbnail with {model} failed: {str(e)[:200]}")
                    if " 429" not in str(e) and " 503" not in str(e):
                        break   # e.g. model not available on this key: try the next model
                    time.sleep(20)
        log("Gemini thumbnail not possible, using a stock photo")
    try:
        for hit in pixabay("photo", plan["thumbnail_query"]):
            if ("photo", hit["id"]) not in USED:
                r = requests.get(hit["largeImageURL"], timeout=60)
                r.raise_for_status()
                save_cover(r.content, path, (1280, 720))
                return path, "Pixabay"
    except Exception as e:
        log(f"Pixabay thumbnail failed ({e}), using a video frame")
    shutil.copy(frame, path)
    return path, "video frame"


# ---------- YouTube ----------
def youtube():
    creds = Credentials(None, refresh_token=os.environ["YT_REFRESH_TOKEN"],
                        client_id=os.environ["YT_CLIENT_ID"], client_secret=os.environ["YT_CLIENT_SECRET"],
                        token_uri="https://oauth2.googleapis.com/token")
    return build("youtube", "v3", credentials=creds)


def upload(job, video, thumb):
    yt = youtube()
    desc = (job["description"] + "\n\nइस वीडियो में AI-जनित आवाज़ का उपयोग किया गया है।"
            "\nVisuals: Pixabay")
    body = {
        "snippet": {"title": job["title"][:100], "description": desc[:4900], "tags": job["tags"],
                    "categoryId": "27", "defaultLanguage": "hi", "defaultAudioLanguage": "hi"},
        "status": {"privacyStatus": PRIVACY, "selfDeclaredMadeForKids": False, "containsSyntheticMedia": True},
    }
    media = MediaFileUpload(video, chunksize=8 * 1024 * 1024, resumable=True, mimetype="video/mp4")

    def send():
        req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
        resp = None
        while resp is None:
            status, resp = req.next_chunk()
            if status:
                log(f"Uploading to YouTube: {int(status.progress() * 100)}%")
        return resp

    log(f"Uploading to YouTube ({PRIVACY})")
    try:
        resp = send()
    except HttpError as e:
        if "containsSyntheticMedia" not in str(e):
            raise
        body["status"].pop("containsSyntheticMedia")   # older API surface: retry without the flag
        resp = send()
    vid = resp["id"]
    log(f"Uploaded, video id {vid}")
    try:
        yt.thumbnails().set(videoId=vid, media_body=MediaFileUpload(thumb, mimetype="image/jpeg")).execute()
        log("Thumbnail set")
    except Exception as e:
        log(f"Thumbnail skipped (channel may need phone verification): {e}")
    return f"https://www.youtube.com/watch?v={vid}"


def main():
    if not PIXABAY_KEY:
        sys.exit("PIXABAY_API_KEY secret is missing: add it in GitHub > Settings > Secrets > Actions.")
    log("Asking the Brain for a job...")
    job = api({"action": "next"}, tries=3).get("job")
    if not job:
        log("No job. (No QUEUE_RENDER row in the Sheet, today's publish limit reached, or pipeline paused.)")
        return
    log(f"Job picked: {job['id']} | row {job['row']} | {job['title']}")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            scenes = split_scenes(job["script"])
            if not scenes:
                raise RuntimeError("Script (column F) is empty")
            log(f"Step 1/5: voiceover ({len(scenes)} scenes)")
            audio, durs = make_voiceover(scenes, tmp)
            log("Step 2/5: choosing B-roll for every scene")
            plan = plan_visuals(job, scenes)
            log("Step 3/5: footage, editing and mastering")
            video, frame = build_video(durs, plan, audio, tmp)
            log("Step 4/5: thumbnail")
            thumb, thumb_src = make_thumbnail(plan, frame, tmp)
            log(f"Thumbnail from {thumb_src}")
            log("Step 5/5: uploading")
            url = upload(job, video, thumb)
    except Exception as e:
        traceback.print_exc()
        log("FAILED, telling the Brain so it can retry later")
        api({"action": "fail", "row": job["row"], "error": str(e)[:300]}, tries=3)
        sys.exit(1)
    # Outside the try: once the video is on YouTube, never report "fail" (that would re-queue and upload it twice).
    log(f"Published: {url}")
    try:
        api({"action": "done", "row": job["row"], "url": url, "title": job["title"],
             "thumb_prompt": plan.get("thumbnail_prompt_used") or plan["thumbnail_prompt"], "thumb_source": thumb_src}, tries=5)
    except Exception:
        traceback.print_exc()
        log(f"!! Video is LIVE but the Sheet was not updated. Set row {job['row']} to PUBLISHED and paste {url} in column N.")
        sys.exit(1)
    log("Sheet updated to PUBLISHED. Done.")


if __name__ == "__main__":
    main()
