# LemmeGetThatSong.spec — build with: pyinstaller LemmeGetThatSong.spec
#
# Produces a single-file executable in dist/. Bundles Python, CustomTkinter,
# Pillow and the app modules. Onyx is NOT bundled — it is discovered at
# runtime (see mogg_crypt._candidates).

import os
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

datas = collect_data_files("customtkinter")

hiddenimports = (
    collect_submodules("customtkinter")
    + ["mogg_check", "mogg_crypt", "preview",
       "PIL._tkinter_finder",
       "pygame", "pygame.mixer"]
)

a = Analysis(
    ["yarg_gui.py"],
    pathex=[os.path.abspath(".")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # numpy is excluded for size. pygame.mixer does not need it, but if
    # previews raise ImportError in the frozen build, drop "numpy" here.
    excludes=["matplotlib", "numpy", "scipy", "pandas", "PyQt5", "PySide2"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="LemmeGetThatSong",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # set True while debugging to see tracebacks
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
