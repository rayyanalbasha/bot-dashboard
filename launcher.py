"""
launcher.py

Wispbyte's Python egg always runs `python {PY_FILE}` — it has no bash step,
so start.sh (which does `python sec.py &` / `uvicorn ... &`) can't run there
directly. This script does the same job in pure Python: start both the bot
(sec.py) and the FastAPI dashboard (main.py via uvicorn) as subprocesses,
and if either one dies, kill the other and exit so Wispbyte restarts the
whole thing cleanly.

Set this as your Wispbyte "Main file" (PY_FILE): launcher.py
"""

import os
import subprocess
import sys
import time

def ensure_requirements(script_dir: str):
    """
    Don't rely on Wispbyte's PY_PACKAGES / REQUIREMENTS_FILE startup
    variables actually being applied -- install requirements.txt ourselves,
    every boot, using the same --prefix .local location the platform's own
    install step uses (so this reuses/extends what's already there instead
    of creating a second, conflicting install location).
    """
    req_path = os.path.join(script_dir, "requirements.txt")
    if not os.path.exists(req_path):
        print(f"[launcher] WARNING: no requirements.txt at {req_path}, skipping install.")
        return

    print("[launcher] Installing requirements.txt ...")
    # Use an ABSOLUTE prefix matching the container's actual home directory
    # (~/.local), not a path relative to script_dir. Wispbyte's own install
    # step runs `pip install --prefix .local` from /home/container, so that's
    # where Python's user-site actually looks -- if we install relative to
    # WebPanel/ instead, packages land somewhere Python never checks.
    local_prefix = os.path.join(os.path.expanduser("~"), ".local")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--prefix", local_prefix, "-r", req_path],
    )
    if result.returncode != 0:
        print(f"[launcher] WARNING: pip install exited with code {result.returncode}")


def main():
    # Always work relative to where launcher.py itself actually lives, so
    # this doesn't depend on what the current working directory happens to
    # be when Wispbyte invokes `python /home/container/${PY_FILE}`.
    script_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(script_dir)

    print(f"[launcher] Running from: {script_dir}")
    print(f"[launcher] Files here: {sorted(os.listdir(script_dir))}")

    for required in ("sec.py", "main.py", "shared_config.py", "requirements.txt"):
        if not os.path.exists(required):
            print(f"[launcher] WARNING: expected '{required}' next to launcher.py but it's missing.")

    ensure_requirements(script_dir)

    port = os.getenv("PORT", "8000")

    # Diagnostic: confirm what THIS process (the direct parent of both
    # sec.py and uvicorn) actually sees, before spawning anything. If this
    # prints False, the problem is upstream of us (the platform hasn't
    # injected the variable into this process's environment at all). If it
    # prints True but sec.py still complains, something inside sec.py's own
    # process is different -- which is why we also now pass env= explicitly
    # below instead of relying on implicit inheritance.
    print(f"[launcher] BOT_TOKEN visible to launcher.py: {bool(os.getenv('BOT_TOKEN'))}")

    child_env = os.environ.copy()

    bot_proc = subprocess.Popen([sys.executable, "sec.py"], env=child_env)
    web_proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", port],
        env=child_env,
    )

    procs = {"bot (sec.py)": bot_proc, "dashboard (main.py)": web_proc}

    try:
        while True:
            for name, proc in procs.items():
                ret = proc.poll()
                if ret is not None:
                    print(f"[launcher] {name} exited with code {ret}; shutting down the other process.")
                    for other_name, other_proc in procs.items():
                        if other_proc.poll() is None:
                            other_proc.terminate()
                    sys.exit(ret if ret else 1)
            time.sleep(2)
    except KeyboardInterrupt:
        for proc in procs.values():
            if proc.poll() is None:
                proc.terminate()


if __name__ == "__main__":
    main()