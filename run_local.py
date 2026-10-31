"""One-click local runner for Plant Debugger.

Spawns both:
1. plant-sim on http://127.0.0.1:8081 (private hydraulic simulator)
2. plant-debugger on http://127.0.0.1:8080 (public Commander UI & Operator UI)
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

PYTHON_BIN = sys.executable

def main():
    print("=" * 70)
    print("  PLANT DEBUGGER — Agentic Field Partner for Brownfield Ops")
    print("=" * 70)
    print("Starting private plant-sim service on http://127.0.0.1:8081 ...")
    env_sim = os.environ.copy()
    env_sim["PORT"] = "8081"
    env_sim["ANOMALY_PUBLISHER"] = "http"
    env_sim["DEBUGGER_URL"] = "http://127.0.0.1:8080"

    sim_proc = subprocess.Popen(
        [PYTHON_BIN, "-m", "uvicorn", "plant_sim.main:app", "--host", "127.0.0.1", "--port", "8081"],
        env=env_sim,
    )

    print("Starting plant-debugger on http://127.0.0.1:8080 ...")
    env_dbg = os.environ.copy()
    env_dbg["PORT"] = "8080"
    env_dbg["SIM_URL"] = "http://127.0.0.1:8081"

    dbg_proc = subprocess.Popen(
        [PYTHON_BIN, "-m", "uvicorn", "plant_debugger.main:app", "--host", "127.0.0.1", "--port", "8080"],
        env=env_dbg,
    )

    time.sleep(4.0)

    print("\n" + "=" * 70)
    print("  SERVICES RUNNING:")
    print("  - Operations Commander UI:  http://127.0.0.1:8080/")
    print("  - Mobile Operator UI:      http://127.0.0.1:8080/operator")
    print("  - Plant Simulator API:     http://127.0.0.1:8081/healthz")
    print("=" * 70)
    print("Press Ctrl+C to terminate both services.\n")

    try:
        dbg_proc.wait()
    except KeyboardInterrupt:
        print("\nShutting down Plant Debugger...")
    finally:
        dbg_proc.terminate()
        sim_proc.terminate()


if __name__ == "__main__":
    main()
