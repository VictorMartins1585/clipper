# -*- mode: python ; coding: utf-8 -*-
# Receita do PyInstaller:  pyinstaller Clipper.spec --noconfirm
# Gera dist/Clipper/ (pasta com Clipper.exe). O ffmpeg e o deno são copiados para dist/Clipper/bin
# depois do build (veja .github/workflows/build-windows.yml e build_windows.bat).
from PyInstaller.utils.hooks import collect_all

datas, binaries, hiddenimports = [("fonts", "fonts")], [], ["clipper"]
for pkg in ("faster_whisper", "ctranslate2", "onnxruntime", "av", "tokenizers", "huggingface_hub",
            "yt_dlp", "yt_dlp_ejs", "googleapiclient", "google_auth_oauthlib", "certifi"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    ["gui.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["matplotlib", "scipy", "pandas", "IPython", "pytest", "torch", "gradio"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Clipper",
    console=False,          # sem janela preta; troque para True se precisar depurar
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="Clipper", upx=False)
