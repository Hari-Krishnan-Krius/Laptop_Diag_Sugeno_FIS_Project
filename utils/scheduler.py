"""
utils/scheduler.py
─────────────────────────────────────────────────────────────────────────────
Background polling scheduler for the Laptop Diagnostics System.

Architecture
  • One daemon thread per laptop (started/stopped via API).
  • A single "cron" daemon thread fires weekly digest emails.
  • All threads share the Flask app context via app.app_context().
  • Thread-safe state tracked in _schedulers dict (in-memory, per-process).

Multi-worker protection (Gunicorn / uWSGI)
  Both the polling scheduler and the weekly digest thread are protected by
  a MongoDB heartbeat lock (scheduler_locks collection).  Only the process
  that holds the lock runs threads; others stand by and promote themselves
  automatically if the lock goes stale (no heartbeat for > LOCK_STALE_SECS).

  Lock document schema:
    {
      _id:          "scheduler_owner",
      worker_id:    "pid-<pid>",
      heartbeat:    ISODate(...)   ← updated every HEARTBEAT_INTERVAL_SECS
    }

  Stale-lock reclaim flow:
    1. Startup: read the lock document.
    2. If absent → insert and own it.
    3. If present and heartbeat < (now - LOCK_STALE_SECS) → replace and own it.
    4. If present and fresh → stand by; poll every STANDBY_POLL_SECS.
    5. Lock owner keeps its heartbeat alive in a background thread.

  This removes the need for manual operator intervention after a crash.

Public API
  start_scheduler(app, db, laptop_id)  → bool
  stop_scheduler(laptop_id)            → bool
  get_scheduler_status()               → dict  {laptop_id: bool}
  start_all_active(app, db)            → int   (count started)
  start_weekly_digest_thread(app, db)  → None
  release_scheduler_lock(db)           → None  (graceful shutdown)
─────────────────────────────────────────────────────────────────────────────
"""

import logging
import os
import threading
import time
from datetime import datetime, timezone, timedelta

from bson import ObjectId

log = logging.getLogger(__name__)

# ── Tunable constants ──────────────────────────────────────────────────────────
# A lock is considered stale if its heartbeat is older than this.
LOCK_STALE_SECS = 60

# How often the lock owner updates its heartbeat.
HEARTBEAT_INTERVAL_SECS = 20

# How often a stand-by worker checks whether the lock has gone stale.
STANDBY_POLL_SECS = 30

# ── Internal state ─────────────────────────────────────────────────────────────
_schedulers: dict = {}           # laptop_id → threading.Event
_lock = threading.Lock()

# Unique ID for this OS process; survives thread restarts.
_WORKER_ID = f"pid-{os.getpid()}"

# Flag so the heartbeat thread knows whether this process holds the lock.
_owns_lock = False
_owns_lock_mutex = threading.Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ─────────────────────────────────────────────────────────────────────────────
# Heartbeat lock — helpers
# ─────────────────────────────────────────────────────────────────────────────

def _lock_is_stale(doc: dict) -> bool:
    """Return True if the lock document's heartbeat is old enough to reclaim."""
    hb = doc.get("heartbeat")
    if hb is None:
        return True
    if hb.tzinfo is None:
        hb = hb.replace(tzinfo=timezone.utc)
    return (_now() - hb).total_seconds() > LOCK_STALE_SECS


def _try_insert_lock(db) -> bool:
    """
    Attempt to atomically insert the lock document.
    Returns True if this process created it (i.e. we own it).
    Returns False if it already existed.
    """
    try:
        db.scheduler_locks.insert_one({
            "_id":       "scheduler_owner",
            "worker_id": _WORKER_ID,
            "heartbeat": _now(),
        })
        return True
    except Exception:
        # DuplicateKeyError → someone else already holds it
        return False


def _try_reclaim_stale_lock(db) -> bool:
    """
    Replace a stale lock document with our own, using findOneAndReplace
    so only one racing worker wins.
    Returns True if we successfully replaced a stale lock.
    """
    try:
        result = db.scheduler_locks.find_one_and_replace(
            {
                "_id":       "scheduler_owner",
                "heartbeat": {"$lt": _now() - timedelta(seconds=LOCK_STALE_SECS)},
            },
            {
                "_id":       "scheduler_owner",
                "worker_id": _WORKER_ID,
                "heartbeat": _now(),
            },
        )
        return result is not None   # None → someone else won the race
    except Exception as exc:
        log.debug("Stale lock reclaim attempt failed: %s", exc)
        return False


def _update_heartbeat(db) -> None:
    """Update our heartbeat timestamp in the lock document."""
    try:
        db.scheduler_locks.update_one(
            {"_id": "scheduler_owner", "worker_id": _WORKER_ID},
            {"$set": {"heartbeat": _now()}},
        )
    except Exception as exc:
        log.warning("Heartbeat update failed: %s", exc)


def _try_claim_lock(db) -> bool:
    """
    Main entry point for the lock acquisition sequence.
    1. Try inserting (no existing lock).
    2. If that fails, check if the existing lock is stale and reclaim it.
    3. If lock is fresh and held by another worker, return False.
    """
    try:
        # Case 1: no lock exists yet
        if _try_insert_lock(db):
            log.info("Scheduler lock created by %s", _WORKER_ID)
            return True

        # Case 2/3: lock exists — check freshness
        doc = db.scheduler_locks.find_one({"_id": "scheduler_owner"})
        if doc is None:
            # Vanished between insert attempt and read — retry once
            return _try_insert_lock(db)

        if doc.get("worker_id") == _WORKER_ID:
            # We already own it (e.g. called twice in same process)
            return True

        if _lock_is_stale(doc):
            won = _try_reclaim_stale_lock(db)
            if won:
                log.info(
                    "Stale scheduler lock reclaimed from %s by %s",
                    doc.get("worker_id"), _WORKER_ID,
                )
            return won

        log.info(
            "Scheduler lock held (fresh) by %s — this worker will stand by",
            doc.get("worker_id"),
        )
        return False

    except Exception as exc:
        # MongoDB unavailable → fall back to running (single-process assumption)
        log.warning("Could not acquire scheduler lock (%s) — proceeding anyway", exc)
        return True


def release_scheduler_lock(db) -> None:
    """Release the lock on graceful shutdown so another worker can take over immediately."""
    global _owns_lock
    try:
        db.scheduler_locks.delete_one(
            {"_id": "scheduler_owner", "worker_id": _WORKER_ID}
        )
        log.info("Scheduler lock released by %s", _WORKER_ID)
    except Exception as exc:
        log.warning("Could not release scheduler lock: %s", exc)
    with _owns_lock_mutex:
        _owns_lock = False


# ─────────────────────────────────────────────────────────────────────────────
# Heartbeat background thread
# ─────────────────────────────────────────────────────────────────────────────

def _heartbeat_loop(app, db) -> None:
    """
    Runs in a daemon thread while this process holds the scheduler lock.
    Updates the heartbeat every HEARTBEAT_INTERVAL_SECS so stand-by workers
    can detect if we crash (heartbeat stops updating → lock goes stale).
    """
    with app.app_context():
        while True:
            time.sleep(HEARTBEAT_INTERVAL_SECS)
            with _owns_lock_mutex:
                if not _owns_lock:
                    break           # lock was released; exit thread
            _update_heartbeat(db)
            log.debug("Scheduler heartbeat updated by %s", _WORKER_ID)


def _start_heartbeat_thread(app, db) -> None:
    t = threading.Thread(
        target=_heartbeat_loop,
        args=(app, db),
        name="sched-heartbeat",
        daemon=True,
    )
    t.start()


# ─────────────────────────────────────────────────────────────────────────────
# Stand-by promotion thread
# ─────────────────────────────────────────────────────────────────────────────

def _standby_loop(app, db) -> None:
    """
    Runs in a daemon thread on worker processes that lost the lock election.
    Polls every STANDBY_POLL_SECS; if the lock has gone stale it promotes
    this worker to scheduler owner and starts all required threads.
    """
    with app.app_context():
        while True:
            time.sleep(STANDBY_POLL_SECS)
            if _try_claim_lock(db):
                log.info(
                    "Stand-by worker %s promoted to scheduler owner", _WORKER_ID
                )
                global _owns_lock
                with _owns_lock_mutex:
                    _owns_lock = True
                _start_heartbeat_thread(app, db)
                # Restart all active schedulers in this process
                try:
                    laptops = list(db.laptops.find({"active": True, "is_local": True}))
                    for laptop in laptops:
                        start_scheduler(app, db, str(laptop["_id"]))
                except Exception as exc:
                    log.error("Stand-by promotion scheduler restart failed: %s", exc)
                # Also re-start weekly digest in this process
                _launch_weekly_digest_thread(app, db)
                break   # promotion done; this thread is no longer needed


def _start_standby_thread(app, db) -> None:
    t = threading.Thread(
        target=_standby_loop,
        args=(app, db),
        name="sched-standby",
        daemon=True,
    )
    t.start()
    log.info("Stand-by promotion thread started for worker %s", _WORKER_ID)


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def start_scheduler(app, db, laptop_id: str) -> bool:
    """
    Start a background polling thread for the given laptop.
    Returns False if a thread for this laptop is already running.
    """
    lid = str(laptop_id)
    with _lock:
        if lid in _schedulers:
            return False
        stop_event = threading.Event()
        _schedulers[lid] = stop_event

    t = threading.Thread(
        target=_poll_loop,
        args=(app, db, lid, stop_event),
        name=f"diag-{lid[:8]}",
        daemon=True,
    )
    t.start()
    log.info("Scheduler started for laptop %s", lid)
    return True


def stop_scheduler(laptop_id: str) -> bool:
    """
    Signal the polling thread for the given laptop to stop.
    Returns False if no thread was running.
    """
    lid = str(laptop_id)
    with _lock:
        event = _schedulers.pop(lid, None)
    if event is None:
        return False
    event.set()
    log.info("Scheduler stopped for laptop %s", lid)
    return True


def get_scheduler_status() -> dict:
    """Return {laptop_id: True} for every currently-running scheduler."""
    with _lock:
        return {lid: True for lid in _schedulers}


def start_all_active(app, db) -> int:
    """
    Called at startup — attempt to become the scheduler owner and resume
    polling for every active local laptop.

    If this process loses the lock election it starts a stand-by thread
    that will promote itself automatically if the current owner crashes.

    Returns the number of schedulers started (0 if lock not acquired).
    """
    global _owns_lock

    if not _try_claim_lock(db):
        # Lost the election — start stand-by watcher and exit
        _start_standby_thread(app, db)
        return 0

    with _owns_lock_mutex:
        _owns_lock = True

    _start_heartbeat_thread(app, db)

    count = 0
    try:
        laptops = list(db.laptops.find({"active": True, "is_local": True}))
        for laptop in laptops:
            lid = str(laptop["_id"])
            if start_scheduler(app, db, lid):
                count += 1
    except Exception as exc:
        log.error("start_all_active failed: %s", exc)
    return count


def start_weekly_digest_thread(app, db) -> None:
    """
    Start the Monday-09:00 weekly digest cron thread — but only on the
    process that holds the scheduler lock to prevent duplicate digest emails
    when multiple workers are running.

    If this process is currently standing by (lost the lock election),
    the stand-by promotion thread will call _launch_weekly_digest_thread()
    when it takes ownership later.
    """
    with _owns_lock_mutex:
        if not _owns_lock:
            log.info(
                "Weekly digest thread skipped on stand-by worker %s", _WORKER_ID
            )
            return
    _launch_weekly_digest_thread(app, db)


def _launch_weekly_digest_thread(app, db) -> None:
    """Internal helper — unconditionally starts the digest thread."""
    t = threading.Thread(
        target=_weekly_digest_loop,
        args=(app, db),
        name="weekly-digest",
        daemon=True,
    )
    t.start()
    log.info("Weekly digest cron thread started on worker %s", _WORKER_ID)


# ─────────────────────────────────────────────────────────────────────────────
# Input validation
# ─────────────────────────────────────────────────────────────────────────────

_INPUT_BOUNDS = [
    (0.0,    100.0),   # X1  cpu_usage    %
    (0.0,   6000.0),   # X2  fan_rpm      RPM
    (-20.0,  150.0),   # X3  cpu_temp     °C
    (0.0,     2.5),    # X4  cpu_voltage  V
    (0.0,     2.5),    # X5  ram_voltage  V
    (0.0,     2.5),    # X6  gpu_voltage  V
    (0.0,     5.5),    # X7  rail_3v3     V
    (0.0,  1000.0),    # X8  rail_5v_mw   mW proxy
]

_INPUT_NAMES = [
    "cpu_usage", "fan_rpm", "cpu_temp", "cpu_voltage",
    "ram_voltage", "gpu_voltage", "rail_3v3", "rail_5v_mw",
]


# Nominal midrange defaults used when a sensor cannot be read (returns None).
# These are physically plausible values — the fuzzy engine will most likely
# classify as Healthy System when inputs are all nominal, which is the correct
# safe assumption when hardware data is simply unavailable.
_SENSOR_DEFAULTS = {
    "cpu_usage":   50.0,
    "fan_rpm":    2500.0,   # mid-range fan speed
    "cpu_temp":    60.0,    # warm but not alarming
    "cpu_voltage":  1.20,
    "ram_voltage":  1.25,
    "gpu_voltage":  1.00,
    "rail_3v3":     3.30,
    "rail_5v_mw": 500.0,
}


def _validate_inputs(inputs: list) -> list:
    """
    Sanitise the 8 fuzzy engine inputs:
    - None (sensor not available on this hardware) → safe nominal default
    - Out-of-range values → clamped with a WARNING log
    - Non-numeric → safe default with a WARNING log

    None values are expected and do NOT produce warning logs — they mean
    the sensor simply doesn't exist on this machine (e.g. HP laptops have
    no fan sensor exposed via LHM).
    """
    cleaned = []
    for i, (val, (lo, hi)) in enumerate(zip(inputs, _INPUT_BOUNDS)):
        name = _INPUT_NAMES[i]

        if val is None:
            # Sensor not available — use nominal default silently
            v = _SENSOR_DEFAULTS.get(name, (lo + hi) / 2)
        else:
            try:
                v = float(val)
            except (TypeError, ValueError):
                log.warning("Input %s is not numeric (%r) — using nominal default",
                            name, val)
                v = _SENSOR_DEFAULTS.get(name, (lo + hi) / 2)

            if v < lo or v > hi:
                clamped = max(lo, min(hi, v))
                log.warning(
                    "Input %s=%.4f out of range [%.2f, %.2f] — clamped to %.4f",
                    name, v, lo, hi, clamped,
                )
                v = clamped

        cleaned.append(v)
    return cleaned


# ─────────────────────────────────────────────────────────────────────────────
# Polling loop
# ─────────────────────────────────────────────────────────────────────────────

def _poll_loop(app, db, laptop_id: str, stop_event: threading.Event) -> None:
    """Main polling loop for one laptop."""
    from utils.system_monitor import get_system_metrics
    from utils.fuzzy_engine   import get_diagnostics_sugeno, categorize_confidence, SEVERITY_MAP
    from utils.email_notifier import maybe_send_fault_alert

    with app.app_context():
        while not stop_event.is_set():
            try:
                laptop = db.laptops.find_one({"_id": ObjectId(laptop_id)})
                if not laptop or not laptop.get("active"):
                    log.info("Laptop %s deactivated — stopping scheduler", laptop_id)
                    break

                interval = int(laptop.get("polling_interval", 60))
                category = laptop.get("category", "midrange")

                metrics = get_system_metrics()

                inputs = _validate_inputs([
                    metrics["cpu_usage"],
                    metrics["fan_rpm"],
                    metrics["cpu_temp"],
                    metrics["cpu_voltage"],
                    metrics["ram_voltage"],
                    metrics["gpu_voltage"],
                    metrics["rail_3v3"],
                    metrics["rail_5v_mw"],
                ])

                diagnosis, secondary, confidence, weights = get_diagnostics_sugeno(
                    inputs, category=category
                )
                conf_level = categorize_confidence(confidence)
                severity   = SEVERITY_MAP.get(diagnosis, "OK")
                ts         = _now()

                record = {
                    "laptop_id":    ObjectId(laptop_id),
                    "laptop_name":  laptop.get("name", ""),
                    "timestamp":    ts,
                    "source":       "auto",
                    "category":     category,
                    "diagnosis":    diagnosis,
                    "secondary":    secondary,
                    "confidence":   round(confidence, 6),
                    "conf_level":   conf_level,
                    "severity":     severity,
                    "rule_weights": [round(w, 6) for w in weights],
                    "metrics":      {k: (round(float(v), 4) if isinstance(v, (int, float)) else None)
                                     for k, v in metrics.items()
                                     if isinstance(v, (int, float))},
                    "notified":     False,
                }

                result = db.diagnostics.insert_one(record)

                db.laptops.update_one(
                    {"_id": ObjectId(laptop_id)},
                    {"$set": {
                        "last_checked":  ts,
                        "last_status":   diagnosis,
                        "last_severity": severity,
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
                        {"_id": result.inserted_id},
                        {"$set": {"notified": True}},
                    )

                log.debug(
                    "Auto diagnosis [%s]: %s (%.1f%%) sev=%s",
                    laptop.get("name"), diagnosis, confidence * 100, severity,
                )

            except Exception as exc:
                log.error("Poll loop error for laptop %s: %s", laptop_id, exc)

            for _ in range(interval * 2):
                if stop_event.is_set():
                    break
                time.sleep(0.5)

    with _lock:
        _schedulers.pop(laptop_id, None)
    log.info("Poll loop exited for laptop %s", laptop_id)


# ─────────────────────────────────────────────────────────────────────────────
# Weekly digest cron
# ─────────────────────────────────────────────────────────────────────────────

def _weekly_digest_loop(app, db) -> None:
    """Sleeps until the next Monday 09:00 UTC, sends digests, then repeats."""
    from utils.email_notifier import send_weekly_digest

    with app.app_context():
        while True:
            now = _now()
            days_until_monday = (7 - now.weekday()) % 7 or 7
            next_monday = (now + timedelta(days=days_until_monday)).replace(
                hour=9, minute=0, second=0, microsecond=0,
                tzinfo=timezone.utc,
            )
            sleep_seconds = (next_monday - now).total_seconds()
            log.info(
                "Weekly digest scheduled in %.0f s (next Monday 09:00 UTC)",
                sleep_seconds,
            )

            elapsed = 0.0
            while elapsed < sleep_seconds:
                time.sleep(min(60, sleep_seconds - elapsed))
                elapsed += 60.0

            try:
                cfg = db.settings.find_one({"_id": "global"}) or {}
                if cfg.get("weekly_report", False):
                    count = send_weekly_digest(db)
                    log.info("Weekly digest sent to %d recipients", count)
            except Exception as exc:
                log.error("Weekly digest error: %s", exc)
