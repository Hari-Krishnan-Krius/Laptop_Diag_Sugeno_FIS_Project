# LaptopDiag: Laptop Motherboard Diagnostics using Sugeno Fuzzy Inference System

> **A continuous, sensor-driven diagnostic platform for laptop motherboards, using Sugeno-style fuzzy inference engine to classify emerging hardware faults from live electrical and thermal telemetry.**

---

## Overview

LaptopDiag is a diagnostic platform that performs **real-time hardware monitoring** and **fault classification** for laptop motherboards.

Unlike conventional monitoring tools that only display raw sensor values, LaptopDiag combines **cross-platform sensor acquisition**, a **Sugeno fuzzy inference engine**, and a **web dashboard with automated alerting** to classify an emerging motherboard fault into one of eight actionable categories - before it produces a visible failure such as a crash, blue screen, or failure to boot.

The system continuously acquires eight hardware parameters from each monitored laptop, evaluates them through eight expert-calibrated fuzzy rules, classifies the machine's state with an associated confidence level and severity rating, stores historical diagnostic records in MongoDB, and presents the results through a centralised web dashboard with email alerting.

This project was developed as an MSc capstone (Artificial Intelligence, REVA Academy for Corporate Excellence, REVA University, 2026) and is intended for predictive maintenance research, IT fleet monitoring, and as a worked example of applying Sugeno fuzzy inference to a domain where labelled failure data is scarce.

---

## Research Motivation

Existing computer-hardware diagnostic tools and expert systems fall into two groups, and both have the same limitation:

- **Raw monitoring tools** (e.g. generic hardware monitors) display sensor values but do not interpret them - the user still has to know what a "healthy" voltage or temperature looks like.
- **Symptom-based expert systems** in the literature (case-based reasoning, if-then rule bases, NLP symptom matching) only begin diagnosis *after* a user or technician has already noticed and described a fault.

Neither approach reads live electrical/thermal sensor data continuously, and neither tolerates the sensor noise and partial sensor availability found on real laptops. LaptopDiag addresses this by fuzzifying live telemetry directly and reasoning over it continuously, so an emerging fault can be classified before it becomes a visible symptom.

---

## Objectives

1. Design and calibrate a Sugeno fuzzy inference engine that classifies eight motherboard fault conditions from eight live hardware parameters, across five laptop hardware categories.
2. Develop lightweight, dependency-free Windows and Linux monitoring agents that read sensor telemetry directly, without requiring the user to observe or report a symptom.
3. Build a web-based monitoring platform (Flask + MongoDB) with a background polling scheduler and automated email alerting, so diagnoses are actionable in real time.
4. Validate and refine the diagnostic engine's classification accuracy and confidence behaviour, in the absence of a public labelled dataset of real-world laptop motherboard failures.

---

## Key Features

### Intelligent Diagnosis
- Zero-order Sugeno (Takagi–Sugeno–Kang) fuzzy inference engine
- Eight expert-calibrated rules combining generalised-bell membership functions
- Category-aware calibration across five laptop classes (basic, midrange, high-end, gaming, workstation)
- Confidence scoring (Low / Medium / High) and severity mapping (OK / MEDIUM / HIGH / CRITICAL) per fault class

### Hardware Monitoring
Live acquisition of the eight parameters the fuzzy engine reasons over:
- CPU usage
- Fan RPM
- CPU temperature
- CPU voltage
- RAM voltage
- GPU voltage
- +3.3V power rail
- +5V power rail metric

Plus auxiliary system metrics shown on the dashboard (RAM usage, disk usage). Sensors are read via LibreHardwareMonitor or WMI on Windows and `lm-sensors` on Linux; any sensor unavailable on a given machine is reported as `None` rather than a fabricated value, and the engine falls back to an alternate detection path where one exists (e.g. the thermal-based fallback for Cooling Failure).

### Fault Classes
The engine classifies each reading into one of eight classes, each with a fixed severity level:

| Fault Class | Severity |
|---|---|
| Short Circuit | CRITICAL |
| VRM Failure | CRITICAL |
| GPU Failure | HIGH |
| BIOS Corrupt | HIGH |
| Cooling Failure | HIGH |
| RAM Fault | MEDIUM |
| PSU Issue | MEDIUM |
| Healthy System | OK |

### Dashboard
- Fleet overview (all registered laptops)
- Individual laptop dashboard
- Diagnostic history with severity filtering
- Intelligence tab (fuzzy rule / confidence insights)
- Settings page (SMTP + alert threshold configuration)

### Automated Diagnosis
- Manual, on-demand diagnosis
- Scheduled/continuous background polling per laptop
- "Diagnose now" trigger from the dashboard

### Historical Records
- Every diagnosis is stored (class, confidence, severity, raw input values, timestamp)
- Per-laptop trend queries via the stats API

### Notification System
- Severity-threshold email alerts with a cooldown to prevent spam
- Weekly fleet health digest

---

## System Architecture

```
                Windows / Linux Laptop
                (LaptopDiagAgent.ps1 / laptop_agent.sh)
                            |
                sensor read: LibreHardwareMonitor /
                     WMI / lm-sensors
                            |
                POST /api/agent/report
                            |
                 +----------------------+
                 |    Flask REST API    |
                 |       (app.py)       |
                 +----------+-----------+
                            |
              utils/fuzzy_engine.py
        (Sugeno fuzzy inference: fuzzify -> 8 rules
              -> weighted-average defuzzify)
                            |
                 MongoDB (laptops, diagnostics,
                     settings, scheduler_locks)
                            |
        +-------------------+-------------------+
        |                                       |
  Web Dashboard                    utils/email_notifier.py
  (Flask templates)                (severity alerts + weekly digest)
```

---

## Technology Stack

| Component | Technology |
|---|---|
| Backend | Python 3.10+ |
| Web Framework | Flask 3.0.3, Flask-PyMongo |
| Database | MongoDB (pymongo) |
| Frontend | HTML5, CSS3, JavaScript (Flask templates) |
| Decision Engine | Sugeno Fuzzy Inference System (NumPy) |
| Hardware Monitoring | LibreHardwareMonitor, WMI (Windows); lm-sensors (Linux) |
| Agents | PowerShell (`LaptopDiagAgent.ps1`), bash (`laptop_agent.sh`), optional Python agent |
| Alerting | smtplib (SMTP) |

---

## Project Structure

```
Code/
│
├── agent/
│   ├── laptop_agent.py
│   ├── LaptopDiagAgent.ps1
│   ├── laptop_agent.sh
│   └── laptop_agent.service
│
├── static/
├── templates/
│   ├── base.html
│   ├── index.html
│   ├── dashboard.html
│   ├── history.html
│   ├── intelligence.html
│   └── settings.html
│
├── utils/
│   ├── fuzzy_engine.py        # Sugeno FIS core
│   ├── system_monitor.py      # sensor acquisition
│   ├── scheduler.py           # background polling
│   └── email_notifier.py      # SMTP alerting
│
├── app.py                     # Flask app and REST API
├── run.py                     # startup script
├── debug_sensors.py
├── requirements.txt
├── env.example
└── bulk_import_template.csv
```

---

## Diagnostic Workflow

1. The agent reads the eight hardware sensor values on the monitored laptop.
2. The agent POSTs the reading to `/api/agent/report`.
3. Inputs are validated, clamped, and normalised against the laptop's category profile.
4. Inputs are fuzzified using generalised-bell membership functions.
5. The eight Sugeno rules are evaluated (fuzzy AND/OR over the membership degrees).
6. Rule outputs are combined by a Sugeno weighted average over one-hot class vectors.
7. The top two classes and a confidence score (top1 / (top1 + top2)) are produced.
8. Severity is looked up from the predicted class.
9. The diagnosis is stored in MongoDB, and an email alert is sent if severity is at or above the configured threshold (subject to a cooldown).
10. The dashboard reflects the new result in real time.

---

## Sugeno Fuzzy Inference System

The reasoning engine is a **zero-order Sugeno (Takagi–Sugeno–Kang) model**:

- **Fuzzification** - each of the eight inputs is fuzzified with generalised-bell membership functions, calibrated per laptop category.
- **Rule evaluation** - eight rules, one per fault class, combine memberships using fuzzy AND (product) or fuzzy OR (max); for example the Cooling Failure rule combines a direct fan-RPM path with a thermal fallback path for laptops that don't expose a fan sensor.
- **Defuzzification** - a weighted average over one-hot class vectors produces a score for all eight classes; the top two are reported as primary and secondary diagnosis.
- **Confidence** - computed as top1 / (top1 + top2), categorised as Low (< 0.60), Medium (0.60–0.80), or High (≥ 0.80).

On a 120-vector synthetic bench-test corpus spanning all eight fault classes and five laptop categories, the engine achieved 98.3% classification accuracy (see the accompanying thesis report, Chapter 10, for full methodology and results).

---

## REST API

### Pages
```
GET  /                                Fleet overview
GET  /dashboard/<laptop_id>           Individual laptop dashboard
GET  /history                         Diagnostic history
GET  /intelligence                    Fuzzy engine / confidence insights
GET  /settings                        SMTP and alert threshold settings
```

### Laptops
```
GET    /api/laptops
POST   /api/laptops
GET    /api/laptops/<id>
PUT    /api/laptops/<id>
DELETE /api/laptops/<id>
POST   /api/laptops/bulk-import
```

### Diagnosis
```
POST /api/diagnose
POST /api/laptops/<id>/diagnose-now
POST /api/laptops/<id>/request-report
GET  /api/history
GET  /api/stats/<id>
GET  /api/fleet/status
```

### Agent
```
POST /api/agent/register
POST /api/agent/report
POST /api/agent/check-pending
```

### System / Settings
```
GET  /api/system/metrics
GET  /api/debug/sensors
GET  /api/settings
POST /api/settings
POST /api/test-email
```

### Scheduler
```
POST /api/scheduler/start/<id>
POST /api/scheduler/stop/<id>
GET  /api/scheduler/status
```

---

## Installation

### Clone Repository

```bash
git clone <your-repository-url>
cd Code
```

> Replace `<your-repository-url>` with your actual repository URL before publishing this README.

### Install Dependencies

```bash
pip install -r requirements.txt
```

Windows-only and Linux-only optional extras (`wmi`, `pywin32`, `lm-sensors`) are listed as comments in `requirements.txt` - install the ones relevant to your platform.

### Configure Environment

```bash
cp env.example .env
```

Then edit `.env` with your MongoDB URI, Flask secret key, and (optionally) SMTP credentials for email alerts - SMTP can also be configured later from the Settings page in the UI.

### Start MongoDB

```bash
mongod
```

### Run the Application

```bash
python run.py
```

The server starts on `http://localhost:5000` by default.

### Start a Laptop Agent

On the machine you want to monitor:

```powershell
# Windows
agent\LaptopDiagAgent.ps1
```
```bash
# Linux / macOS
agent/laptop_agent.sh
```

---

## Research Contributions

- A zero-order Sugeno fuzzy inference engine for laptop motherboard fault classification, calibrated across five laptop hardware categories.
- A continuous, sensor-driven diagnostic architecture that removes the dependency on user-reported symptoms present in prior computer-hardware expert systems.
- A bench-test validation methodology for a diagnostic domain where no public labelled failure dataset exists.
- A deployable reference platform (agents, API, dashboard, alerting) demonstrating the approach end-to-end.

---

## Potential Applications

- IT support teams managing laptop fleets (computer labs, examination centres, shared workstations)
- Educational institutions and research into fuzzy expert systems
- Predictive maintenance for consumer electronics more broadly

---

## Future Enhancements

- Field validation against real-world failure logs (current validation is bench-test/synthetic)
- Refining the rule overlap identified between GPU Failure and Cooling Failure (both key on high CPU temperature)
- Adaptive Neuro-Fuzzy Inference System (ANFIS) for automatic rule/membership tuning
- Extending the fault taxonomy to desktop motherboards and multi-rail server power supplies
- Mobile companion app

---

## Citation

```
Hari Krishnan, "Laptop Motherboard Diagnostics using Sugeno Fuzzy Inference System,"
MSc Capstone Project, REVA Academy for Corporate Excellence, REVA University, 2026.
```

---

## License and Ownership

This project was completed as an academic capstone at REVA Academy for Corporate Excellence (RACE), REVA University. Per the project ownership terms accepted as part of the academic submission, the work product is the property of RACE, REVA University. Any reuse, redistribution, or commercial application beyond academic evaluation should be discussed with RACE prior to use.

---

## Author

**Hari Krishnan**

MSc Artificial Intelligence, REVA Academy for Corporate Excellence (RACE), REVA University - 2026

Research area: fuzzy logic systems, predictive maintenance, hardware diagnostics.

---

© 2026 Hari Krishnan. Academic project - REVA Academy for Corporate Excellence, REVA University.
