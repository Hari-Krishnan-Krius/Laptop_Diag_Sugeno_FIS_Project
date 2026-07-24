"""
app.py
─────────────────────────────────────────────────────────────────────────────
Laptop Motherboard Diagnostics System
Sugeno Fuzzy Inference System
Laptop-Motherboard-Diagnostics © 2026

Routes
  GET  /                         → index.html  (fleet overview)
  GET  /dashboard/<laptop_id>    → dashboard.html (per-laptop live view)
  GET  /history                  → history.html
  GET  /settings                 → settings.html

API Endpoints
  GET    /api/laptops                     List all active laptops
  POST   /api/laptops                     Register a new laptop
  GET    /api/laptops/<id>                Get laptop details
  PUT    /api/laptops/<id>                Update laptop configuration
  DELETE /api/laptops/<id>                Soft-deactivate a laptop
  POST   /api/diagnose                    Run diagnosis (manual or auto)
  GET    /api/history                     Paginated diagnostics history
  GET    /api/stats/<id>                  Trend + distribution data
  GET    /api/system/metrics              Live OS metrics
  POST   /api/scheduler/start/<id>        Start polling scheduler
  POST   /api/scheduler/stop/<id>         Stop polling scheduler
  GET    /api/scheduler/status            All scheduler states
  GET    /api/settings                    Read global settings
  POST   /api/settings                    Save global settings
─────────────────────────────────────────────────────────────────────────────
"""

import os
import platform
import logging
import utils.email_notifier as email_notifier
from datetime import datetime, timezone

from bson       import ObjectId
from bson.errors import InvalidId
from dotenv     import load_dotenv
from flask      import Flask, render_template, jsonify, request, abort
from flask_pymongo import PyMongo

# ── Load environment variables ────────────────────────────────────────────────
load_dotenv()

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
log = logging.getLogger(__name__)

# ── Flask + MongoDB setup ─────────────────────────────────────────────────────
app = Flask(__name__, template_folder="templates", static_folder="static", static_url_path="/static")
app.secret_key = os.environ.get("SECRET_KEY", "change_me_to_a_long_random_string")

_mongo_uri = os.environ.get(
    "MONGO_URI", "mongodb://localhost:27017/laptop_diagnostics"
)
# Append a short server-selection timeout so a down/unreachable MongoDB
# fails fast (3s) instead of hanging on every request for pymongo's
# default 30s timeout. This only affects connection behaviour, not queries.
if "serverSelectionTimeoutMS" not in _mongo_uri:
    sep = "&" if "?" in _mongo_uri else "?"
    _mongo_uri = f"{_mongo_uri}{sep}serverSelectionTimeoutMS=3000"

app.config["MONGO_URI"] = _mongo_uri

mongo = PyMongo(app)
db    = mongo.db          # shorthand — used throughout


def _check_mongo_connection() -> bool:
    """Ping MongoDB once at startup and print a clear status line."""
    try:
        mongo.cx.admin.command("ping")
        log.info("✅ MongoDB connection OK (%s)", _mongo_uri.split("@")[-1])
        return True
    except Exception as exc:
        log.error("❌ MongoDB connection FAILED: %s", exc)
        log.error(
            "   The app will still start, but every page will show errors "
            "until MongoDB is running. Start it with: mongosh / "
            "sudo systemctl start mongod / brew services start mongodb-community"
        )
        return False

# ── Lazy imports (avoid circular at startup) ──────────────────────────────────
from utils.fuzzy_engine    import (
    get_diagnostics_sugeno, categorize_confidence, SEVERITY_MAP, CLASSES,
    CATEGORY_PROFILES,
)
from utils.system_monitor  import get_system_metrics, get_hardware_identity
from utils.hardware_specs  import resolve_calibration_profile
from utils.email_notifier  import maybe_send_fault_alert
from utils import scheduler as sched
from utils.scheduler import _validate_inputs


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _oid(raw: str) -> ObjectId:
    """Convert a string to ObjectId, raising 400 on invalid format."""
    try:
        return ObjectId(raw)
    except (InvalidId, TypeError):
        abort(400, f"Invalid id: {raw}")


def _serialize(doc: dict) -> dict:
    """
    Recursively convert MongoDB ObjectIds and datetimes to JSON-serialisable
    types. ObjectId → str, datetime → ISO-8601 str.
    """
    out = {}
    for k, v in doc.items():
        if isinstance(v, ObjectId):
            out[k] = str(v)
        elif isinstance(v, datetime):
            out[k] = v.isoformat()
        elif isinstance(v, dict):
            out[k] = _serialize(v)
        elif isinstance(v, list):
            out[k] = [
                _serialize(i) if isinstance(i, dict)
                else (str(i) if isinstance(i, ObjectId) else i)
                for i in v
            ]
        else:
            out[k] = v
    return out


def _safe_float(val, default=0.0) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _maybe_recompute_profile(laptop: dict, identity: dict) -> dict:
    """
    Resolve (and, if needed, persist) this laptop's hardware-specific
    calibration profile — see utils/hardware_specs.py.

    Auto-recompute policy (per project decision): the resolved profile is
    recomputed whenever either of the following changes since it was last
    computed, rather than only once at registration:
      - the laptop's category (e.g. a technician corrects a wrong bucket)
      - the identified cpu_model / ram_type / gpu_model (e.g. a RAM upgrade,
        or a first successful identification after an earlier report where
        LHM hadn't warmed up yet and these fields were still empty)

    `identity` is whatever cpu_model/ram_type/gpu_model the caller has
    available for *this* request (may be partially or fully empty — older
    agents, or a platform where a given field isn't resolvable, send "").
    Empty incoming fields never overwrite a previously-resolved value with
    a blank — they simply aren't compared/updated, so a laptop's profile
    only ever gets more specific over time, never regresses.

    Returns the resolved profile dict to pass into get_diagnostics_sugeno()
    as profile_override.
    """
    category = laptop.get("category", "midrange")
    stored_identity = {
        "cpu_model":    laptop.get("cpu_model", ""),
        "ram_type":     laptop.get("ram_type", ""),
        "gpu_model":    laptop.get("gpu_model", ""),
        "system_model": laptop.get("model", ""),
    }

    # Merge: incoming non-empty fields win; empty incoming fields keep
    # whatever was already stored (never regress to unknown).
    merged_identity = dict(stored_identity)
    for key, incoming_key in (("cpu_model", "cpu_model"), ("ram_type", "ram_type"),
                               ("gpu_model", "gpu_model")):
        if identity.get(incoming_key):
            merged_identity[key] = identity[incoming_key]

    identity_changed = merged_identity != stored_identity
    category_changed = laptop.get("_profile_category") != category
    profile_missing   = "resolved_profile" not in laptop

    if not (identity_changed or category_changed or profile_missing):
        return laptop["resolved_profile"]

    category_defaults = CATEGORY_PROFILES.get(category, CATEGORY_PROFILES["midrange"])
    resolved = resolve_calibration_profile(
        category_defaults,
        cpu_model=merged_identity["cpu_model"],
        ram_type=merged_identity["ram_type"],
        gpu_model=merged_identity["gpu_model"],
        system_model=merged_identity["system_model"],
    )

    update_fields = {
        "resolved_profile":   resolved,
        "_profile_category":  category,
        "cpu_model":          merged_identity["cpu_model"],
        "ram_type":           merged_identity["ram_type"],
        "gpu_model":          merged_identity["gpu_model"],
    }
    db.laptops.update_one({"_id": laptop["_id"]}, {"$set": update_fields})
    log.info(
        "Calibration profile recomputed for '%s' (category=%s, identity_changed=%s, "
        "category_changed=%s): sources=%s",
        laptop.get("name"), category, identity_changed, category_changed,
        resolved.get("_sources"),
    )
    return resolved


# ─────────────────────────────────────────────────────────────────────────────
# Page Routes
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/dashboard/<laptop_id>")
def dashboard(laptop_id: str):
    laptop = db.laptops.find_one({"_id": _oid(laptop_id), "active": True})
    if not laptop:
        abort(404, "Laptop not found")
    return render_template("dashboard.html", laptop=_serialize(laptop))


@app.route("/history")
def history():
    return render_template("history.html")


@app.route("/intelligence")
def intelligence():
    return render_template("intelligence.html")


@app.route("/settings")
def settings():
    return render_template("settings.html")


# ─────────────────────────────────────────────────────────────────────────────
# API — Laptops
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/laptops", methods=["GET"])
def api_get_laptops():
    """Return all active laptops sorted by name, with a fleet health summary."""
    laptops = list(
        db.laptops.find({"active": True}).sort("name", 1)
    )
    local_count = sum(1 for l in laptops if l.get("is_local"))
    response = {
        "laptops": [_serialize(l) for l in laptops],
        "meta": {
            "total":       len(laptops),
            "local_count": local_count,
        },
    }
    if local_count > 1:
        response["meta"]["warning"] = (
            f"{local_count} laptops are marked is_local=True. They all share "
            "the host machine's metrics. For fleet monitoring of separate "
            "physical machines, set is_local=False and supply metrics via "
            "POST /api/diagnose."
        )
    return jsonify(response)


@app.route("/api/laptops", methods=["POST"])
def api_add_laptop():
    """Register a new laptop."""
    data = request.get_json(force=True) or {}

    name  = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip()
    if not name or not email:
        return jsonify({"error": "name and email are required"}), 400

    valid_categories = {"basic", "midrange", "highend", "gaming", "workstation"}
    category = data.get("category", "midrange").lower()
    if category not in valid_categories:
        category = "midrange"

    doc = {
        "name":             name,
        "category":         category,
        "model":            (data.get("model") or "").strip(),
        "cpu_model":        (data.get("cpu_model") or "").strip(),
        "ram_type":         (data.get("ram_type") or "").strip(),
        "gpu_model":        (data.get("gpu_model") or "").strip(),
        "email":            email,
        "polling_interval": max(10, int(data.get("polling_interval", 60))),
        "is_local":         bool(data.get("is_local", True)),
        "notify_email":     bool(data.get("notify_email", True)),
        "active":           True,
        "created_at":       datetime.now(timezone.utc),
        "last_checked":     None,
        "last_status":      None,
        "last_severity":    None,
    }

    # Warn if more than one is_local laptop is being registered.
    # All is_local laptops receive identical metrics from the host running
    # Flask. This is fine if each laptop runs its own instance of this app,
    # but will produce duplicate/identical diagnostics if multiple local
    # laptops are registered against a single Flask server.
    if doc["is_local"]:
        existing_local_count = db.laptops.count_documents(
            {"is_local": True, "active": True}
        )
        if existing_local_count >= 1:
            log.warning(
                "Registering '%s' as is_local=True but %d local laptop(s) already "
                "exist. All local laptops share the same host metrics. If this "
                "server monitors multiple physical machines, set is_local=False and "
                "supply metrics manually via POST /api/diagnose.",
                name, existing_local_count,
            )

    result  = db.laptops.insert_one(doc)
    doc["_id"] = result.inserted_id

    # Auto-start scheduler if this is a local laptop
    if doc["is_local"]:
        sched.start_scheduler(app, db, str(result.inserted_id))

    log.info("Laptop registered: %s (%s)", name, category)
    resp = _serialize(doc)

    # Surface the warning to the caller so the UI can display it
    existing_local = db.laptops.count_documents({"is_local": True, "active": True})
    if doc["is_local"] and existing_local > 1:
        resp["warning"] = (
            f"{existing_local} laptops are marked is_local=True. They will all "
            "receive identical diagnostics from this host. See server logs for details."
        )
    return jsonify(resp), 201


@app.route("/api/laptops/<laptop_id>", methods=["GET"])
def api_get_laptop(laptop_id: str):
    laptop = db.laptops.find_one({"_id": _oid(laptop_id), "active": True})
    if not laptop:
        return jsonify({"error": "Not found"}), 404
    return jsonify(_serialize(laptop))


@app.route("/api/laptops/<laptop_id>", methods=["PUT"])
def api_update_laptop(laptop_id: str):
    """Update mutable laptop fields."""
    data = request.get_json(force=True) or {}
    allowed = {
        "name", "category", "model", "email",
        "polling_interval", "is_local", "notify_email",
    }
    update = {k: v for k, v in data.items() if k in allowed}
    if not update:
        return jsonify({"error": "No valid fields to update"}), 400

    # If polling_interval changed, restart scheduler
    restart = "polling_interval" in update or "is_local" in update

    db.laptops.update_one({"_id": _oid(laptop_id)}, {"$set": update})

    # Warn if is_local is being set to True when other local laptops exist
    if update.get("is_local") is True:
        existing_local = db.laptops.count_documents(
            {"is_local": True, "active": True, "_id": {"$ne": _oid(laptop_id)}}
        )
        if existing_local >= 1:
            log.warning(
                "Laptop %s updated to is_local=True but %d other local laptop(s) "
                "already exist. All local laptops share the same host metrics.",
                laptop_id, existing_local,
            )

    if restart:
        sched.stop_scheduler(laptop_id)
        laptop = db.laptops.find_one({"_id": _oid(laptop_id)})
        if laptop and laptop.get("is_local") and laptop.get("active"):
            sched.start_scheduler(app, db, laptop_id)

    resp = {"ok": True}
    if update.get("is_local") is True:
        total_local = db.laptops.count_documents({"is_local": True, "active": True})
        if total_local > 1:
            resp["warning"] = (
                f"{total_local} laptops are now marked is_local=True and will all "
                "receive identical host metrics."
            )
    return jsonify(resp)


@app.route("/api/laptops/<laptop_id>", methods=["DELETE"])
def api_delete_laptop(laptop_id: str):
    """Soft-delete (deactivate) a laptop."""
    sched.stop_scheduler(laptop_id)
    db.laptops.update_one(
        {"_id": _oid(laptop_id)},
        {"$set": {"active": False}},
    )
    log.info("Laptop deactivated: %s", laptop_id)
    return jsonify({"ok": True})


# ─────────────────────────────────────────────────────────────────────────────
# API — Diagnosis
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/diagnose", methods=["POST"])
def api_diagnose():
    """
    Run a Sugeno FIS diagnosis.

    Accepts JSON body:
      laptop_id    (optional) — link result to a registered laptop
      source       "manual" | "auto"   default "manual"
      category     laptop category — used if no laptop_id provided

      For manual / quick diagnosis — all 8 inputs required:
        cpu_usage, fan_rpm, cpu_temp, cpu_voltage,
        ram_voltage, gpu_voltage, rail_3v3, rail_5v_mw

      For auto diagnosis (is_local laptop):
        inputs are read from the OS automatically; any supplied values
        are ignored.
    """
    data      = request.get_json(force=True) or {}
    source    = data.get("source", "manual")
    laptop_id = data.get("laptop_id") or None
    laptop    = None
    category  = data.get("category", "midrange")
    profile_override = None

    # Resolve laptop document
    if laptop_id:
        laptop = db.laptops.find_one({"_id": _oid(laptop_id), "active": True})
        if not laptop:
            return jsonify({"error": "Laptop not found"}), 404
        category = laptop.get("category", "midrange")

    # Gather inputs
    if source == "auto" and laptop and laptop.get("is_local"):
        metrics = get_system_metrics()
        inputs = [
            metrics["cpu_usage"],
            metrics["fan_rpm"],
            metrics["cpu_temp"],
            metrics["cpu_voltage"],
            metrics["ram_voltage"],
            metrics["gpu_voltage"],
            metrics["rail_3v3"],
            metrics["rail_5v_mw"],
        ]
        identity = get_hardware_identity()
        profile_override = _maybe_recompute_profile(laptop, identity)
    elif source == "auto" and laptop and not laptop.get("is_local"):
        # Previously this silently fell through to the manual branch below
        # and fabricated default values (50% CPU, 2500 RPM, etc.) for any
        # field not explicitly supplied — producing a fake diagnosis that
        # looked real and was stored in history. Fixed: for a real remote
        # (agent-monitored) laptop, "auto" must use its last known genuine
        # agent reading, exactly like /api/laptops/<id>/diagnose-now does.
        # If no real reading exists yet, fail clearly instead of guessing.
        last_record = db.diagnostics.find_one(
            {"laptop_id": _oid(laptop_id), "source": "agent"},
            sort=[("timestamp", -1)],
        )
        if not last_record or not last_record.get("metrics"):
            return jsonify({
                "error": "No agent data received yet for this laptop. "
                         "Make sure the agent is installed and running on the target "
                         "machine, or supply source=\"manual\" with explicit input values."
            }), 409
        metrics = last_record["metrics"]
        inputs = _validate_inputs([
            _safe_float(metrics.get("cpu_usage"),   50.0),
            _safe_float(metrics.get("fan_rpm"),     2500.0),
            _safe_float(metrics.get("cpu_temp"),    60.0),
            _safe_float(metrics.get("cpu_voltage"), 1.20),
            _safe_float(metrics.get("ram_voltage"), 1.25),
            _safe_float(metrics.get("gpu_voltage"), 1.00),
            _safe_float(metrics.get("rail_3v3"),    3.30),
            _safe_float(metrics.get("rail_5v_mw"),  500.0),
        ])
        source = "auto_remote"
        profile_override = _maybe_recompute_profile(laptop, {
            "cpu_model": metrics.get("cpu_model", ""),
            "ram_type":  metrics.get("ram_type", ""),
            "gpu_model": metrics.get("gpu_model", ""),
        })
    else:
        source = "manual"   # explicit manual request, or no laptop_id supplied at all
        inputs = _validate_inputs([
            _safe_float(data.get("cpu_usage"),   50.0),
            _safe_float(data.get("fan_rpm"),     2500.0),
            _safe_float(data.get("cpu_temp"),    60.0),
            _safe_float(data.get("cpu_voltage"), 1.20),
            _safe_float(data.get("ram_voltage"), 1.25),
            _safe_float(data.get("gpu_voltage"), 1.00),
            _safe_float(data.get("rail_3v3"),    3.30),
            _safe_float(data.get("rail_5v_mw"),  500.0),
        ])
        metrics = {
            "cpu_usage":    inputs[0],
            "fan_rpm":      inputs[1],
            "cpu_temp":     inputs[2],
            "cpu_voltage":  inputs[3],
            "ram_voltage":  inputs[4],
            "gpu_voltage":  inputs[5],
            "rail_3v3":     inputs[6],
            "rail_5v_mw":   inputs[7],
        }
        if laptop:
            profile_override = _maybe_recompute_profile(laptop, {
                "cpu_model": laptop.get("cpu_model", ""),
                "ram_type":  laptop.get("ram_type", ""),
                "gpu_model": laptop.get("gpu_model", ""),
            })

    # Run Sugeno FIS
    diagnosis, secondary, confidence, weights = get_diagnostics_sugeno(
        inputs, category=category, profile_override=profile_override
    )
    conf_level = categorize_confidence(confidence)
    severity   = SEVERITY_MAP.get(diagnosis, "OK")
    ts         = datetime.now(timezone.utc)

    record = {
        "laptop_id":    _oid(laptop_id) if laptop_id else None,
        "laptop_name":  laptop.get("name", "") if laptop else "",
        "timestamp":    ts,
        "source":       source,
        "category":     category,
        "diagnosis":    diagnosis,
        "secondary":    secondary,
        "confidence":   round(confidence, 6),
        "conf_level":   conf_level,
        "severity":     severity,
        "rule_weights": [round(w, 6) for w in weights],
        "metrics":      {k: (round(float(v), 4) if isinstance(v, (int, float))
                              else (v if isinstance(v, str) and len(v) < 100 else None))
                         for k, v in metrics.items()
                         if not isinstance(v, dict)},
        "notified":     False,
    }

    inserted = db.diagnostics.insert_one(record)

    # Update laptop last-checked fields
    if laptop_id and laptop:
        db.laptops.update_one(
            {"_id": _oid(laptop_id)},
            {"$set": {
                "last_checked":  ts,
                "last_status":   diagnosis,
                "last_severity": severity,
            }},
        )

    # Email notification
    notified = False
    if laptop and laptop.get("notify_email", True):
        notified = maybe_send_fault_alert(db, laptop, {
            "diagnosis":  diagnosis,
            "secondary":  secondary,
            "confidence": confidence,
            "conf_level": conf_level,
            "severity":   severity,
            "timestamp":  ts.isoformat(),
        })
        if notified:
            db.diagnostics.update_one(
                {"_id": inserted.inserted_id},
                {"$set": {"notified": True}},
            )

    response = {
        "diagnosis":    diagnosis,
        "secondary":    secondary,
        "confidence":   round(confidence, 6),
        "conf_level":   conf_level,
        "severity":     severity,
        "rule_weights": [round(w, 6) for w in weights],
        "notified":     notified,
        "source":       source,
        "timestamp":    ts.isoformat(),
        "record_id":    str(inserted.inserted_id),
    }
    log.info(
        "Diagnosis [%s|%s]: %s (%.1f%%) sev=%s notified=%s",
        source, category, diagnosis, confidence * 100, severity, notified,
    )
    return jsonify(response)


# ─────────────────────────────────────────────────────────────────────────────
# API — History
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/history", methods=["GET"])
def api_history():
    """
    Query parameters:
      laptop_id  filter by laptop _id
      diagnosis  filter by diagnosis label
      severity   filter by severity (OK / LOW / MEDIUM / HIGH / CRITICAL)
      source     filter by source (agent / auto / manual)
      limit      default 25, max 200
      page       default 1
    """
    laptop_id = request.args.get("laptop_id", "")
    diagnosis = request.args.get("diagnosis", "")
    severity  = request.args.get("severity",  "")
    source    = request.args.get("source",    "")
    limit     = min(int(request.args.get("limit", 25)), 200)
    page      = max(int(request.args.get("page",  1)), 1)
    skip      = (page - 1) * limit

    query = {}
    if laptop_id:
        try:
            query["laptop_id"] = _oid(laptop_id)
        except Exception:
            pass
    if diagnosis:
        query["diagnosis"] = diagnosis
    if severity:
        query["severity"] = severity
    if source:
        # "manual" matches manual, manual_local, manual_remote
        if source == "manual":
            query["source"] = {"$in": ["manual", "manual_local", "manual_remote"]}
        else:
            query["source"] = source

    total   = db.diagnostics.count_documents(query)
    records = list(
        db.diagnostics.find(query)
        .sort("timestamp", -1)
        .skip(skip)
        .limit(limit)
    )

    return jsonify({
        "total":   total,
        "page":    page,
        "limit":   limit,
        "records": [_serialize(r) for r in records],
    })


# ─────────────────────────────────────────────────────────────────────────────
# API — Stats
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/stats/<laptop_id>", methods=["GET"])
def api_stats(laptop_id: str):
    """
    Return trend (last 50 records) and fault distribution for a laptop.
    """
    lid = _oid(laptop_id)

    # Trend — last 50 diagnoses, ascending
    trend_raw = list(
        db.diagnostics.find({"laptop_id": lid})
        .sort("timestamp", -1)
        .limit(50)
    )
    trend_raw.reverse()
    trend = [
        {
            "timestamp":  r["timestamp"].isoformat() if isinstance(r.get("timestamp"), datetime) else r.get("timestamp"),
            "confidence": round(r.get("confidence", 0), 4),
            "diagnosis":  r.get("diagnosis", ""),
            "severity":   r.get("severity", ""),
        }
        for r in trend_raw
    ]

    # Distribution — count per diagnosis label
    pipeline = [
        {"$match": {"laptop_id": lid}},
        {"$group": {"_id": "$diagnosis", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
    ]
    distribution = list(db.diagnostics.aggregate(pipeline))

    return jsonify({"trend": trend, "distribution": distribution})


# ─────────────────────────────────────────────────────────────────────────────
# API — Live System Metrics
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/system/metrics", methods=["GET"])
def api_system_metrics():
    """
    Return current live hardware metrics from the host machine.
    Sensor values that cannot be read are returned as null (JSON null).
    The dashboard renders null as 'N/A' instead of a fake number.
    """
    metrics = get_system_metrics()
    serialised = {}
    for k, v in metrics.items():
        if v is None:
            serialised[k] = None          # null in JSON → 'N/A' in UI
        elif isinstance(v, (int, float)):
            serialised[k] = round(float(v), 4)
        else:
            serialised[k] = v
    return jsonify(serialised)


@app.route("/api/debug/sensors", methods=["GET"])
def api_debug_sensors():
    """
    Dump ALL raw WMI sensor data from LHM/OHM.
    Visit http://localhost:5000/api/debug/sensors to see exact sensor names and types.
    Use this to diagnose why sensors are not being parsed correctly.
    """
    import platform as _platform
    if _platform.system() != "Windows":
        return jsonify({"error": "Windows only"}), 400
    try:
        import wmi
    except ImportError:
        return jsonify({"error": "wmi not installed — pip install wmi pywin32"}), 500

    result = {}
    for ns in (r"root\LibreHardwareMonitor", r"root\OpenHardwareMonitor"):
        try:
            sensors = wmi.WMI(namespace=ns).Sensor()
            if not sensors:
                result[ns] = {"status": "connected but 0 sensors — LHM not running as Admin?"}
                continue
            items = []
            for s in sensors:
                try:
                    items.append({
                        "Name":       s.Name,
                        "SensorType": s.SensorType,
                        "Value":      float(s.Value or 0),
                        "Identifier": getattr(s, "Identifier", ""),
                    })
                except Exception as e:
                    items.append({"error": str(e)})
            result[ns] = {"status": f"{len(items)} sensors", "sensors": items}
        except Exception as e:
            result[ns] = {"status": "failed", "error": str(e)}
    return jsonify(result)


# ─────────────────────────────────────────────────────────────────────────────
# API — Scheduler Control
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/scheduler/start/<laptop_id>", methods=["POST"])
def api_scheduler_start(laptop_id: str):
    laptop = db.laptops.find_one({"_id": _oid(laptop_id), "active": True})
    if not laptop:
        return jsonify({"error": "Laptop not found"}), 404
    if not laptop.get("is_local"):
        return jsonify({"error": "Scheduler only available for local laptops"}), 400
    started = sched.start_scheduler(app, db, laptop_id)
    return jsonify({"ok": True, "started": started})


@app.route("/api/scheduler/stop/<laptop_id>", methods=["POST"])
def api_scheduler_stop(laptop_id: str):
    stopped = sched.stop_scheduler(laptop_id)
    return jsonify({"ok": True, "stopped": stopped})


@app.route("/api/scheduler/status", methods=["GET"])
def api_scheduler_status():
    return jsonify(sched.get_scheduler_status())


# ─────────────────────────────────────────────────────────────────────────────
# API — Global Settings
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/settings", methods=["GET"])
def api_get_settings():
    doc = db.settings.find_one({"_id": "global"}) or {}
    # Never expose raw SMTP password or agent key
    safe = {k: v for k, v in doc.items() if k != "_id"}
    safe.pop("smtp_password", None)
    # Tell the UI whether the agent key is configured, without revealing it
    safe["agent_api_key_set"] = bool(os.environ.get("AGENT_API_KEY", ""))
    return jsonify(safe)


@app.route("/api/settings", methods=["POST"])
def api_save_settings():
    data = request.get_json(force=True) or {}

    allowed = {
        "smtp_host", "smtp_port", "smtp_user", "smtp_password",
        "sender_name", "sender_email",
        "min_severity", "alert_cooldown", "weekly_report",
        "default_alert_email",   # fallback email for auto-registered agents with no DIAG_EMAIL set
    }
    update = {k: v for k, v in data.items() if k in allowed}

    # Coerce types
    if "smtp_port"      in update: update["smtp_port"]      = int(update["smtp_port"])
    if "alert_cooldown" in update: update["alert_cooldown"] = int(update["alert_cooldown"])
    if "weekly_report"  in update: update["weekly_report"]  = bool(update["weekly_report"])

    db.settings.update_one(
        {"_id": "global"},
        {"$set": update},
        upsert=True,
    )
    log.info("Global settings updated")
    return jsonify({"ok": True})


# ─────────────────────────────────────────────────────────────────────────────
# MongoDB Indexes (created once at startup)
# ─────────────────────────────────────────────────────────────────────────────

def _ensure_indexes():
    try:
        db.diagnostics.create_index([("laptop_id", 1), ("timestamp", -1)])
        db.diagnostics.create_index([("timestamp", -1)])
        db.diagnostics.create_index([("severity", 1)])
        db.diagnostics.create_index([("source", 1)])
        db.laptops.create_index([("active", 1)])
        db.laptops.create_index([("is_local", 1)])
        db.laptops.create_index([("last_checked", -1)])
        db.laptops.create_index([("machine_id", 1)], sparse=True)   # auto-registration dedup
        db.scheduler_locks.create_index([("heartbeat", 1)])
        log.info("MongoDB indexes ensured")
    except Exception as exc:
        log.warning("Index creation: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# Application startup
# ─────────────────────────────────────────────────────────────────────────────

with app.app_context():
    _check_mongo_connection()
    _ensure_indexes()

    # ── Windows sensor pre-flight check (DLL-based, no WMI needed) ──────────
    if platform.system() == "Windows":
        from pathlib import Path as _Path
        _base = _Path(__file__).parent
        _dll  = (
            _base / "LibreHardwareMonitorLib.dll"
            if (_base / "LibreHardwareMonitorLib.dll").exists()
            else _base / "LibreHardwareMonitor.dll"
        )
        _clr_ok = False
        try:
            import clr as _clr_test
            _clr_ok = True
        except ImportError:
            pass

        if _dll.exists() and _clr_ok:
            log.info("✅ Sensor setup: LibreHardwareMonitorLib.dll found + pythonnet installed")
        elif not _dll.exists():
            log.warning(
                "\n"
                "  ╔══════════════════════════════════════════════════════════╗\n"
                "  ║  SENSOR SETUP — LibreHardwareMonitorLib.dll missing     ║\n"
                "  ║                                                          ║\n"
                "  ║  For full sensor data (temp, voltage, fan):             ║\n"
                "  ║  1. pip install pythonnet                                ║\n"
                "  ║  2. Download LHM zip from:                              ║\n"
                "  ║     github.com/LibreHardwareMonitor/releases             ║\n"
                "  ║  3. Copy LibreHardwareMonitorLib.dll next to run.py     ║\n"
                "  ║  No admin rights needed. No separate app needed.        ║\n"
                "  ╚══════════════════════════════════════════════════════════╝"
            )
        elif not _clr_ok:
            log.warning(
                "\n"
                "  ╔══════════════════════════════════════════════════════════╗\n"
                "  ║  SENSOR SETUP — pythonnet not installed                 ║\n"
                "  ║                                                          ║\n"
                "  ║  LibreHardwareMonitorLib.dll found but pythonnet        ║\n"
                "  ║  is required to load it.                                ║\n"
                "  ║                                                          ║\n"
                "  ║  Fix:  pip install pythonnet                             ║\n"
                "  ║  Then restart the server.                                ║\n"
                "  ╚══════════════════════════════════════════════════════════╝"
            )

    started = sched.start_all_active(app, db)
    if started:
        log.info(
            "✅ Scheduler owner: worker %s started %d laptop scheduler(s)",
            sched._WORKER_ID, started,
        )
    else:
        log.info(
            "⏳ Worker %s is in stand-by mode — scheduler already owned by "
            "another process. Will auto-promote if owner becomes unresponsive "
            "(stale threshold: %ds).",
            sched._WORKER_ID, sched.LOCK_STALE_SECS,
        )
    sched.start_weekly_digest_thread(app, db)


# ─────────────────────────────────────────────────────────────────────────────
# FLEET MONITORING ADDITIONS
# ─────────────────────────────────────────────────────────────────────────────
# New endpoints added for fleet-scale monitoring:
#
#   POST /api/agent/report              ← agents POST metrics here every 5 min
#   POST /api/laptops/bulk-import       ← register up to 1000 laptops via CSV/JSON
#   GET  /api/fleet/status              ← live status of every registered laptop
#   POST /api/laptops/<id>/diagnose-now ← trigger an immediate manual diagnosis
# ─────────────────────────────────────────────────────────────────────────────

import csv
import io


def _check_agent_key(req) -> bool:
    """Validate the X-Agent-Key header against AGENT_API_KEY in .env."""
    expected = os.environ.get("AGENT_API_KEY", "")
    if not expected:
        # If no key configured, log a warning but allow (dev mode)
        log.warning("AGENT_API_KEY not set in .env — agent endpoint is open!")
        return True
    return req.headers.get("X-Agent-Key", "") == expected


# ── Agent auto-registration ──────────────────────────────────────────────────

@app.route("/api/agent/register", methods=["POST"])
def api_agent_register():
    """
    Self-registration endpoint called by the agent on first startup.

    The agent sends its machine_id (SHA-1 of MAC+hostname), display name,
    model, category, email, platform, and hostname.

    The server uses machine_id as a unique key:
      - If no laptop with this machine_id exists  → create and return 201
      - If one already exists                      → return existing record (200)

    This makes the endpoint fully idempotent — running the agent on the
    same machine multiple times, or restarting after a crash, never creates
    duplicate laptop records.

    After registration the agent saves the returned laptop_id locally so
    subsequent reports do not need to call this endpoint again.
    """
    if not _check_agent_key(request):
        return jsonify({"error": "Unauthorized"}), 401

    data       = request.get_json(force=True) or {}
    machine_id = (data.get("machine_id") or "").strip()
    name       = (data.get("name")       or "").strip()
    hostname   = (data.get("hostname")   or "").strip()

    if not machine_id:
        return jsonify({"error": "machine_id required"}), 400

    # Use hostname as fallback name — never allow empty string
    if not name or not name.strip():
        name = hostname or machine_id[:12] or "Unknown-Laptop"
    name = name.strip()

    # Idempotent upsert — find by machine_id, create if absent
    existing = db.laptops.find_one({"machine_id": machine_id, "active": True})

    if existing:
        # Already registered — update hostname/platform in case it changed
        try:
            agent_interval = int(data.get("polling_interval") or 600)
            agent_interval = max(30, min(3600, agent_interval))
        except (ValueError, TypeError):
            agent_interval = 600

        refresh = {
            "hostname":         hostname,
            "platform":         data.get("platform", ""),
            "polling_interval": agent_interval,
            "last_agent_seen":  datetime.now(timezone.utc),
        }
        # Only overwrite identity fields if the agent now has a non-empty
        # value — at first registration LHM often hasn't warmed up yet
        # (see agent/laptop_agent.py), so a later re-registration is the
        # first real chance to fill these in. Never overwrite a known
        # value with a blank one.
        for key in ("cpu_model", "ram_type", "gpu_model"):
            val = (data.get(key) or "").strip()
            if val:
                refresh[key] = val

        db.laptops.update_one({"_id": existing["_id"]}, {"$set": refresh})
        log.info("Agent re-registered: %s (%s)", existing.get("name"), str(existing["_id"]))
        return jsonify({
            "laptop_id": str(existing["_id"]),
            "name":      existing.get("name", name),
            "existing":  True,
        }), 200

    # New laptop — register it
    category = (data.get("category") or "midrange").lower()
    if category not in {"basic","midrange","highend","gaming","workstation"}:
        category = "midrange"

    email = (data.get("email") or "").strip()
    # If no email supplied, read default from global settings
    if not email:
        settings = db.settings.find_one({"_id": "global"}) or {}
        email = settings.get("default_alert_email", "")

    # Use the interval the agent sends (it knows its own schedule)
    # Fall back to 600s (10 min) if not provided
    try:
        agent_interval = int(data.get("polling_interval") or 600)
        agent_interval = max(30, min(3600, agent_interval))  # clamp 30s-1hr
    except (ValueError, TypeError):
        agent_interval = 600

    doc = {
        "name":             name,
        "machine_id":       machine_id,
        "hostname":         hostname,
        "model":            (data.get("model") or "").strip(),
        "cpu_model":        (data.get("cpu_model") or "").strip(),
        "ram_type":         (data.get("ram_type") or "").strip(),
        "gpu_model":        (data.get("gpu_model") or "").strip(),
        "platform":         data.get("platform", ""),
        "category":         category,
        "email":            email,
        "polling_interval": agent_interval,  # from agent, so stale detection is accurate
        "is_local":         False,   # agent laptops are always remote
        "notify_email":     bool(email),
        "active":           True,
        "auto_registered":  True,
        "created_at":       datetime.now(timezone.utc),
        "last_agent_seen":  datetime.now(timezone.utc),
        "last_checked":     None,
        "last_status":      None,
        "last_severity":    None,
    }

    result = db.laptops.insert_one(doc)
    lid    = str(result.inserted_id)

    log.info("New laptop auto-registered: %s | %s | ID: %s", name, hostname, lid)
    return jsonify({
        "laptop_id": lid,
        "name":      name,
        "existing":  False,
    }), 201


# ── Agent report receiver ─────────────────────────────────────────────────────

@app.route("/api/agent/report", methods=["POST"])
def api_agent_report():
    """
    Receive a hardware metrics report from a remote laptop agent.

    POST body (JSON):
        laptop_id  : str   — registered laptop _id
        metrics    : dict  — all 8 fuzzy inputs plus extra host info

    The server runs the Sugeno FIS immediately and stores the result.
    If a fault is detected and email alerts are enabled, an alert is sent.
    Returns the diagnosis result to the agent so it can log it locally.
    """
    if not _check_agent_key(request):
        return jsonify({"error": "Unauthorized"}), 401

    data      = request.get_json(force=True) or {}
    laptop_id = (data.get("laptop_id") or "").strip()
    metrics   = data.get("metrics") or {}

    if not laptop_id:
        return jsonify({"error": "laptop_id required"}), 400

    laptop = db.laptops.find_one({"_id": _oid(laptop_id), "active": True})
    if not laptop:
        return jsonify({"error": "Laptop not found"}), 404

    category = laptop.get("category", "midrange")
    profile_override = _maybe_recompute_profile(laptop, {
        "cpu_model": metrics.get("cpu_model", ""),
        "ram_type":  metrics.get("ram_type", ""),
        "gpu_model": metrics.get("gpu_model", ""),
    })

    # Build and validate the 8 fuzzy inputs from the reported metrics
    raw_inputs = [
        _safe_float(metrics.get("cpu_usage"),   50.0),
        _safe_float(metrics.get("fan_rpm"),     2500.0),
        _safe_float(metrics.get("cpu_temp"),    60.0),
        _safe_float(metrics.get("cpu_voltage"), 1.20),
        _safe_float(metrics.get("ram_voltage"), 1.25),
        _safe_float(metrics.get("gpu_voltage"), 1.00),
        _safe_float(metrics.get("rail_3v3"),    3.30),
        _safe_float(metrics.get("rail_5v_mw"),  500.0),
    ]
    inputs = _validate_inputs(raw_inputs)

    diagnosis, secondary, confidence, weights = get_diagnostics_sugeno(
        inputs, category=category, profile_override=profile_override
    )
    conf_level = categorize_confidence(confidence)
    severity   = SEVERITY_MAP.get(diagnosis, "OK")
    ts         = datetime.now(timezone.utc)

    record = {
        "laptop_id":    _oid(laptop_id),
        "laptop_name":  laptop.get("name", ""),
        "timestamp":    ts,
        "source":       "agent",
        "category":     category,
        "diagnosis":    diagnosis,
        "secondary":    secondary,
        "confidence":   round(confidence, 6),
        "conf_level":   conf_level,
        "severity":     severity,
        "rule_weights": [round(w, 6) for w in weights],
        "metrics":      {k: (round(float(v), 4) if isinstance(v, (int, float))
                              else (v if isinstance(v, str) and len(v) < 100 else None))
                         for k, v in metrics.items()
                         if not isinstance(v, dict)},
        "notified":     False,
        "agent_host":   metrics.get("hostname", ""),
        "agent_platform": metrics.get("platform", ""),
    }
    inserted = db.diagnostics.insert_one(record)

    db.laptops.update_one(
        {"_id": _oid(laptop_id)},
        {"$set": {
            "last_checked":   ts,
            "last_status":    diagnosis,
            "last_severity":  severity,
            "last_agent_host": metrics.get("hostname", ""),
        }},
    )

    notified = False
    if laptop.get("notify_email", True):
        notified = maybe_send_fault_alert(db, laptop, {
            "diagnosis":  diagnosis,
            "secondary":  secondary,
            "confidence": confidence,
            "conf_level": conf_level,
            "severity":   severity,
            "timestamp":  ts.isoformat(),
        })
        if notified:
            db.diagnostics.update_one(
                {"_id": inserted.inserted_id},
                {"$set": {"notified": True}},
            )

    log.info(
        "Agent report [%s / %s]: %s | %s | notified=%s",
        laptop.get("name"), metrics.get("hostname","?"),
        diagnosis, severity, notified,
    )

    return jsonify({
        "ok":         True,
        "diagnosis":  diagnosis,
        "secondary":  secondary,
        "confidence": round(confidence, 6),
        "conf_level": conf_level,
        "severity":   severity,
        "notified":   notified,
        "timestamp":  ts.isoformat(),
    })


# ── Bulk import ───────────────────────────────────────────────────────────────

@app.route("/api/laptops/bulk-import", methods=["POST"])
def api_bulk_import():
    """
    Register multiple laptops at once from a CSV file or JSON array.

    CSV format (first row = headers):
        name, email, category, model, polling_interval, notify_email

    JSON format:
        [ {"name": "...", "email": "...", "category": "midrange", ...}, ... ]

    Content-Type: multipart/form-data  with field "file" for CSV
                  application/json                          for JSON array

    Returns:
        { "imported": N, "skipped": N, "errors": [...], "laptops": [...] }

    Laptops with the same name that already exist are skipped (not duplicated).
    Maximum 1000 rows per request.
    """
    MAX_ROWS        = 1000
    valid_cats      = {"basic", "midrange", "highend", "gaming", "workstation"}
    imported        = []
    skipped         = []
    errors          = []

    # ── Parse input ───────────────────────────────────────────────────────────
    rows = []

    if request.content_type and "multipart" in request.content_type:
        # CSV file upload
        f = request.files.get("file")
        if not f:
            return jsonify({"error": "No file uploaded"}), 400
        try:
            content  = f.read().decode("utf-8-sig")   # strip BOM if present
            reader   = csv.DictReader(io.StringIO(content))
            rows     = [r for r in reader]
        except Exception as exc:
            return jsonify({"error": f"CSV parse error: {exc}"}), 400
    else:
        # JSON array
        body = request.get_json(force=True) or []
        if not isinstance(body, list):
            return jsonify({"error": "JSON body must be an array"}), 400
        rows = body

    if len(rows) > MAX_ROWS:
        return jsonify({"error": f"Maximum {MAX_ROWS} rows per import"}), 400

    # ── Process each row ──────────────────────────────────────────────────────
    for i, row in enumerate(rows):
        row_num = i + 2    # 1-indexed, +1 for header

        name  = str(row.get("name")  or "").strip()
        email = str(row.get("email") or "").strip()

        if not name or not email:
            errors.append({"row": row_num, "reason": "name and email are required", "data": row})
            continue

        category = str(row.get("category") or "midrange").lower().strip()
        if category not in valid_cats:
            category = "midrange"

        try:
            poll_interval = max(30, int(row.get("polling_interval") or 300))
        except (ValueError, TypeError):
            poll_interval = 300

        notify = str(row.get("notify_email") or "true").lower() not in ("false","0","no")

        # Skip if a laptop with this exact name already exists
        existing = db.laptops.find_one({"name": name, "active": True})
        if existing:
            skipped.append({"name": name, "reason": "already exists", "_id": str(existing["_id"])})
            continue

        doc = {
            "name":             name,
            "email":            email,
            "category":         category,
            "model":            str(row.get("model") or "").strip(),
            "polling_interval": poll_interval,
            "is_local":         False,   # bulk imports are always remote agents
            "notify_email":     notify,
            "active":           True,
            "created_at":       datetime.now(timezone.utc),
            "last_checked":     None,
            "last_status":      None,
            "last_severity":    None,
        }

        try:
            result = db.laptops.insert_one(doc)
            doc["_id"] = result.inserted_id
            imported.append(_serialize(doc))
        except Exception as exc:
            errors.append({"row": row_num, "name": name, "reason": str(exc)})

    log.info(
        "Bulk import: %d imported, %d skipped, %d errors",
        len(imported), len(skipped), len(errors),
    )
    return jsonify({
        "imported": len(imported),
        "skipped":  len(skipped),
        "errors":   len(errors),
        "error_details": errors,
        "skip_details":  skipped,
        "laptops":  imported,   # includes assigned _id for each — use in agent config
    }), 200 if not errors else 207


# ── Live fleet status ─────────────────────────────────────────────────────────

@app.route("/api/fleet/status", methods=["GET"])
def api_fleet_status():
    """
    Return a live summary of every registered laptop.

    Each entry shows the latest diagnosis, severity, when it was last seen,
    and whether it is considered online (last report within 2x polling_interval).

    Used by the fleet overview dashboard to render live status cards.
    """
    laptops = list(db.laptops.find({"active": True}).sort("name", 1))
    now     = datetime.now(timezone.utc)
    result  = []

    for lap in laptops:
        last_checked = lap.get("last_checked")
        if last_checked and last_checked.tzinfo is None:
            last_checked = last_checked.replace(tzinfo=timezone.utc)

        poll_interval = int(lap.get("polling_interval", 300))
        # Considered offline if no report received within one full interval
        # plus 90 seconds grace (network delay / slow start).
        # Previously poll_interval * 2 meant a stopped agent showed online
        # for 20 minutes. Now it goes offline within ~1.5 minutes of stopping.
        stale_after = poll_interval + 90

        if last_checked:
            age_secs = (now - last_checked).total_seconds()
            online   = age_secs <= stale_after
            last_seen_secs = int(age_secs)
        else:
            online         = False
            last_seen_secs = None

        result.append({
            "_id":             str(lap["_id"]),
            "name":            lap.get("name", ""),
            "hostname":        lap.get("hostname", ""),
            "model":           lap.get("model", ""),
            "category":        lap.get("category", ""),
            "email":           lap.get("email", ""),
            "is_local":        lap.get("is_local", False),
            "auto_registered": lap.get("auto_registered", False),
            "platform":        lap.get("platform", ""),
            "last_status":     lap.get("last_status"),
            "last_severity":   lap.get("last_severity"),
            "last_checked":    last_checked.isoformat() if last_checked else None,
            "last_seen_secs":  last_seen_secs,
            "online":          online,
            "polling_interval": poll_interval,
        })

    total    = len(result)
    online   = sum(1 for r in result if r["online"])
    critical = sum(1 for r in result if r.get("last_severity") in ("CRITICAL","HIGH"))

    return jsonify({
        "summary": {
            "total":    total,
            "online":   online,
            "offline":  total - online,
            "critical": critical,
        },
        "laptops": result,
    })


# ── Immediate manual diagnosis for any laptop ─────────────────────────────────

@app.route("/api/laptops/<laptop_id>/diagnose-now", methods=["POST"])
def api_diagnose_now(laptop_id: str):
    """
    Trigger an immediate diagnosis for any laptop.

    For is_local laptops  — reads metrics from this host.
    For remote laptops    — uses the most recent agent metrics stored in the
                            last diagnostics record (last-known values).

    This is what the dashboard "Diagnose Now" button calls.
    """
    laptop = db.laptops.find_one({"_id": _oid(laptop_id), "active": True})
    if not laptop:
        return jsonify({"error": "Laptop not found"}), 404

    category = laptop.get("category", "midrange")

    if laptop.get("is_local"):
        # Live read from host machine
        metrics = get_system_metrics()
        source  = "manual_local"
        identity = get_hardware_identity()
    else:
        # Use last known metrics from the most recent agent report
        last_record = db.diagnostics.find_one(
            {"laptop_id": _oid(laptop_id), "source": "agent"},
            sort=[("timestamp", -1)],
        )
        if not last_record or not last_record.get("metrics"):
            return jsonify({
                "error": "No agent data received yet for this laptop. "
                         "Make sure the agent is installed and running on the target machine."
            }), 409

        metrics = last_record["metrics"]
        source  = "manual_remote"
        # Identity fields are resolved and cached on the laptop document at
        # report-ingestion time (see api_agent_report) — nothing new to pass
        # here, _maybe_recompute_profile will reuse the cached identity.
        identity = {}

    profile_override = _maybe_recompute_profile(laptop, identity)

    inputs = _validate_inputs([
        _safe_float(metrics.get("cpu_usage"),   50.0),
        _safe_float(metrics.get("fan_rpm"),     2500.0),
        _safe_float(metrics.get("cpu_temp"),    60.0),
        _safe_float(metrics.get("cpu_voltage"), 1.20),
        _safe_float(metrics.get("ram_voltage"), 1.25),
        _safe_float(metrics.get("gpu_voltage"), 1.00),
        _safe_float(metrics.get("rail_3v3"),    3.30),
        _safe_float(metrics.get("rail_5v_mw"),  500.0),
    ])

    diagnosis, secondary, confidence, weights = get_diagnostics_sugeno(
        inputs, category=category, profile_override=profile_override
    )
    conf_level = categorize_confidence(confidence)
    severity   = SEVERITY_MAP.get(diagnosis, "OK")
    ts         = datetime.now(timezone.utc)

    record = {
        "laptop_id":    _oid(laptop_id),
        "laptop_name":  laptop.get("name",""),
        "timestamp":    ts,
        "source":       source,
        "category":     category,
        "diagnosis":    diagnosis,
        "secondary":    secondary,
        "confidence":   round(confidence, 6),
        "conf_level":   conf_level,
        "severity":     severity,
        "rule_weights": [round(w, 6) for w in weights],
        "metrics":      {k: (round(float(v), 4) if isinstance(v, (int, float))
                              else (v if isinstance(v, str) and len(v) < 100 else None))
                         for k, v in metrics.items()
                         if not isinstance(v, dict)},
        "notified":     False,
    }
    inserted = db.diagnostics.insert_one(record)
    db.laptops.update_one(
        {"_id": _oid(laptop_id)},
        {"$set": {"last_checked": ts, "last_status": diagnosis, "last_severity": severity}},
    )

    notified = False
    if laptop.get("notify_email", True):
        notified = maybe_send_fault_alert(db, laptop, {
            "diagnosis": diagnosis, "secondary": secondary,
            "confidence": confidence, "conf_level": conf_level,
            "severity": severity, "timestamp": ts.isoformat(),
        })
        if notified:
            db.diagnostics.update_one({"_id": inserted.inserted_id}, {"$set": {"notified": True}})

    return jsonify({
        "diagnosis":  diagnosis,
        "secondary":  secondary,
        "confidence": round(confidence, 6),
        "conf_level": conf_level,
        "severity":   severity,
        "notified":   notified,
        "source":     source,
        "timestamp":  ts.isoformat(),
        "record_id":  str(inserted.inserted_id),
    })


# ── Request immediate report from remote agent ─────────────────────────────────
@app.route("/api/laptops/<laptop_id>/request-report", methods=["POST"])
def api_request_report(laptop_id: str):
    """
    Dashboard calls this when user clicks Diagnose Now on a remote/agent laptop.
    Sets a flag in MongoDB — the agent checks this flag every 10 seconds and
    sends a fresh report immediately instead of waiting for its full interval.
    """
    laptop = db.laptops.find_one({"_id": _oid(laptop_id), "active": True})
    if not laptop:
        return jsonify({"error": "Laptop not found"}), 404
    if laptop.get("is_local"):
        return jsonify({"error": "Local laptop — use diagnose-now directly"}), 400
    db.laptops.update_one(
        {"_id": _oid(laptop_id)},
        {"$set": {
            "report_requested":    True,
            "report_requested_at": datetime.now(timezone.utc),
        }}
    )
    return jsonify({"ok": True})


# ── Agent polls this to check if an immediate report is needed ─────────────────
@app.route("/api/test-email", methods=["POST"])
def api_test_email():
    """
    Directly test SMTP configuration without touching the diagnosis engine.
    Sends a plain test email to the address supplied in the request body.
    Returns success/failure with a specific error message.
    """
    data      = request.get_json(silent=True) or {}
    recipient = (data.get("email") or "").strip()

    if not recipient:
        return jsonify({"success": False, "message": "No recipient email address supplied"}), 400

    cfg = email_notifier._get_smtp_config(db)

    if not cfg.get("user") or not cfg.get("password"):
        return jsonify({
            "success": False,
            "message": "SMTP credentials not configured. Set SMTP_USER and SMTP_PASSWORD in Settings."
        }), 400

    subject    = "✅ LaptopDiag — SMTP Test Email"
    plain_body = (
        "This is a test email from your Laptop Motherboard Diagnostics System.\n"
        "If you received this, your SMTP configuration is working correctly.\n\n"
        "Sugeno FIS · 2026"
    )
    html_body  = """
    <div style="font-family:Arial,sans-serif;max-width:520px;margin:0 auto;
                background:#0d1b2a;color:#e0e8f0;border-radius:10px;padding:28px;">
      <h2 style="color:#00b4d8;margin-top:0;">✅ SMTP Test Successful</h2>
      <p style="font-size:15px;line-height:1.6;">
        Your email configuration is working correctly.<br>
        Fault alerts and weekly reports will be delivered to this address.
      </p>
      <hr style="border:1px solid #1e3a5f;margin:20px 0;">
      <p style="font-size:12px;color:#8fa8c8;">
        Laptop Motherboard Diagnostics System &nbsp;·&nbsp;
        Sugeno FIS &nbsp;·&nbsp; 2026
      </p>
    </div>"""

    success = email_notifier._send_email(cfg, recipient, subject, html_body, plain_body)

    if success:
        return jsonify({"success": True,  "message": f"Test email sent to {recipient}"})
    else:
        if cfg.get("host", "").endswith("gmail.com"):
            hint = ("Gmail rejected the credentials. "
                    "Make sure you are using a Gmail App Password, "
                    "not your regular Gmail password.")
        else:
            hint = f"SMTP connection to {cfg.get('host')} failed. Check host/port/credentials."
        return jsonify({"success": False, "message": hint}), 500


@app.route("/api/agent/check-pending", methods=["POST"])
def api_check_pending():
    """
    Called by the agent every 10 seconds while idle.
    Returns {"report_now": true} if the dashboard wants a fresh report.
    Clears the flag atomically before returning.
    """
    if not _check_agent_key(request):
        return jsonify({"error": "Unauthorized"}), 401

    data      = request.get_json(silent=True) or {}
    laptop_id = data.get("laptop_id", "")
    if not laptop_id:
        return jsonify({"report_now": False})

    try:
        laptop = db.laptops.find_one({"_id": _oid(laptop_id)})
    except Exception:
        return jsonify({"report_now": False})

    if laptop and laptop.get("report_requested"):
        db.laptops.update_one(
            {"_id": _oid(laptop_id)},
            {"$unset": {"report_requested": "", "report_requested_at": ""}}
        )
        return jsonify({"report_now": True})

    return jsonify({"report_now": False})
