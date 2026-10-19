"""Build LivePanel.exe: one file, no Python needed on the user's computer (Windows).

    pip install pyinstaller moomoo-api
    python scripts/build_exe.py            ->  dist/LivePanel.exe

Double-clicking the .exe opens the panel window. The first time there is no config, so one is created in
%USERPROFILE%\\.livepanel\\config.toml and the settings page opens. IBKR is included only if ib_async is installed
when you build.
"""
import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENTRY = ROOT / "scripts" / "_exe_entry.py"


def main() -> int:
    ENTRY.write_text(
        "import sys\n"
        "from livepanel.__main__ import main\n"
        "# double-click = open the window; any arguments are passed through as typed\n"
        "sys.exit(main(sys.argv[1:] or ['--window']))\n", encoding="utf-8")
    sep = ";" if sys.platform == "win32" else ":"
    args = [sys.executable, "-m", "PyInstaller", str(ENTRY), "--name", "LivePanel", "--onefile", "--noconsole",
            "--noconfirm", "--clean", "--icon", str(ROOT / "livepanel" / "web" / "icon.ico"),
            "--distpath", str(ROOT / "dist"), "--workpath", str(ROOT / "build"), "--specpath", str(ROOT / "build"),
            "--paths", str(ROOT),
            "--add-data", f"{ROOT / 'livepanel' / 'web'}{sep}livepanel/web",
            "--add-data", f"{ROOT / 'livepanel' / 'config.example.toml'}{sep}livepanel",
            "--hidden-import", "livepanel.brokers.moomoo", "--hidden-import", "livepanel.brokers.ibkr",
            "--hidden-import", "livepanel.settings", "--hidden-import", "livepanel.tray",
            "--hidden-import", "livepanel.updates"]
    # whatever else is installed in the building Python stays out: pandas / moomoo only import these optionally
    for m in ("torch", "torchvision", "torchaudio", "transformers", "triton", "sklearn", "scipy", "matplotlib",
              "tkinter", "_tkinter", "IPython", "jupyter", "notebook", "sqlalchemy", "lxml", "PIL", "cv2", "numba",
              "sympy", "tensorflow", "keras", "uvicorn", "fastapi", "soundfile", "pytest", "playwright", "pyarrow",
              "openpyxl", "bokeh", "plotly", "dask", "numexpr", "bottleneck", "tables", "h5py"):
        args += ["--exclude-module", m]
    if importlib.util.find_spec("moomoo"):
        args += ["--collect-all", "moomoo"]
    else:
        print("moomoo-api is not installed: the .exe will not connect to moomoo", file=sys.stderr)
    if importlib.util.find_spec("ib_async"):
        args += ["--collect-all", "ib_async"]
    r = subprocess.run(args, cwd=ROOT)
    ENTRY.unlink(missing_ok=True)
    if r.returncode == 0:
        print("built", ROOT / "dist" / "LivePanel.exe")
    return r.returncode


if __name__ == "__main__":
    sys.exit(main())
