"""Start the local worker and ADK UI together without exposing credentials."""
import argparse
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Check local configuration without starting services")
    args = parser.parse_args()
    os.chdir(ROOT)
    from dotenv import load_dotenv
    load_dotenv(ROOT / "ranking_agent" / ".env", override=False)

    key = os.getenv("OPENAI_API_KEY", "").strip()
    model = os.getenv("AGENT_MODEL", "").strip()
    google_key = os.getenv("GOOGLE_API_KEY", "").strip()

    # Determine which key is needed based on the model
    is_gemini = model and not model.startswith("openai/")
    active_key = google_key if is_gemini else key
    key_name = "GOOGLE_API_KEY (Gemini)" if is_gemini else "OPENAI_API_KEY"

    ready = bool(active_key and model)
    print(f"{key_name}: " + ("configured (not yet validated)" if active_key else "missing"))
    print("Agent model: " + ("configured" if model else "missing"))
    if not ready:
        missing_key_url = "https://aistudio.google.com/apikey" if is_gemini else "https://platform.openai.com/api-keys"
        print(f"Enter {key_name} in ranking_agent/.env ({missing_key_url}), then run this command again.")
        return 2
    if args.check:
        return 0

    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", 8000))
        except OSError:
            print("Port 8000 is occupied. Stop the existing service before starting this launcher.")
            return 2
    scripts = Path(sys.executable).parent
    adk = scripts / ("adk.exe" if os.name == "nt" else "adk")
    worker = scripts / ("rank-agent.exe" if os.name == "nt" else "rank-agent")
    if not adk.is_file() or not worker.is_file():
        print("Run with the project's .venv Python after installing the project dependencies.")
        return 2

    logs = Path(os.getenv("RANK_DATA_DIR", "./data")) / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    os.environ["PYTHONUTF8"] = "1"
    python_bin = str(Path(sys.executable))
    processes = []
    try:
        with (logs / "worker.log").open("a", encoding="utf-8") as worker_log, \
             (logs / "adk.log").open("a", encoding="utf-8") as adk_log, \
             (logs / "custom_ui.log").open("a", encoding="utf-8") as ui_log:
            processes.append(subprocess.Popen([str(worker), "worker"], cwd=ROOT, stdout=worker_log,
                                               stderr=subprocess.STDOUT, creationflags=flags))
            processes.append(subprocess.Popen([str(adk), "web", "--host", "127.0.0.1", "--port", "8000", "ranking_agent"],
                                               cwd=ROOT, stdout=adk_log, stderr=subprocess.STDOUT,
                                               stdin=subprocess.DEVNULL, creationflags=flags))
            processes.append(subprocess.Popen([python_bin, "-m", "uvicorn", "ranking_agent.api:app", "--host", "127.0.0.1", "--port", "8080"],
                                               cwd=ROOT, stdout=ui_log, stderr=subprocess.STDOUT,
                                               stdin=subprocess.DEVNULL, creationflags=flags))
            print("==============================================================", flush=True)
            print("  CUSTOM DASHBOARD: http://127.0.0.1:8080", flush=True)
            print("  ADK DEBUG CHAT:   http://127.0.0.1:8000", flush=True)
            print("==============================================================", flush=True)
            print(f"Logs: {logs.resolve()}. Press Ctrl+C to stop all services.", flush=True)
            service_names = ["worker", "adk", "custom_ui"]
            while True:
                time.sleep(1)
                for i, proc in enumerate(processes):
                    if proc.poll() is not None:
                        print(
                            f"Service '{service_names[i]}' exited with code {proc.returncode}. "
                            f"Check {logs.resolve() / service_names[i]}.log for details."
                        )
                        return 1
    except KeyboardInterrupt:
        print("Stopping local services.")
        return 0
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


if __name__ == "__main__":
    raise SystemExit(main())
