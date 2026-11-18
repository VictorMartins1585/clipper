#!/usr/bin/env python3
"""
Clipper: link do YouTube (ou arquivo de vídeo) -> momentos de destaque -> cortes 9:16 com legenda
-> SQLite -> (opcional) YouTube Shorts.

Uso:
    python gui.py                                      # abre a janela (recomendado)
    python clipper.py --url LINK [LINK ...]            # modo terminal (só gera os cortes)
    python clipper.py --url LINK --upload --len 90 --n 4

O link pode ser um vídeo, uma live gravada, uma playlist ou um canal
(ex.: https://www.youtube.com/@canal/streams). Também aceita um arquivo de vídeo local.

Critérios de corte — cada um vira uma curva "interesse x tempo"; as curvas são
normalizadas e somadas com os pesos escolhidos na interface:
    audio    : saltos de volume em relação ao entorno (gritos, explosões, risadas)
    motion   : mudança visual entre quadros (ação, troca de cena)
    chat     : pico de mensagens no chat (replay de lives; já compensa o atraso do chat)
    heatmap  : gráfico "Mais repetidos" do YouTube (quando o vídeo tem)
    keywords : palavras-chave faladas (transcreve o vídeo inteiro: lento em lives longas)
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import yt_dlp
from yt_dlp.utils import DownloadCancelled

# ----------------------------- CONFIGURAÇÃO -----------------------------
FROZEN = getattr(sys, "frozen", False)                       # True dentro do .exe
APP_DIR = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
# No .exe os arquivos do usuário ficam em C:\Users\<você>\Clipper; rodando do código, ao lado dele.
BASE = (Path.home() / "Clipper") if FROZEN else APP_DIR
BASE.mkdir(parents=True, exist_ok=True)
VOD_DIR, CLIP_DIR = BASE / "vods", BASE / "clips"
DB_PATH = BASE / "clipper.db"
FONT_FILE = "Anton-Regular.ttf"                              # fonte das legendas (pasta fonts/)

COOKIES_BROWSER = None        # "firefox", "chrome", "edge", "brave"... (None = sem cookies)
MAX_HEIGHT = 720              # resolução máxima do download
MIN_CLIP_LEN = 70             # cortes nunca menores que 1min10s
MAX_CLIP_LEN = 180            # Shorts aceitam até 3 min
CHAT_DELAY = 8                # o chat reage ~8s depois do momento
MAX_UPLOADS_PER_RUN = 5       # cota padrão da API: ~6 uploads/dia
CATEGORY_ID = "20"            # 20 = Games, 24 = Entretenimento, 22 = Pessoas e blogs

CRITERIA = {                  # nome interno -> nome exibido
    "audio": "áudio",
    "motion": "movimento",
    "chat": "chat",
    "heatmap": "mais repetidos",
    "keywords": "palavras-chave",
}
DEFAULT_KEYWORDS = ["meu deus", "caramba", "nossa", "não acredito", "que isso",
                    "socorro", "olha isso", "absurdo", "insano", "vamos"]


@dataclass
class Settings:
    max_videos: int = 1                 # p/ canais e playlists
    clip_len: int = 80                  # segundos
    n_clips: int = 5                    # máx. de cortes por vídeo
    min_score: float = 1.0              # 0 = sempre pega os N melhores; maior = mais exigente
    weights: dict = field(default_factory=lambda: {
        "audio": 1.0, "motion": 0.6, "chat": 1.0, "heatmap": 1.2, "keywords": 0.0})
    keywords: list = field(default_factory=lambda: list(DEFAULT_KEYWORDS))
    layout: str = "blur"                # "blur" = vídeo inteiro c/ fundo desfocado | "crop" = recorte central
    subtitles: bool = True
    whisper_model: str = "small"        # tiny/base/small/medium
    language: str = "pt"                # "" = detectar automaticamente
    force: bool = False                 # reprocessa vídeos já processados
    delete_vod: bool = True             # apaga o vídeo baixado depois de cortar
    upload: bool = False
    privacy: str = "private"            # troque para "public" só depois de revisar
    title: str = "{title} | parte {n}"
    description: str = "Corte de {credit}\nVídeo original: {url}\n#Shorts"
    tags: list = field(default_factory=lambda: ["Shorts", "cortes"])
# -------------------------------------------------------------------------


# ------------------------- FERRAMENTAS E CANCELAMENTO --------------------
def setup_tools_path():
    """Põe ffmpeg/deno embutidos (pasta bin/ ao lado do programa) no PATH deste processo."""
    extra = [APP_DIR / "bin", APP_DIR, Path.home() / ".deno" / "bin"]
    cur = os.environ.get("PATH", "")
    add = [str(p) for p in extra if p.is_dir() and str(p) not in cur]
    if add:
        os.environ["PATH"] = os.pathsep.join(add + [cur])


setup_tools_path()
NO_WINDOW = 0x08000000 if os.name == "nt" else 0     # CREATE_NO_WINDOW: não pisca janela preta no Windows


def missing_tools():
    return [t for t in ("ffmpeg",) if not shutil.which(t)]


class Cancelled(Exception):
    """Levantada quando o usuário clica em Parar."""


CANCEL = threading.Event()
_procs = set()
_procs_lock = threading.Lock()


def check_cancel():
    if CANCEL.is_set():
        raise Cancelled()


def cancel():
    """Pede para parar: sinaliza e mata os subprocessos (ffmpeg) em andamento."""
    CANCEL.set()
    with _procs_lock:
        for p in list(_procs):
            try:
                p.kill()
            except OSError:
                pass


def popen(cmd, **kw):
    check_cancel()
    p = subprocess.Popen(cmd, creationflags=NO_WINDOW, **kw)
    with _procs_lock:
        _procs.add(p)
    return p


def _release(p):
    with _procs_lock:
        _procs.discard(p)


def run(cmd, **kw):
    p = popen(cmd, **kw)
    try:
        rc = p.wait()
    finally:
        _release(p)
    check_cancel()
    if rc:
        raise subprocess.CalledProcessError(rc, cmd)


def fmt_time(t):
    t = int(t)
    return f"{t // 3600}:{t % 3600 // 60:02}:{t % 60:02}" if t >= 3600 else f"{t // 60}:{t % 60:02}"


# ------------------------------- SQLITE ---------------------------------
def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS vods(id TEXT PRIMARY KEY, credit TEXT);
        CREATE TABLE IF NOT EXISTS clips(
            key TEXT PRIMARY KEY,
            vod_id TEXT, sha256 TEXT UNIQUE, path TEXT,
            credit TEXT, youtube_id TEXT);
    """)
    # colunas novas (migração automática de bancos antigos)
    for table, col, typ in [("vods", "title", "TEXT"), ("vods", "url", "TEXT"),
                            ("clips", "start", "REAL"), ("clips", "score", "REAL"),
                            ("clips", "reason", "TEXT"), ("clips", "title", "TEXT"),
                            ("clips", "src_url", "TEXT")]:
        try:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        except sqlite3.OperationalError:
            pass
    db.commit()
    return db


# ------------------------------ DOWNLOAD --------------------------------
def ydl_base():
    o = {"quiet": True, "no_warnings": True, "ignoreerrors": True, "noprogress": True}
    if COOKIES_BROWSER:
        o["cookiesfrombrowser"] = (COOKIES_BROWSER,)
    return o


def make_ydl(opts, log=print):
    """YoutubeDL com fallback: se os cookies do navegador não puderem ser lidos, tenta sem eles."""
    try:
        return yt_dlp.YoutubeDL(opts)
    except Exception as e:
        if "cookiesfrombrowser" not in opts:
            raise
        log(f"aviso: não consegui ler os cookies do {COOKIES_BROWSER} ({str(e)[:90]}). Seguindo sem cookies.")
        return yt_dlp.YoutubeDL({k: v for k, v in opts.items() if k != "cookiesfrombrowser"})


def list_videos(url, limit, log=print):
    """Expande vídeo/playlist/canal em [(id, url)]."""
    check_cancel()
    with make_ydl({**ydl_base(), "extract_flat": "in_playlist", "playlistend": limit}, log) as y:
        info = y.extract_info(url, download=False)
    if not info:
        return []
    if "entries" not in info:
        return [(info["id"], info.get("webpage_url") or url)]
    out = []
    for e in info.get("entries") or []:
        if not e or len(out) >= limit:
            continue
        link = e.get("url") or e.get("webpage_url") or f"https://www.youtube.com/watch?v={e['id']}"
        if e.get("ie_key") == "YoutubeTab" or "/playlist?" in link:   # aba de canal / sub-playlist
            out += list_videos(link, limit - len(out), log)
        else:
            out.append((e["id"], link))
    return out[:limit]


def download(url, want_chat, log=print):
    """Baixa o vídeo (e o replay do chat, se pedido). Retorna (path, info) ou (None, None)."""
    VOD_DIR.mkdir(exist_ok=True)
    last = {"pct": -1}

    def hook(d):
        if CANCEL.is_set():
            raise DownloadCancelled()
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if total:
                pct = int(100 * d.get("downloaded_bytes", 0) / total) // 10 * 10
                if pct != last["pct"]:
                    last["pct"] = pct
                    log(f"baixando... {pct}%")

    opts = {**ydl_base(),
            "format": f"bv*[height<={MAX_HEIGHT}]+ba/b[height<={MAX_HEIGHT}]/b",
            "merge_output_format": "mp4",
            "outtmpl": str(VOD_DIR / "%(id)s.%(ext)s"),
            "progress_hooks": [hook]}
    if want_chat:
        opts.update(writesubtitles=True, subtitleslangs=["live_chat"])
    try:
        with make_ydl(opts, log) as y:
            info = y.extract_info(url, download=True)
    except DownloadCancelled:
        raise Cancelled()
    check_cancel()
    if not info:
        return None, None
    path = VOD_DIR / f"{info['id']}.mp4"
    if not path.exists():
        cands = [p for p in VOD_DIR.glob(f"{info['id']}.*") if p.suffix in (".mp4", ".mkv", ".webm")]
        path = cands[0] if cands else None
    return path, info


# ------------------------------- SINAIS ---------------------------------
def fit(x, n):
    """Ajusta um sinal ao comprimento n (corta ou repete a borda)."""
    x = np.asarray(x, dtype=np.float32)
    if len(x) >= n:
        return x[:n]
    return np.pad(x, (0, n - len(x)), mode="edge") if len(x) else np.zeros(n, np.float32)


def moving_avg(x, k):
    return np.convolve(x, np.ones(k) / k, mode="same") if k > 1 else x


def rolling_median(x, k):
    k |= 1
    pad = np.pad(x, (k // 2, k // 2), mode="edge")
    return np.median(np.lib.stride_tricks.sliding_window_view(pad, k), axis=1)


def robust_z(x):
    med = np.median(x)
    mad = np.median(np.abs(x - med)) * 1.4826
    sd = max(mad, 0.1 * x.std(), 1e-9)          # piso: sinal quase constante não vira "pico"
    return (x - med) / sd


def signal_audio(video, sr=8000):
    """RMS por segundo."""
    proc = popen(["ffmpeg", "-v", "error", "-i", str(video), "-vn", "-ac", "1",
                  "-ar", str(sr), "-f", "s16le", "-"], stdout=subprocess.PIPE)
    rms = []
    while True:
        buf = proc.stdout.read(sr * 2 * 600)          # 10 min por leitura
        if not buf:
            break
        x = np.frombuffer(buf, dtype=np.int16).astype(np.float32) / 32768
        m = len(x) // sr
        if m:
            rms.extend(np.sqrt((x[: m * sr].reshape(m, sr) ** 2).mean(axis=1)))
    proc.wait()
    _release(proc)
    check_cancel()
    return np.array(rms, dtype=np.float32)


def signal_motion(video, n):
    """Diferença média entre keyframes consecutivos (só decodifica keyframes: rápido mesmo em lives longas)."""
    w, h = 64, 36
    proc = popen(["ffmpeg", "-v", "info", "-nostats", "-skip_frame", "nokey", "-i", str(video),
                  "-an", "-vf", f"scale={w}:{h},format=gray,showinfo", "-fps_mode", "passthrough",
                  "-f", "rawvideo", "-"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    data, err = proc.communicate()
    _release(proc)
    check_cancel()
    times = [float(t) for t in re.findall(rb"pts_time:\s*([\d.]+)", err)]
    k = min(len(data) // (w * h), len(times))
    if k < 3:
        return None
    f = np.frombuffer(data[: k * w * h], np.uint8).reshape(-1, w * h)[:k].astype(np.float32)
    d = np.abs(np.diff(f, axis=0)).mean(axis=1)
    return np.interp(np.arange(n), np.array(times[1:k]), d).astype(np.float32)


def signal_chat(json_path, n):
    """Mensagens de chat por segundo (replay de live), adiantado CHAT_DELAY s."""
    if not json_path or not Path(json_path).exists():
        return None
    counts = np.zeros(n, np.float32)
    with open(json_path, encoding="utf-8") as fh:
        for line in fh:
            try:
                act = json.loads(line).get("replayChatItemAction") or {}
                t = int(act["videoOffsetTimeMsec"]) // 1000
            except (ValueError, KeyError, TypeError):
                continue
            if 0 <= t < n:
                counts[t] += max(1, len(act.get("actions", [])))
    if counts.sum() < 50:
        return None
    return np.concatenate([counts[CHAT_DELAY:], np.zeros(CHAT_DELAY, np.float32)])


def signal_heatmap(info, n):
    hm = (info or {}).get("heatmap") or []
    if not hm:
        return None
    v = np.zeros(n, np.float32)
    for seg in hm:
        v[int(seg["start_time"]): int(np.ceil(seg["end_time"]))] = seg["value"]
    return v


_whisper = {}


def whisper_model(name, log=print):
    """Carrega o modelo de transcrição (faster-whisper, roda na CPU). Baixa na primeira vez."""
    if name not in _whisper:
        from faster_whisper import WhisperModel
        log(f"carregando modelo de legendas '{name}' (na primeira vez ele é baixado, pode demorar)...")
        _whisper[name] = WhisperModel(name, device="cpu", compute_type="int8",
                                      download_root=str(BASE / "models"))
    return _whisper[name]


def signal_keywords(video, n, s, log=print):
    words = [k.strip().lower() for k in s.keywords if k.strip()]
    if not words:
        return None
    segments, _ = whisper_model(s.whisper_model, log).transcribe(
        str(video), language=s.language or None, condition_on_previous_text=False, vad_filter=True)
    pats = [re.compile(r"\b" + re.escape(k) + r"\b") for k in words]
    v = np.zeros(n, np.float32)
    next_log = 600
    for seg in segments:                     # gerador: a transcrição acontece aqui
        check_cancel()
        hits = sum(len(p.findall(seg.text.lower())) for p in pats)
        if hits:
            a = int(seg.start)
            v[a: max(a + 1, int(seg.end))] += hits
        if seg.end > next_log:
            log(f"transcrevendo... {fmt_time(seg.end)} de {fmt_time(n)}")
            next_log += 600
    return v if v.any() else None


def compute_signals(video, info, chat_json, s, log):
    """Retorna (rms, {critério: z-score por segundo})."""
    w = s.weights
    log("analisando áudio...")
    rms = signal_audio(video)
    n = len(rms)
    if n == 0:
        return rms, {}
    raw = {"audio": rms}
    if w.get("motion", 0) > 0:
        log("analisando movimento...")
        raw["motion"] = signal_motion(video, n)
    if w.get("chat", 0) > 0:
        raw["chat"] = signal_chat(chat_json, n)
        log("chat: " + ("ok" if raw["chat"] is not None else "indisponível (não é live ou sem replay)"))
    if w.get("heatmap", 0) > 0:
        raw["heatmap"] = signal_heatmap(info, n)
        log("mais repetidos: " + ("ok" if raw["heatmap"] is not None else "indisponível"))
    if w.get("keywords", 0) > 0:
        log("transcrevendo o vídeo inteiro para palavras-chave (pode demorar)...")
        raw["keywords"] = signal_keywords(video, n, s, log)

    z = {}
    for name, x in raw.items():
        if x is None:
            continue
        x = fit(x, n)
        if name in ("audio", "motion"):            # o que importa é o salto em relação ao entorno
            x = x - rolling_median(x, 61)
        elif name == "chat":
            x = moving_avg(x, 10)
            x = x - rolling_median(x, 301)
        elif name == "keywords":
            x = moving_avg(x, 5)
        z[name] = robust_z(x)
    return rms, z


def pick_moments(rms, z, s):
    """Combina os critérios e escolhe os melhores trechos sem sobreposição."""
    n, L = len(rms), s.clip_len
    parts = {k: s.weights[k] * np.clip(v, -1, 5) for k, v in z.items() if s.weights.get(k, 0) > 0}
    if not parts or n < L + 10:
        return []
    total = sum(parts.values()) / sum(s.weights[k] for k in parts)
    score = robust_z(moving_avg(total, 3))

    lead = int(L * 0.35)                       # o pico fica a ~35% do corte (contexto antes, reação depois)
    db_level = 20 * np.log10(rms + 1e-6)
    picked = []                                # (pico, nota, início)
    for i in np.argsort(score)[::-1]:
        if len(picked) >= s.n_clips or score[i] < s.min_score:
            break
        i = int(i)
        if any(abs(i - p) < L // 2 for p, _, _ in picked):   # mesmo momento de um corte já escolhido
            continue
        s0 = int(np.clip(i - lead, 0, n - L))
        lo, hi = max(0, s0 - 8), min(n - L, s0 + 4)
        start = lo + int(np.argmin(db_level[lo: hi + 1]))   # começa num momento mais silencioso
        if all(abs(start - st) >= L for _, _, st in picked):  # sem sobreposição
            picked.append((i, float(score[i]), start))

    out = []
    for i, sc, start in sorted(picked, key=lambda t: t[2]):
        contrib = {k: max(0.0, float(p[max(0, i - 2): i + 3].mean())) for k, p in parts.items()}
        tot = sum(contrib.values()) or 1
        reason = ", ".join(f"{CRITERIA[k]} {100 * v / tot:.0f}%"
                           for k, v in sorted(contrib.items(), key=lambda kv: -kv[1]) if v / tot >= 0.1)
        out.append({"peak": i, "start": start, "score": round(sc, 2), "reason": reason or "-"})
    return out


# ---------------------------- CORTE + LEGENDAS ---------------------------
LAYOUTS = {
    "crop": "crop=trunc(ih*9/16/2)*2:ih,scale=1080:1920:flags=lanczos,setsar=1",
    "blur": ("split[a][b];"
             "[a]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,boxblur=10:2,"
             "scale=1080:1920[bg];"
             "[b]scale=1080:-2:flags=lanczos[fg];"
             "[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1"),
}


def transcribe_to_srt(audio, srt, s, log=print):
    """Legenda dinâmica: blocos de 3 palavras com timestamps por palavra."""
    segments, _ = whisper_model(s.whisper_model, log).transcribe(
        str(audio), word_timestamps=True, language=s.language or None, vad_filter=True)
    words = []
    for seg in segments:
        check_cancel()
        words += seg.words or []

    def ts(t):
        h, r = divmod(t, 3600)
        m, sec = divmod(r, 60)
        return f"{int(h):02}:{int(m):02}:{sec:06.3f}".replace(".", ",")

    lines = []
    for i in range(0, len(words), 3):
        chunk = words[i:i + 3]
        text = "".join(w.word for w in chunk).strip().upper()
        if text:
            lines.append(f"{len(lines) + 1}\n{ts(chunk[0].start)} --> {ts(chunk[-1].end)}\n{text}\n")
    srt.write_text("\n".join(lines), encoding="utf-8")
    return bool(lines)


def prepare_font():
    """Copia a fonte das legendas para clips/_fonts (o ffmpeg a lê por caminho relativo, sem escapes)."""
    cands = [APP_DIR / "fonts" / FONT_FILE, Path(getattr(sys, "_MEIPASS", APP_DIR)) / "fonts" / FONT_FILE]
    src = next((c for c in cands if c.is_file()), None)
    if not src:
        return False
    dst = CLIP_DIR / "_fonts"
    dst.mkdir(parents=True, exist_ok=True)
    if not (dst / FONT_FILE).exists():
        shutil.copy(src, dst / FONT_FILE)
    return True


def make_clip(vod, start, out, s, log=print):
    CLIP_DIR.mkdir(exist_ok=True)
    vf = LAYOUTS[s.layout]
    srt = out.with_suffix(".srt")
    if s.subtitles:
        wav = out.with_suffix(".wav")
        run(["ffmpeg", "-v", "error", "-y", "-ss", str(start), "-t", str(s.clip_len), "-i", str(vod),
             "-vn", "-ac", "1", "-ar", "16000", str(wav)])
        try:
            has_speech = transcribe_to_srt(wav, srt, s, log)
        finally:
            wav.unlink(missing_ok=True)
        if has_speech:
            style = "Alignment=2,MarginV=60,FontSize=14,Bold=0,Outline=3,PrimaryColour=&H00FFFFFF"
            fonts = ""
            if prepare_font():
                style = "Fontname=Anton," + style
                fonts = ":fontsdir=_fonts"
            vf += f",subtitles={srt.name}{fonts}:force_style='{style}'"
    # cwd=CLIP_DIR evita problemas de escape de caminho (C:\...) dentro do filtro
    try:
        run(["ffmpeg", "-v", "error", "-y", "-ss", str(start), "-i", str(Path(vod).resolve()),
             "-t", str(s.clip_len), "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
             "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", out.name], cwd=CLIP_DIR)
    finally:
        srt.unlink(missing_ok=True)


# ------------------------------ PIPELINE ---------------------------------
def process_video(db, vod_id, vod_path, info, chat_json, s, log):
    credit = (info or {}).get("channel") or (info or {}).get("uploader") or "desconhecido"
    title = (info or {}).get("title") or vod_id
    src_url = (info or {}).get("webpage_url") or str(vod_path)

    rms, z = compute_signals(vod_path, info, chat_json, s, log)
    log(f"duração: {fmt_time(len(rms))} | critérios usados: {', '.join(CRITERIA[k] for k in z) or 'nenhum'}")
    moments = pick_moments(rms, z, s)
    if not moments:
        log("nenhum momento passou do limiar — tente diminuir a 'exigência'.")

    made = []
    for m in moments:
        key = f"{vod_id}:{m['start']}:{s.clip_len}"
        if db.execute("SELECT 1 FROM clips WHERE key=?", (key,)).fetchone():
            continue
        out = CLIP_DIR / f"{vod_id}_{m['start']}_{s.clip_len}s.mp4"
        log(f"cortando trecho {fmt_time(m['start'])}–{fmt_time(m['start'] + s.clip_len)} ({m['reason']})")
        check_cancel()
        make_clip(vod_path, m["start"], out, s, log)
        sha = hashlib.sha256(out.read_bytes()).hexdigest()
        if db.execute("SELECT 1 FROM clips WHERE sha256=?", (sha,)).fetchone():
            out.unlink()
            continue
        db.execute("INSERT INTO clips(key,vod_id,sha256,path,credit,start,score,reason,title,src_url) "
                   "VALUES(?,?,?,?,?,?,?,?,?,?)",
                   (key, vod_id, sha, str(out), credit, m["start"], m["score"], m["reason"], title, src_url))
        db.commit()
        made.append({"key": key, "path": str(out), "file": out.name, **m})
    return made


def run_pipeline(urls, s, log=print):
    s.clip_len = int(np.clip(s.clip_len, MIN_CLIP_LEN, MAX_CLIP_LEN))
    check_cancel()
    if missing_tools():
        raise RuntimeError("ffmpeg não encontrado. Instale o ffmpeg ou use a versão completa do programa.")
    if s.subtitles or s.weights.get("keywords", 0) > 0:
        try:
            import faster_whisper  # noqa: F401
        except ImportError:
            log("aviso: faster-whisper não instalado (pip install faster-whisper) — seguindo sem legendas.")
            s.subtitles, s.weights["keywords"] = False, 0
    db = init_db()
    all_clips = []
    for url in urls:
        url = url.strip()
        if not url:
            continue
        local = Path(url).expanduser()
        if local.is_file():                                   # arquivo local
            log(f"arquivo local: {local.name}")
            vid = "local_" + hashlib.sha1(str(local.resolve()).encode()).hexdigest()[:10]
            info = {"title": local.stem, "channel": "", "webpage_url": str(local)}
            all_clips += process_video(db, vid, local, info, None, s, log)
            continue

        log(f"lendo {url} ...")
        videos = list_videos(url, max(1, s.max_videos), log)
        if not videos:
            log("nada encontrado nesse link (privado? live ainda acontecendo? cookies?).")
        for vid, link in videos:
            check_cancel()
            if not s.force and db.execute("SELECT 1 FROM vods WHERE id=?", (vid,)).fetchone():
                log(f"{vid} já foi processado (marque 'Reprocessar' para cortar de novo).")
                continue
            log(f"baixando {link} ...")
            path, info = download(link, s.weights.get("chat", 0) > 0, log)
            if not path:
                log(f"falha ao baixar {link} (veja a opção de cookies do navegador)")
                continue
            log(f"baixado: {info.get('title')}")
            chat_json = VOD_DIR / f"{vid}.live_chat.json"
            clips = process_video(db, vid, path, info, chat_json, s, log)
            db.execute("INSERT OR REPLACE INTO vods(id,credit,title,url) VALUES(?,?,?,?)",
                       (vid, info.get("channel") or info.get("uploader"), info.get("title"), link))
            db.commit()
            all_clips += clips
            if s.delete_vod:
                path.unlink(missing_ok=True)
                chat_json.unlink(missing_ok=True)

    log(f"pronto: {len(all_clips)} corte(s) em {CLIP_DIR}")
    if s.upload and all_clips:
        for key, yt_id in upload_pending(db, s, [c["key"] for c in all_clips], log).items():
            for c in all_clips:
                if c["key"] == key:
                    c["youtube"] = f"https://youtube.com/shorts/{yt_id}"
    db.close()
    return all_clips


# ------------------------------- YOUTUBE ---------------------------------
def youtube_service():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    scopes = ["https://www.googleapis.com/auth/youtube.upload"]
    token = BASE / "token.json"
    creds = Credentials.from_authorized_user_file(token, scopes) if token.exists() else None
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:   # primeira vez: abre o navegador para autorizar
            if not (BASE / "client_secret.json").exists():
                raise RuntimeError(f"Falta o client_secret.json do YouTube em {BASE}. "
                                   "Veja o README (seção 'Publicar no YouTube').")
            flow = InstalledAppFlow.from_client_secrets_file(BASE / "client_secret.json", scopes)
            creds = flow.run_local_server(port=0)
        token.write_text(creds.to_json())
    return build("youtube", "v3", credentials=creds)


def upload_pending(db, s, keys=None, log=print):
    """Sobe os cortes ainda não enviados (só os de `keys`, se informado)."""
    from googleapiclient.http import MediaFileUpload
    q = "SELECT key, path, credit, title, src_url FROM clips WHERE youtube_id IS NULL"
    args = []
    if keys:
        q += f" AND key IN ({','.join('?' * len(keys))})"
        args = list(keys)
    rows = db.execute(q + " ORDER BY score DESC LIMIT ?", (*args, MAX_UPLOADS_PER_RUN)).fetchall()
    if not rows:
        return {}
    yt = youtube_service()
    done = {}
    for n, (key, path, credit, title, src_url) in enumerate(rows, 1):
        check_cancel()
        fields = {"title": title or "", "credit": credit or "", "url": src_url or "", "n": n}
        yt_title = re.sub(r"[<>]", "", s.title.format(**fields)).strip()[:100] or "Corte"
        body = {
            "snippet": {"title": yt_title,
                        "description": re.sub(r"[<>]", "", s.description.format(**fields))[:5000],
                        "tags": s.tags, "categoryId": CATEGORY_ID},
            "status": {"privacyStatus": s.privacy, "selfDeclaredMadeForKids": False},
        }
        req = yt.videos().insert(part="snippet,status", body=body,
                                 media_body=MediaFileUpload(path, resumable=True))
        resp = None
        while resp is None:
            _, resp = req.next_chunk()
        db.execute("UPDATE clips SET youtube_id=? WHERE key=?", (resp["id"], key))
        db.commit()
        done[key] = resp["id"]
        log(f"[upload] {Path(path).name} -> https://youtube.com/shorts/{resp['id']}")
    return done


# --------------------------- OPÇÕES PARA A INTERFACE ---------------------
STYLES = {   # estilo -> pesos dos critérios (o que não estiver disponível no vídeo é ignorado sozinho)
    "Automático": {"audio": 1.0, "motion": 0.6, "chat": 1.0, "heatmap": 1.2, "keywords": 0},
    "Live com chat": {"audio": 0.8, "motion": 0.3, "chat": 1.6, "heatmap": 1.0, "keywords": 0},
    "Gameplay / ação": {"audio": 1.2, "motion": 1.2, "chat": 0.6, "heatmap": 1.0, "keywords": 0},
    "Conversa / podcast (mais lento)": {"audio": 0.6, "motion": 0, "chat": 0.8, "heatmap": 1.2,
                                        "keywords": 1.2},
}
DURATIONS = {"1:15": 75, "1:30": 90, "2:00": 120, "3:00": 180}
PRIVACY_LABELS = {"Privado": "private", "Não listado": "unlisted", "Público": "public"}


# -------------------------------- MAIN -----------------------------------
def main():
    ap = argparse.ArgumentParser(description="Gera cortes verticais de vídeos do YouTube.")
    ap.add_argument("--url", nargs="*", help="link(s) ou arquivo(s); sem isso abre a janela")
    ap.add_argument("--len", type=int, default=Settings.clip_len, help="duração do corte em segundos")
    ap.add_argument("--n", type=int, default=Settings.n_clips, help="máx. de cortes por vídeo")
    ap.add_argument("--videos", type=int, default=Settings.max_videos, help="vídeos por canal/playlist")
    ap.add_argument("--layout", choices=list(LAYOUTS), default=Settings.layout)
    ap.add_argument("--cookies", default=None, help="navegador para ler cookies: firefox, chrome, edge...")
    ap.add_argument("--no-subs", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--upload", action="store_true")
    args = ap.parse_args()

    if not args.url:
        import gui
        gui.main()
        return
    global COOKIES_BROWSER
    COOKIES_BROWSER = args.cookies
    s = Settings(clip_len=args.len, n_clips=args.n, max_videos=args.videos, layout=args.layout,
                 subtitles=not args.no_subs, force=args.force, upload=args.upload)
    run_pipeline(args.url, s)


if __name__ == "__main__":
    main()
