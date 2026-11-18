#!/usr/bin/env python3
"""
Janela do Clipper (tkinter).

    python gui.py          # a partir do código
    Clipper.exe            # versão empacotada (Windows)
    Clipper.exe --selftest # verifica se tudo que o programa precisa está embutido (usado no GitHub Actions)
"""
import json
import os
import queue
import shutil
import sqlite3
import sys
import threading
import time
import traceback

# Dentro do .exe sem console, stdout/stderr são None e algumas bibliotecas quebram ao imprimir.
for _name in ("stdout", "stderr"):
    if getattr(sys, _name) is None:
        setattr(sys, _name, open(os.devnull, "w"))

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

import clipper

SETTINGS_FILE = clipper.BASE / "settings.json"
COOKIES = {"Nenhum": None, "Firefox": "firefox", "Chrome": "chrome", "Edge": "edge", "Brave": "brave"}
MODELS = {"Rápida (tiny)": "tiny", "Equilibrada (base)": "base", "Boa (small)": "small",
          "Máxima (medium)": "medium"}
LANGS = {"Português": "pt", "Inglês": "en", "Espanhol": "es", "Detectar": ""}
LAYOUT_NAMES = {"Vídeo inteiro": "blur", "Zoom no centro": "crop"}

YOUTUBE_HELP = (
    "Para publicar direto no YouTube você precisa de um arquivo 'client_secret.json' (grátis):\n\n"
    "1. Acesse console.cloud.google.com e crie um projeto.\n"
    "2. Ative a 'YouTube Data API v3'.\n"
    "3. Em 'Tela de consentimento OAuth', escolha Externo e adicione seu e-mail como usuário de teste.\n"
    "4. Em 'Credenciais', crie um 'ID do cliente OAuth' do tipo 'App para computador' e baixe o JSON.\n"
    "5. Clique em 'Escolher client_secret.json' aqui ao lado.\n\n"
    "Na primeira publicação o navegador abre para você autorizar. Os vídeos sobem como Privado "
    "(projetos novos da API só podem publicar assim até serem auditados pelo Google)."
)


def open_path(path):
    path = str(path)
    if sys.platform.startswith("win"):
        os.startfile(path)                                   # noqa: S606
    elif sys.platform == "darwin":
        os.system(f'open "{path}"')
    else:
        os.system(f'xdg-open "{path}" >/dev/null 2>&1 &')


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Clipper — cortes verticais automáticos")
        self.geometry("960x720")
        self.minsize(820, 640)
        try:
            ttk.Style().theme_use("vista" if os.name == "nt" else "clam")
        except tk.TclError:
            pass
        self.q = queue.Queue()
        self.running = False
        self.t0 = 0.0
        self.saved = self.load_settings()
        self.build()
        self.refresh_clips()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(150, self.poll)
        if clipper.missing_tools():
            self.after(400, lambda: messagebox.showwarning(
                "ffmpeg não encontrado",
                "O ffmpeg não foi encontrado. Na versão completa ele já vem junto; "
                "rodando do código, instale o ffmpeg e coloque no PATH."))

    # ------------------------------ interface ---------------------------
    def var(self, key, default, kind=tk.StringVar):
        return kind(value=self.saved.get(key, default))

    def build(self):
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=8, pady=8)
        self.nb = nb
        tab1, tab2 = ttk.Frame(nb, padding=10), ttk.Frame(nb, padding=10)
        nb.add(tab1, text="  1. Gerar cortes  ")
        nb.add(tab2, text="  2. Cortes e YouTube  ")
        self.build_generate(tab1)
        self.build_clips(tab2)

    def build_generate(self, f):
        f.columnconfigure(0, weight=1)
        ttk.Label(f, text="Link do vídeo, live, playlist ou canal (um por linha) — ou escolha um arquivo:"
                  ).grid(row=0, column=0, sticky="w")
        row = ttk.Frame(f)
        row.grid(row=1, column=0, sticky="ew", pady=(2, 8))
        row.columnconfigure(0, weight=1)
        self.links = tk.Text(row, height=3, wrap="none")
        self.links.grid(row=0, column=0, sticky="ew")
        ttk.Button(row, text="Escolher arquivo…", command=self.pick_video).grid(row=0, column=1, padx=(8, 0))

        box = ttk.LabelFrame(f, text="Opções", padding=8)
        box.grid(row=2, column=0, sticky="ew")
        for c in (1, 3, 5):
            box.columnconfigure(c, weight=1)

        self.v_style = self.var("style", "Automático")
        self.v_dur = self.var("dur", "1:30")
        self.v_n = self.var("n", 5, tk.IntVar)
        self.v_layout = self.var("layout", "Vídeo inteiro")
        self.v_lang = self.var("lang", "Português")
        self.v_cookies = self.var("cookies", "Nenhum")
        self.v_model = self.var("model", "Boa (small)")
        self.v_videos = self.var("videos", 1, tk.IntVar)
        self.v_subs = self.var("subs", True, tk.BooleanVar)
        self.v_strict = self.var("strict", False, tk.BooleanVar)
        self.v_skip = self.var("skip", False, tk.BooleanVar)

        def combo(r, c, label, v, values):
            ttk.Label(box, text=label).grid(row=r, column=c, sticky="w", padx=(0, 4), pady=3)
            ttk.Combobox(box, textvariable=v, values=values, state="readonly", width=22
                         ).grid(row=r, column=c + 1, sticky="ew", padx=(0, 12))

        def spin(r, c, label, v, lo, hi):
            ttk.Label(box, text=label).grid(row=r, column=c, sticky="w", padx=(0, 4), pady=3)
            ttk.Spinbox(box, from_=lo, to=hi, textvariable=v, width=5
                        ).grid(row=r, column=c + 1, sticky="w", padx=(0, 12))

        combo(0, 0, "Tipo de vídeo", self.v_style, list(clipper.STYLES))
        combo(0, 2, "Duração do corte", self.v_dur, list(clipper.DURATIONS))
        spin(0, 4, "Quantos cortes", self.v_n, 1, 10)
        combo(1, 0, "Enquadramento", self.v_layout, list(LAYOUT_NAMES))
        combo(1, 2, "Idioma da legenda", self.v_lang, list(LANGS))
        spin(1, 4, "Vídeos por canal", self.v_videos, 1, 10)
        combo(2, 0, "Qualidade da legenda", self.v_model, list(MODELS))
        combo(2, 2, "Cookies do navegador", self.v_cookies, list(COOKIES))
        chk = ttk.Frame(box)
        chk.grid(row=3, column=0, columnspan=6, sticky="w", pady=(4, 0))
        ttk.Checkbutton(chk, text="Legendas automáticas", variable=self.v_subs).pack(side="left", padx=(0, 14))
        ttk.Checkbutton(chk, text="Só momentos muito fortes (pode gerar menos cortes)",
                        variable=self.v_strict).pack(side="left", padx=(0, 14))
        ttk.Checkbutton(chk, text="Pular vídeos já processados", variable=self.v_skip).pack(side="left")
        ttk.Label(box, foreground="#666", wraplength=860,
                  text="Se o YouTube pedir login ou disser que você é um robô, escolha o navegador em que está "
                       "logado (o Firefox é o que funciona melhor no Windows)."
                  ).grid(row=4, column=0, columnspan=6, sticky="w", pady=(6, 0))

        bar = ttk.Frame(f)
        bar.grid(row=3, column=0, sticky="ew", pady=10)
        self.btn_go = ttk.Button(bar, text="✂  Gerar cortes", command=self.start)
        self.btn_go.pack(side="left", ipadx=14, ipady=4)
        self.btn_stop = ttk.Button(bar, text="■  Parar", command=self.stop, state="disabled")
        self.btn_stop.pack(side="left", padx=8, ipady=4)
        ttk.Button(bar, text="Abrir pasta dos cortes", command=self.open_clips).pack(side="right")
        self.status = tk.StringVar(value="Pronto. Cole um link e clique em “Gerar cortes”.")
        ttk.Label(f, textvariable=self.status).grid(row=4, column=0, sticky="w")
        self.progress = ttk.Progressbar(f, mode="indeterminate")
        self.progress.grid(row=5, column=0, sticky="ew", pady=(2, 6))
        f.rowconfigure(6, weight=1)
        self.log_box = ScrolledText(f, height=8, state="disabled", wrap="word")
        self.log_box.grid(row=6, column=0, sticky="nsew")

    def build_clips(self, f):
        f.columnconfigure(0, weight=1)
        f.rowconfigure(1, weight=1)
        ttk.Label(f, text="Selecione os cortes (Ctrl/Shift para vários). Duplo clique abre o vídeo."
                  ).grid(row=0, column=0, sticky="w")
        cols = ("file", "start", "reason", "credit", "yt")
        self.tree = ttk.Treeview(f, columns=cols, show="headings", selectmode="extended")
        for c, text, w in (("file", "Arquivo", 250), ("start", "Início", 60), ("reason", "Por que foi escolhido", 250),
                           ("credit", "Canal", 130), ("yt", "YouTube", 90)):
            self.tree.heading(c, text=text)
            self.tree.column(c, width=w, anchor="w")
        self.tree.grid(row=1, column=0, sticky="nsew", pady=6)
        sb = ttk.Scrollbar(f, command=self.tree.yview)
        sb.grid(row=1, column=1, sticky="ns", pady=6)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.bind("<Double-1>", self.open_selected)

        pub = ttk.LabelFrame(f, text="Publicar no YouTube Shorts", padding=8)
        pub.grid(row=2, column=0, columnspan=2, sticky="ew")
        pub.columnconfigure(3, weight=1)
        self.v_privacy = self.var("privacy", "Privado")
        self.v_title = self.var("title", "{title} | parte {n}")
        ttk.Label(pub, text="Visibilidade").grid(row=0, column=0, sticky="w")
        ttk.Combobox(pub, textvariable=self.v_privacy, values=list(clipper.PRIVACY_LABELS), state="readonly",
                     width=12).grid(row=0, column=1, padx=(4, 12))
        ttk.Label(pub, text="Título").grid(row=0, column=2, sticky="w")
        ttk.Entry(pub, textvariable=self.v_title).grid(row=0, column=3, sticky="ew", padx=4)
        ttk.Label(pub, foreground="#666", text="{title} = título original · {credit} = canal · {n} = número"
                  ).grid(row=1, column=0, columnspan=4, sticky="w", pady=(2, 6))
        b = ttk.Frame(pub)
        b.grid(row=2, column=0, columnspan=4, sticky="w")
        self.btn_pub = ttk.Button(b, text="📤  Publicar selecionados", command=self.publish)
        self.btn_pub.pack(side="left", ipady=3)
        ttk.Button(b, text="Escolher client_secret.json…", command=self.pick_secret).pack(side="left", padx=8)
        ttk.Button(b, text="Como configurar?", command=lambda: messagebox.showinfo(
            "Publicar no YouTube", YOUTUBE_HELP)).pack(side="left")
        ttk.Button(b, text="Atualizar lista", command=self.refresh_clips).pack(side="left", padx=8)

    # ------------------------------ configurações -----------------------
    def load_settings(self):
        try:
            return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def save_settings(self):
        data = {"style": self.v_style.get(), "dur": self.v_dur.get(), "n": self.v_n.get(),
                "layout": self.v_layout.get(), "lang": self.v_lang.get(), "cookies": self.v_cookies.get(),
                "model": self.v_model.get(), "videos": self.v_videos.get(), "subs": self.v_subs.get(),
                "strict": self.v_strict.get(), "skip": self.v_skip.get(), "privacy": self.v_privacy.get(),
                "title": self.v_title.get()}
        try:
            SETTINGS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

    # ------------------------------ ações -------------------------------
    def log(self, text):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", f"[{time.strftime('%H:%M:%S')}] {text}\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def set_running(self, running, text=None):
        self.running = running
        self.btn_go.configure(state="disabled" if running else "normal")
        self.btn_pub.configure(state="disabled" if running else "normal")
        self.btn_stop.configure(state="normal" if running else "disabled")
        if running:
            self.t0 = time.time()
            self.progress.start(12)
        else:
            self.progress.stop()
        if text:
            self.status.set(text)

    def pick_video(self):
        p = filedialog.askopenfilename(title="Escolha um vídeo", filetypes=[
            ("Vídeos", "*.mp4 *.mkv *.mov *.webm *.avi"), ("Todos", "*.*")])
        if p:
            self.links.insert("end", ("\n" if self.links.get("1.0", "end").strip() else "") + p)

    def start(self):
        if self.running:
            return
        links = [l.strip() for l in self.links.get("1.0", "end").splitlines() if l.strip()]
        if not links:
            messagebox.showinfo("Falta o link", "Cole pelo menos um link (ou escolha um arquivo de vídeo).")
            return
        try:
            n, videos = int(self.v_n.get()), int(self.v_videos.get())
        except (tk.TclError, ValueError):
            messagebox.showwarning("Opções", "“Quantos cortes” e “Vídeos por canal” precisam ser números.")
            return
        s = clipper.Settings(
            clip_len=clipper.DURATIONS[self.v_dur.get()], n_clips=n, max_videos=videos,
            min_score=2.0 if self.v_strict.get() else 0.5, weights=dict(clipper.STYLES[self.v_style.get()]),
            layout=LAYOUT_NAMES[self.v_layout.get()], subtitles=self.v_subs.get(),
            whisper_model=MODELS[self.v_model.get()], language=LANGS[self.v_lang.get()],
            force=not self.v_skip.get())
        clipper.COOKIES_BROWSER = COOKIES[self.v_cookies.get()]
        clipper.CANCEL.clear()
        self.save_settings()
        self.log("=== iniciando ===")
        self.set_running(True, "Começando…")
        threading.Thread(target=self.work, args=(links, s), daemon=True).start()

    def work(self, links, s):
        try:
            clips = clipper.run_pipeline(links, s, log=lambda m: self.q.put(("log", m)))
            self.q.put(("done", len(clips)))
        except clipper.Cancelled:
            self.q.put(("cancelled", None))
        except Exception as e:                                   # noqa: BLE001
            if clipper.CANCEL.is_set():
                self.q.put(("cancelled", None))
            else:
                self.q.put(("error", (str(e), traceback.format_exc())))

    def stop(self):
        if self.running:
            clipper.cancel()
            self.status.set("Parando…")

    def poll(self):
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "log":
                    self.log(val)
                    self.status.set(val[:110])
                elif kind == "done":
                    self.set_running(False, f"Pronto: {val} corte(s) gerado(s).")
                    self.refresh_clips()
                    if val:
                        self.nb.select(1)
                elif kind == "cancelled":
                    self.set_running(False, "Interrompido.")
                    self.log("interrompido pelo usuário.")
                    self.refresh_clips()
                elif kind == "error":
                    msg, tb = val
                    self.log(f"ERRO: {msg}\n{tb}")
                    self.set_running(False, f"Erro: {msg[:100]}")
                elif kind == "published":
                    self.set_running(False, val)
                    self.refresh_clips()
        except queue.Empty:
            pass
        if self.running:
            elapsed = int(time.time() - self.t0)
            cur = self.status.get().split("  ·  ")[0]
            if cur not in ("Parando…",):
                self.status.set(f"{cur}  ·  {elapsed // 60}:{elapsed % 60:02}")
        self.after(1000 if self.running else 250, self.poll)

    # ------------------------------ cortes ------------------------------
    def refresh_clips(self):
        self.tree.delete(*self.tree.get_children())
        self.paths = {}
        try:
            db = sqlite3.connect(clipper.DB_PATH)
            rows = db.execute("SELECT key, path, start, reason, credit, youtube_id FROM clips "
                              "ORDER BY rowid DESC LIMIT 300").fetchall()
            db.close()
        except sqlite3.Error:
            return
        for key, path, start, reason, credit, yt in rows:
            self.paths[key] = path
            self.tree.insert("", "end", iid=key, values=(
                os.path.basename(path), clipper.fmt_time(start) if start is not None else "",
                reason or "", credit or "", "postado" if yt else "pendente"))

    def open_selected(self, _e=None):
        sel = self.tree.selection()
        if sel and os.path.exists(self.paths.get(sel[0], "")):
            open_path(self.paths[sel[0]])
        elif sel:
            messagebox.showinfo("Arquivo não encontrado", "Esse corte não existe mais na pasta.")

    def open_clips(self):
        clipper.CLIP_DIR.mkdir(exist_ok=True)
        open_path(clipper.CLIP_DIR)

    # ------------------------------ YouTube -----------------------------
    def pick_secret(self):
        p = filedialog.askopenfilename(title="Escolha o client_secret.json", filetypes=[("JSON", "*.json")])
        if not p:
            return
        try:
            data = json.load(open(p, encoding="utf-8"))
            if "installed" not in data:
                raise ValueError("não é um 'ID do cliente OAuth' do tipo App para computador")
        except (OSError, ValueError) as e:
            messagebox.showerror("Arquivo inválido", f"Esse arquivo não parece ser o client_secret.json:\n{e}")
            return
        shutil.copy(p, clipper.BASE / "client_secret.json")
        (clipper.BASE / "token.json").unlink(missing_ok=True)     # força novo login com a nova credencial
        messagebox.showinfo("Pronto", "client_secret.json salvo. Agora é só publicar os cortes.")

    def publish(self):
        keys = list(self.tree.selection())
        if not keys:
            messagebox.showinfo("Publicar", "Selecione pelo menos um corte na lista.")
            return
        if not (clipper.BASE / "client_secret.json").exists() and not (clipper.BASE / "token.json").exists():
            messagebox.showinfo("Falta configurar o YouTube", YOUTUBE_HELP)
            return
        if not messagebox.askyesno("Publicar", f"Enviar {min(len(keys), clipper.MAX_UPLOADS_PER_RUN)} corte(s) "
                                   f"para o YouTube como “{self.v_privacy.get()}”?"):
            return
        s = clipper.Settings(privacy=clipper.PRIVACY_LABELS[self.v_privacy.get()],
                             title=self.v_title.get() or "{title}")
        self.save_settings()
        clipper.CANCEL.clear()
        self.log("=== publicando no YouTube ===")
        self.set_running(True, "Enviando para o YouTube…")

        def go():
            try:
                db = clipper.init_db()
                done = clipper.upload_pending(db, s, keys, log=lambda m: self.q.put(("log", m)))
                db.close()
                extra = (f" (limite de {clipper.MAX_UPLOADS_PER_RUN} por vez: selecione os outros e repita)"
                         if len(keys) > len(done) else "")
                self.q.put(("published", f"{len(done)} corte(s) enviado(s).{extra}" if done
                            else "Nada novo para enviar (já estavam publicados)."))
            except clipper.Cancelled:
                self.q.put(("cancelled", None))
            except Exception as e:                               # noqa: BLE001
                self.q.put(("error", (str(e), traceback.format_exc())))

        threading.Thread(target=go, daemon=True).start()

    def on_close(self):
        if self.running:
            if not messagebox.askyesno("Sair", "Há uma tarefa em andamento. Parar e sair?"):
                return
            clipper.cancel()
        self.destroy()


# ---------------------------------- SELFTEST --------------------------------
def selftest():
    """Confere que o pacote tem tudo que precisa. Escreve o resultado em Clipper/selftest.txt."""
    import importlib
    import subprocess
    problems, notes = [], []

    for mod in ("faster_whisper", "ctranslate2", "onnxruntime", "av", "tokenizers", "huggingface_hub",
                "yt_dlp", "yt_dlp_ejs", "numpy", "google_auth_oauthlib", "googleapiclient.http", "tkinter",
                "_tkinter"):
        try:
            importlib.import_module(mod)
        except Exception as e:                                   # noqa: BLE001
            problems.append(f"import {mod}: {e!r}")

    try:                                                         # documentos estáticos da API do YouTube
        from googleapiclient.discovery import build
        build("youtube", "v3", developerKey="x", cache_discovery=False)
    except Exception as e:                                       # noqa: BLE001
        problems.append(f"googleapiclient/youtube: {e!r}")

    try:
        import yt_dlp
        yt_dlp.YoutubeDL({"quiet": True}).get_info_extractor("Youtube")
    except Exception as e:                                       # noqa: BLE001
        problems.append(f"yt-dlp extrator Youtube: {e!r}")

    if clipper.missing_tools():
        problems.append("ffmpeg não encontrado")
    else:
        try:
            out = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True,
                                 creationflags=clipper.NO_WINDOW).stdout
            if " subtitles " not in out:
                problems.append("ffmpeg sem o filtro 'subtitles' (libass)")
            # renderiza uma legenda de verdade com a fonte embutida
            if not clipper.prepare_font():
                problems.append(f"fonte {clipper.FONT_FILE} não encontrada")
            else:
                clipper.CLIP_DIR.mkdir(exist_ok=True)
                srt = clipper.CLIP_DIR / "selftest.srt"
                srt.write_text("1\n00:00:00,000 --> 00:00:01,500\nTESTE DE LEGENDA\n", encoding="utf-8")
                outp = clipper.CLIP_DIR / "selftest.mp4"
                outp.unlink(missing_ok=True)
                clipper.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=blue:s=320x568:d=1.5",
                             "-vf", "subtitles=selftest.srt:fontsdir=_fonts:force_style='Fontname=Anton,FontSize=14'",
                             "-c:v", "libx264", "selftest.mp4"], cwd=clipper.CLIP_DIR)
                if not outp.exists() or outp.stat().st_size == 0:
                    problems.append("ffmpeg não gerou o vídeo de teste com legenda")
                for f in (srt, outp):
                    f.unlink(missing_ok=True)
        except Exception as e:                                   # noqa: BLE001
            problems.append(f"ffmpeg/legenda: {e!r}")

    if not clipper.missing_tools():         # decodificação de áudio usada pelas legendas (ffmpeg -> numpy)
        try:
            tone = clipper.BASE / "selftest_tone.wav"
            clipper.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=f=440:r=16000",
                         "-t", "2", str(tone)])
            a = clipper.load_audio(tone)
            tone.unlink(missing_ok=True)
            if not (30000 <= len(a) <= 34000):
                problems.append(f"load_audio devolveu {len(a)} amostras (esperado ~32000)")
        except Exception as e:                                   # noqa: BLE001
            problems.append(f"load_audio (ffmpeg -> numpy): {e!r}")

    try:                                    # modelo de detecção de voz (onnxruntime + arquivo embutido)
        import numpy as np
        from faster_whisper.vad import get_speech_timestamps
        get_speech_timestamps(np.zeros(16000 * 2, dtype=np.float32))
    except Exception as e:                                       # noqa: BLE001
        problems.append(f"VAD (onnxruntime/silero): {e!r}")

    if os.environ.get("CLIPPER_SELFTEST_ONLINE") and not clipper.missing_tools():
        try:                                # teste de ponta a ponta: baixa o modelo tiny e transcreve silêncio
            wav = clipper.BASE / "selftest.wav"
            clipper.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono",
                         "-t", "3", str(wav)])
            audio = clipper.load_audio(wav)      # decodifica pelo ffmpeg, como o programa faz de verdade
            wav.unlink(missing_ok=True)
            segs, _ = clipper.whisper_model("tiny", print).transcribe(audio, vad_filter=True)
            list(segs)
            notes.append("modelo tiny baixado e executado")
        except Exception as e:                                   # noqa: BLE001
            problems.append(f"faster-whisper online: {e!r}")

    if not shutil.which("deno"):
        problems.append("deno não encontrado (o yt-dlp precisa dele para o YouTube)")
    notes.append(f"BASE={clipper.BASE} APP_DIR={clipper.APP_DIR} frozen={clipper.FROZEN}")

    text = ("SELFTEST OK\n" if not problems else "SELFTEST FALHOU\n") + "\n".join(problems + notes) + "\n"
    try:
        (clipper.BASE / "selftest.txt").write_text(text, encoding="utf-8")
    except OSError:
        pass
    print(text)
    return 1 if problems else 0


def main():
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    if os.name == "nt":                                          # texto nítido em telas com escala > 100%
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:                                        # noqa: BLE001
            pass
    App().mainloop()


if __name__ == "__main__":
    main()
