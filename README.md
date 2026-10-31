# Plant Debugger — Agentic Field Partner for Brownfield Ops

Plant Debugger is an agentic troubleshooting system for brownfield industrial facilities where the "last meter has no API."

When central telemetry detects an anomaly, the observations and physical adjustments required to resolve it (analog visual differential-pressure gauges, manual bypass handwheels, physical access constraints) remain outside the digital API boundary. Plant Debugger pairs **Google ADK + Gemini** to dynamically plan and re-plan diagnostic steps with the **Field Operator** as the physical reach and **deterministic code** as the immutable authority for safety bounds and recovery verification.

---

## 1. Collaborative Loop & Authority Split

| Responsibility | Authority / Owner | Mechanism |
|---|---|---|
| **Diagnostic Objective & Dynamic Planning** | **Google ADK + Gemini** | Evaluates telemetry & evidence; dispatches physical field tasks via `LongRunningFunctionTool` (`RequestInput -> PAUSE`), resumes on field evidence (`FunctionResponse -> RESUME`). |
| **Physical Reach (Eyes & Hands)** | **Field Operator** | Local visual inspection, analog gauge capture, bounded manual adjustments, final-position evidence, explicit Site Knowledge. |
| **Safety Bounds & Budgets** | **Deterministic `ActionGuard`** | Enforces capability allow-list, manual adjustment delta limits (e.g. ±25% step bound), turn/action budgets, idempotency keys, and final-position photo verification policy. |
| **Resolution Authority** | **Deterministic `RecoveryVerifier`** | **Gemini never receives authority to declare `RESOLVED` on its own.** Sustained healthy telemetry (FT-101 flow within 90–115 m³/h) over a continuous window proves recovery. |
| **Long-Term Memory** | **Operator-Authored Site Knowledge** | Only explicit human assertions become durable plant facts. Model inferences and temporary evidence are never silently promoted. |

---

## 2. Architecture & Components

```
                +-------------------------------------------------------------+
                |                    USERS & PLANT EDGE                       |
                |  Commander UI (Desktop)          Mobile Operator UI (Phone) |
                +------------------------------+------------------------------+
                                               |
                                               v
+-----------------------------------------------------------------------------------------+
|                  PUBLIC APPLICATION LAYER (Cloud Run: plant-debugger)                   |
|                                                                                         |
|   +-----------------------+     +------------------------+     +--------------------+   |
|   | FastAPI REST Server   |     |  IncidentOrchestrator  |     |  AdkWorkflowHost   |   |
|   | - /api/incidents      | <-> |  - Coordinates loop    | <-> |  - ADK Runner      |   |
|   | - /api/tasks          |     |  - Pause / Resume      |     |  - Gemini 2.5      |   |
|   | - /api/knowledge      |     +-----------+------------+     |  - LRO Tools       |   |
|   +-----------------------+                 |                  +--------------------+   |
|                                             v                                           |
|   +-----------------------------------------+---------------------------------------+   |
|   |                          CONTROL & SAFETY BOUNDARY                              |   |
|   |   ActionGuard: Bounded deltas, action budgets, idempotency, final photo check   |   |
|   |   RecoveryVerifier: Deterministic flow window evaluation for RESOLVED           |   |
|   +-----------------------------------------+---------------------------------------+   |
|                                             |                                           |
|       +-------------------------------------+-----------------------------------+       |
|       |                                                                         |       |
|       v                                                                         v       |
|  [State Backend]                                                           [Vision]     |
|  - JSON (Local dev) / Firestore (Cloud)                                    - Local (sim)|
|  [Evidence Storage]                                                        - Gemini 2.5 |
|  - Local disk / GCS with signed URLs + SHA-256                             (Multimodal) |
+-----------------------------------------------------------------------------------------+
                                               ^
                                               | Telemetry / Idempotent Actions
                                               v
+-----------------------------------------------------------------------------------------+
|                    PRIVATE SIMULATION LAYER (Cloud Run: plant-sim)                      |
|                                                                                         |
|   - First-order hydraulic physics (P-101 pump curve, F-17 strainer, BV-104 bypass)      |
|   - Hidden fault injection (clogging, wear) isolated from planner                       |
|   - Synthetic field camera rendering for analog gauges & valve position flags           |
|   - Pub/Sub anomaly publisher                                                           |
+-----------------------------------------------------------------------------------------+
```

---

## 3. Reference Troubleshooting Run

1. **Anomaly**: Strainer F-17 clogs in `plant-sim`. Flow meter FT-101 reports `< 85 m³/h` while pump P-101 is running.
2. **Context Evaluation**: ADK evaluates streaming telemetry and determines visual inspection of F-17's local differential-pressure gauge provides highest information value.
3. **Field Task Dispatched (PAUSE)**: Operator receives work order on phone: *"Capture one readable photo of F-17 local ΔP indicator."*
4. **Multimodal Evidence**: Operator uploads gauge photo. Gemini Vision classifies ΔP as `HIGH`.
5. **Re-planning (RESUME)**: ADK resumes, identifies F-17 fouling, and selects manual bypass valve BV-104.
6. **Bounded Experimentation (PAUSE)**: ADK issues a safe +10% probe. ActionGuard verifies delta (`10% <= 25%` limit).
7. **Action Confirmation**: Operator opens BV-104 by +10%. Simulator reflects movement; flow partially recovers to ~72 m³/h.
8. **Incomplete Recovery Reasoning (RESUME)**: ADK notes partial recovery and orders BV-104 to `FULL_OPEN`.
9. **Final-Position Verification Policy**: ActionGuard mandates a physical end-state photo for 100% OPEN.
10. **Visual Completion Evidence**: Gemini Vision verifies pointer at the mechanical OPEN stop mark.
11. **Deterministic Recovery Verification**: RecoveryVerifier monitors FT-101 flow in the 90–115 m³/h band for a sustained window. Once verified, the incident is marked **`RESOLVED`**.

---

## 4. Quick Start: Local Run

### Prerequisites
- Python 3.10+ (tested with 3.13 / 3.14)

### Setup & Launch
```powershell
# 1. Create and activate virtual environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 2. Install dependencies
pip install -r requirements.txt

# 3. Launch both services locally with one command
python run_local.py
```

### Access UIs
- **Operations Commander**: [http://127.0.0.1:8080/](http://127.0.0.1:8080/)
  - Live P&ID schematic with dynamic overlays
  - Real-time ADK Decision Trace (`NEW EVIDENCE -> DIAGNOSIS UPDATE -> NEXT FIELD TASK`)
  - Immutable SHA-256 Audit Trail
- **Field Operator Mobile Companion**: [http://127.0.0.1:8080/operator](http://127.0.0.1:8080/operator)
  - Work orders with bounded steps, safety notes, and access guidance
  - Camera upload & simulation photo shortcuts
  - "Teach this plant to the agent" form

---

## 5. Automated Tests

Run the complete test suite (unit tests for ActionGuard, RecoveryVerifier, Site Knowledge adaptation, and the full end-to-end loop):

```powershell
.\.venv\Scripts\python.exe -m pytest tests -v
```

All 11 tests pass out of the box with zero external dependencies.

---

## 6. Google Cloud Deployment (Submission Mode)

In submission mode, Plant Debugger runs fail-closed with zero silent fallbacks:

```bash
DECISION_ENGINE=adk
ADK_FALLBACK_POLICY=false
ADK_SESSION_BACKEND=firestore
VISION_ENGINE=gemini
VISION_FALLBACK_POLICY=false
FIELD_STATE_BACKEND=firestore
FIELD_EVIDENCE_BACKEND=gcs
```

To deploy to Google Cloud Run, Firestore, and Pub/Sub:

```bash
# Set your GCP Project ID
export GOOGLE_CLOUD_PROJECT="your-project-id"

# Run automated deployment
chmod +x deploy/deploy_gcp.sh
./deploy/deploy_gcp.sh
```

