# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller onedir spec for the Moru engine sidecar.

Build (from engine/):

    uv sync --locked --group build
    uv run pyinstaller --clean --noconfirm packaging/moru-engine-server.spec

Output: dist/moru-engine-server/ — stage the whole directory at
desktop/resources/engine/ before `bun run dist`. The desktop main process
spawns resources/engine/moru-engine-server(.exe) --port N --token T
(desktop/src/main/sidecar.ts) and sets MORU_ARTIFACTS_DIR to
resources/engine/artifacts.
"""

import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# SPECPATH = directory containing this spec file (engine/packaging).
ENGINE_DIR = os.path.dirname(SPECPATH)
PKG_SRC = os.path.join(ENGINE_DIR, "src", "moru_engine")

# Package data referenced via Path(__file__) at runtime — must land inside
# _internal/moru_engine/ mirroring the source layout. Vanilla glossaries are
# NOT bundled: the runtime receives vanilla terms via community sync.
datas = [
    (
        os.path.join(PKG_SRC, "assets", "vanilla_minecraft_assets"),
        "moru_engine/assets/vanilla_minecraft_assets",
    ),
    (
        os.path.join(PKG_SRC, "evalset", "data"),
        "moru_engine/evalset/data",
    ),
    (
        os.path.join(PKG_SRC, "assets", "pack.png"),
        "moru_engine/assets",
    ),
]
# litellm ships JSON data (model cost map, tokenizer configs) loaded at import.
datas += collect_data_files("litellm")
datas += collect_data_files("dspy")

hiddenimports = [
    "moru_engine._embedded_keys",
    # tiktoken discovers encoding plugins via pkgutil — invisible to analysis.
    "tiktoken_ext",
    "tiktoken_ext.openai_public",
]
# Editable install (uv_build) + heavy dynamic imports: collect explicitly.
hiddenimports += collect_submodules("moru_engine")
hiddenimports += collect_submodules("dspy")
hiddenimports += collect_submodules("litellm")
hiddenimports += collect_submodules("uvicorn")
hiddenimports += collect_submodules("ftb_snbt_lib")
hiddenimports += collect_submodules("aiofiles")

a = Analysis(
    [os.path.join(SPECPATH, "entry.py")],
    pathex=[os.path.join(ENGINE_DIR, "src")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        "matplotlib",
        "IPython",
        "notebook",
        "pytest",
        "tkinter",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="moru-engine-server",
    debug=False,
    strip=False,
    upx=False,
    # Console stays on: stdout/stderr are piped into the Electron main log.
    console=True,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="moru-engine-server",
)
