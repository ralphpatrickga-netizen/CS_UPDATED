import os
import re
import csv
import io
import hashlib
import hmac
import secrets
import threading
from functools import wraps
from datetime import datetime, timedelta
import random
from urllib.parse import urlsplit

from flask import (
    Flask, render_template, request, redirect, url_for, session, flash,
    jsonify, send_file, abort
)
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

import db as dbh
import chatbot
import priority as prioritymod
import email_service

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
ALLOWED_EXT = {"jpg", "jpeg", "png", "webp", "pdf"}
ALLOWED_MAGIC = {
    b"\xff\xd8\xff": "jpg",          # JPEG
    b"\x89PNG\r\n\x1a\n": "png",     # PNG
    b"RIFF": "webp",                  # WEBP (validated by next 4 bytes)
    b"%PDF": "pdf",                  # PDF
}
MAX_FILE_SIZE = 5 * 1024 * 1024  # 5MB
PROFILE_IMAGE_EXT = {"jpg", "jpeg", "png"}
PROFILE_IMAGE_MAX_SIZE = 20 * 1024 * 1024
COMPLAINT_ATTACHMENT_EXT = {"jpg", "jpeg", "png", "webp", "pdf", "mp4", "mov", "webm", "avi", "m4v"}
COMPLAINT_ATTACHMENT_MAX_FILES = 5
COMPLAINT_ATTACHMENT_MAX_TOTAL_BYTES = 100 * 1024 * 1024
COMPLAINT_DAILY_LIMIT = 5
COMPLAINT_ALLOWANCE_MAX = COMPLAINT_DAILY_LIMIT

REQUEST_TYPES = ["Certificate of Indigency", "Certificate of Residency", "Barangay Clearance"]
CIVIL_STATUS_OPTIONS = ["Single", "Married", "Widowed", "Separated", "Divorced"]
VALID_ID_TYPES = [
    "PhilSys / National ID", "UMID", "Driver's License", "Passport", "Voter's ID",
    "Postal ID", "SSS ID", "PhilHealth ID", "Senior Citizen ID", "PWD ID",
    "School ID", "Barangay ID", "Other",
]

# Certificate of Indigency has its own purpose list (replaces the free-text
# "Purpose of request" field) — each purpose has its own attachment
# requirements, shown to the resident as they pick one. Always free.
INDIGENCY_PURPOSES = [
    "Financial Assistance", "Medical Assistance", "Burial Assistance",
    "Death Residency", "Scholarship ID", "Philhealth", "P.A.O.",
    "First Time Job Seeker", "Bail Bond", "Cohabitation",
]
INDIGENCY_REQUIREMENTS = {
    "Financial Assistance": ["Xerox ID with barangay address"],
    "Medical Assistance": ["Xerox ID with barangay address"],
    "Burial Assistance": ["ID of deceased", "ID of representative", "Death certificate"],
    "Death Residency": ["ID of deceased", "ID of representative", "Death certificate"],
    "Scholarship ID": ["ID with barangay address"],
    "Philhealth": ["ID with barangay address"],
    "P.A.O.": ["ID with barangay address"],
    "First Time Job Seeker": ["ID with barangay address"],
    "Bail Bond": ["Court order", "Xerox ID of accused", "ID of representative"],
    "Cohabitation": ["Affidavit of cohabitation", "ID xerox"],
}

# Barangay Clearance also has its own purpose list, each with its own fee
# (replaces both the free-text purpose field and the flat fee for this type),
# alphabetized. None = fee to confirm at the office (not specified by the
# barangay's posted fee schedule).
BARANGAY_CLEARANCE_PURPOSES = [
    ("Application for Business", 200),
    ("Application for GSIS / SSS / Pag-IBIG / BIR / Driver's License / Security Guard", 100),
    ("Application for Marriage License", 100),
    ("Application for On-the-Job Training", 50),
    ("Application for PVAO", 50),
    ("Application for Vehicle Registration / Franchise", 200),
    ("Bank Requirement", 100),
    ("Cable Installation (per meter)", 100),
    ("Excavation Permit", None),
    ("Firearms Permit / License (per unit)", 200),
    ("International Employment", 200),
    ("Loan Application", 200),
    ("Local Employment", 50),
    ("Passport / Visa Requirement", 200),
    ("Pipe Laying", 3000),
    ("Post Erection (per cost)", 3000),
    ("Postal I.D.", 100),
    ("Residential (per cubic meter)", 100),
    ("Student / Indigent / Senior Citizen / Scholarship / PWD", 0),
]
BARANGAY_CLEARANCE_FEE_MAP = dict(BARANGAY_CLEARANCE_PURPOSES)
FREE_TIER_CLEARANCE_PURPOSE = "Student / Indigent / Senior Citizen / Scholarship / PWD"

# Certificate of Residency: PHP 50, free for registered voters. The resident pays
# at the Treasurer FIRST, then presents the official receipt at the Secretary to
# claim the document. The system does not track payment (see PRD limitations).
RESIDENCY_FEE = 50

# Where to claim the finished document, and whether payment is involved there.
CLAIM_LOCATIONS = {
    "Certificate of Indigency": "Barangay Secretary Office",
    "Certificate of Residency": "Barangay Secretary Office",
    "Barangay Clearance": "Barangay Treasurer Office",
}
DEFAULT_CLAIM_LOCATION = "Barangay Secretary Office"
# Document types whose fee is paid at a DIFFERENT office than the one that releases them.
PAYMENT_LOCATIONS = {
    "Certificate of Residency": "Barangay Treasurer Office",
}

# ---- Staff offices -----------------------------------------------------------
# Single source of truth for who may handle what. A staff account belongs to exactly
# one office; a staff member with no office can access nothing (fail closed).
# Admins are not offices and can access everything.
OFFICES = {
    "treasurer": {
        "label": "Barangay Treasurer",
        "requests": ["Barangay Clearance"],
        "complaints": False,
    },
    "secretary": {
        "label": "Barangay Secretary",
        "requests": ["Certificate of Indigency", "Certificate of Residency"],
        "complaints": False,
    },
    "bpso": {
        "label": "BPSO (Public Safety)",
        "requests": [],
        "complaints": True,
    },
}

COMPLAINT_CATEGORIES = [
    "Noise Complaint", "Neighbor Dispute", "Illegal Parking",
    "Sanitation", "Public Safety", "Domestic Concern", "Others",
]
REQUEST_STATUSES = ["Submitted", "Under Review", "Processing", "Approved", "Rejected", "Completed"]
COMPLAINT_STATUSES = ["Pending", "Under Review", "Scheduled", "Resolved", "Closed", "Rejected"]
PRIORITY_LEVELS = ["High", "Normal"]
COMPLAINT_STATUS_COLORS = {
    "Pending": "#F0A93F",
    "Under Review": "#2451B0",
    "Scheduled": "#0EA5E9",
    "Resolved": "#2FBF71",
    "Closed": "#64748B",
    "Rejected": "#E5484D",
}
REQUEST_STATUS_COLORS = {
    "Submitted": "#F0A93F",
    "Under Review": "#2451B0",
    "Processing": "#0EA5E9",
    "Approved": "#2FBF71",
    "Rejected": "#E5484D",
    "Completed": "#0F9D6E",
}

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key-change-in-production")
_db_ready = False
_db_startup_lock = threading.Lock()


@app.before_request
def _ensure_database_ready():
    global _db_ready
    if _db_ready:
        return
    with _db_startup_lock:
        if _db_ready:
            return
        if os.path.isfile(dbh.DB_PATH):
            dbh.migrate_db()
        else:
            dbh.init_db()
        _db_ready = True


@app.before_request
def _automatic_backup():
    """Daily automatic database backup (see db.auto_backup_if_due). Never blocks a request."""
    if request.endpoint == "profile":
        request.max_content_length = PROFILE_IMAGE_MAX_SIZE + 1024 * 1024
    dbh.auto_backup_if_due()
app.config["MAX_CONTENT_LENGTH"] = MAX_FILE_SIZE + 1024


@app.after_request
def _protect_password_reset_response(response):
    if request.endpoint == "reset_password":
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
    return response


# ---------------------------------------------------------------- helpers --

def current_user():
    if "user_id" not in session:
        return None
    conn = dbh.get_db()
    row = conn.execute("SELECT * FROM users WHERE id=?", (session["user_id"],)).fetchone()
    conn.close()
    return row


def configured_minutes(name, default, minimum=1):
    try:
        return max(minimum, int(os.environ.get(name, str(default))))
    except ValueError:
        app.logger.error("%s must be an integer; using the default.", name)
        return default


def _timestamp(value):
    return datetime.fromisoformat(value) if value else None


def _token_digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _password_reset_link(token):
    base_url = (
        os.environ.get("APP_BASE_URL", "").strip()
        or os.environ.get("RENDER_EXTERNAL_URL", "").strip()
        or request.url_root
    )
    try:
        parsed = urlsplit(base_url)
        hostname = (parsed.hostname or "").lower()
    except ValueError as exc:
        raise email_service.EmailConfigurationError("APP_BASE_URL is invalid.") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or (parsed.scheme != "https" and hostname not in {"localhost", "127.0.0.1", "::1"})
    ):
        raise email_service.EmailConfigurationError(
            "APP_BASE_URL must be a public HTTPS origin."
        )
    return base_url.rstrip("/") + url_for("reset_password", token=token)


def _otp_digest(value):
    return hmac.new(app.secret_key.encode("utf-8"), value.encode("utf-8"), hashlib.sha256).hexdigest()


def _normalize_ph_mobile(value):
    digits = re.sub(r"[\s()-]", "", value)
    if digits.startswith("09") and len(digits) == 11:
        digits = "+63" + digits[1:]
    elif digits.startswith("639") and len(digits) == 12:
        digits = "+" + digits
    if not re.fullmatch(r"\+639\d{9}", digits):
        return None
    return digits


def concern_allowance(conn, resident_id, now=None):
    now = now or datetime.now()
    row = conn.execute(
        "SELECT * FROM resident_concern_allowances WHERE user_id=?", (resident_id,)
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO resident_concern_allowances (user_id, remaining) VALUES (?,?)",
            (resident_id, COMPLAINT_ALLOWANCE_MAX),
        )
        row = conn.execute(
            "SELECT * FROM resident_concern_allowances WHERE user_id=?", (resident_id,)
        ).fetchone()

    expires_at = _timestamp(row["cooldown_expires_at"])
    if expires_at and expires_at <= now:
        restored = COMPLAINT_ALLOWANCE_MAX - row["remaining"]
        conn.execute(
            "UPDATE resident_concern_allowances SET remaining=?, cooldown_started_at=NULL, "
            "cooldown_expires_at=NULL WHERE user_id=?",
            (COMPLAINT_ALLOWANCE_MAX, resident_id),
        )
        conn.execute(
            "INSERT INTO concern_allowance_events (user_id, event_type, slots, created_at) "
            "VALUES (?,'cooldown_refill',?,?)",
            (resident_id, restored, now.isoformat(timespec="seconds")),
        )
        row = conn.execute(
            "SELECT * FROM resident_concern_allowances WHERE user_id=?", (resident_id,)
        ).fetchone()
    cooldown_seconds = 0
    expires_at = _timestamp(row["cooldown_expires_at"])
    if row["remaining"] == 0 and expires_at:
        cooldown_seconds = max(0, int((expires_at - now).total_seconds()))
    return row, cooldown_seconds


def restore_resolved_concern_slot(conn, complaint, now=None):
    now = now or datetime.now()
    cur = conn.execute(
        "INSERT OR IGNORE INTO concern_allowance_events "
        "(user_id, complaint_id, event_type, slots, created_at) "
        "VALUES (?,?,'resolved_restoration',0,?)",
        (complaint["resident_id"], complaint["id"], now.isoformat(timespec="seconds")),
    )
    if cur.rowcount == 0:
        return False
    allowance, _ = concern_allowance(conn, complaint["resident_id"], now)
    new_remaining = min(COMPLAINT_ALLOWANCE_MAX, allowance["remaining"] + 1)
    restored = new_remaining - allowance["remaining"]
    if restored:
        conn.execute(
            "UPDATE resident_concern_allowances SET remaining=?, cooldown_started_at=NULL, "
            "cooldown_expires_at=NULL WHERE user_id=?",
            (new_remaining, complaint["resident_id"]),
        )
        dbh.notify(
            conn,
            complaint["resident_id"],
            "Your resolved concern restored one available concern submission.",
            url_for("new_complaint"),
        )
    conn.execute(
        "UPDATE concern_allowance_events SET slots=? WHERE id=?",
        (restored, cur.lastrowid),
    )
    return bool(restored)


def issue_login_otp(user, now=None):
    now = now or datetime.now()
    ttl_minutes = configured_minutes("ADMIN_OTP_TTL_MINUTES", 5)
    resend_seconds = configured_minutes("ADMIN_OTP_RESEND_SECONDS", 60)
    max_sends = configured_minutes("ADMIN_OTP_MAX_SENDS_PER_HOUR", 5)
    conn = dbh.get_db()
    conn.execute("BEGIN IMMEDIATE")
    row = conn.execute(
        "SELECT * FROM admin_otp_challenges WHERE user_id=?", (user["id"],)
    ).fetchone()
    window_started = _timestamp(row["window_started_at"]) if row else None
    sent_at = _timestamp(row["sent_at"]) if row else None
    if window_started and now - window_started < timedelta(hours=1):
        sends = row["sends_in_window"]
        next_window_seconds = max(1, int((window_started + timedelta(hours=1) - now).total_seconds()))
    else:
        sends = 0
        next_window_seconds = 0
        window_started = now
    wait_seconds = max(0, int((sent_at + timedelta(seconds=resend_seconds) - now).total_seconds())) if sent_at else 0
    if sends >= max_sends:
        conn.rollback()
        conn.close()
        return False, next_window_seconds
    if wait_seconds:
        conn.rollback()
        conn.close()
        return False, wait_seconds

    email_code = f"{secrets.randbelow(1_000_000):06d}"
    expires_at = now + timedelta(minutes=ttl_minutes)
    conn.execute(
        "INSERT INTO admin_otp_challenges "
        "(user_id, email_otp_hash, mode, expires_at, sent_at, attempts, "
        "window_started_at, sends_in_window, used_at) "
        "VALUES (?,?,?,?,?,0,?,?,NULL) ON CONFLICT(user_id) DO UPDATE SET "
        "email_otp_hash=excluded.email_otp_hash, mode=excluded.mode, "
        "expires_at=excluded.expires_at, "
        "sent_at=excluded.sent_at, "
        "attempts=0, window_started_at=excluded.window_started_at, "
        "sends_in_window=excluded.sends_in_window, used_at=NULL",
        (
            user["id"], _otp_digest(email_code), "email_only",
            expires_at.isoformat(timespec="seconds"), now.isoformat(timespec="seconds"),
            window_started.isoformat(timespec="seconds"), sends + 1,
        ),
    )
    conn.commit()
    conn.close()

    try:
        message = (
            f"Your BarangayConnect email verification code is {email_code}.\n\n"
            f"It expires in {ttl_minutes} minutes. If you did not request this code, "
            "you can ignore this email."
        )
        email_service.send_email(
            user["email"], "BarangayConnect email verification code", message
        )
    except (
        email_service.EmailConfigurationError,
        email_service.smtplib.SMTPException,
        OSError,
        ValueError,
        RuntimeError,
    ):
        conn = dbh.get_db()
        conn.execute(
            "UPDATE admin_otp_challenges SET email_otp_hash=NULL, "
            "expires_at=?, used_at=? WHERE user_id=? AND email_otp_hash=?",
            (
                now.isoformat(timespec="seconds"), now.isoformat(timespec="seconds"),
                user["id"], _otp_digest(email_code),
            ),
        )
        conn.commit()
        conn.close()
        raise
    return True, 0


def otp_page_context(conn, user_id, now=None):
    now = now or datetime.now()
    row = conn.execute(
        "SELECT expires_at, sent_at FROM admin_otp_challenges WHERE user_id=?",
        (user_id,),
    ).fetchone()
    if not row:
        return {"otp_expires_in": 0, "otp_resend_wait": 0}
    expires_at = _timestamp(row["expires_at"])
    sent_at = _timestamp(row["sent_at"])
    resend_seconds = configured_minutes("ADMIN_OTP_RESEND_SECONDS", 60)
    return {
        "otp_expires_in": max(0, int((expires_at - now).total_seconds())) if expires_at else 0,
        "otp_resend_wait": max(
            0, int((sent_at + timedelta(seconds=resend_seconds) - now).total_seconds())
        ) if sent_at else 0,
    }


def login_required(*roles):
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            user = current_user()
            if not user:
                flash("Please log in to continue.", "error")
                return redirect(url_for("login"))
            if roles and user["role"] not in roles:
                abort(403)
            return f(user, *args, **kwargs)
        return wrapped
    return decorator


def handled_request_types(user):
    """Request types this user may see/process. [] for residents and office-less staff."""
    if user["role"] == "admin":
        return list(REQUEST_TYPES)
    if user["role"] == "staff":
        cfg = OFFICES.get(user["office"])
        return list(cfg["requests"]) if cfg else []
    return []


def can_handle_request_type(user, rtype):
    return rtype in handled_request_types(user)


def can_handle_complaints(user):
    if user["role"] == "admin":
        return True
    if user["role"] == "staff":
        cfg = OFFICES.get(user["office"])
        return bool(cfg and cfg["complaints"])
    return False


def offices_for_request_type(rtype):
    return [k for k, v in OFFICES.items() if rtype in v["requests"]]


def offices_for_complaints():
    return [k for k, v in OFFICES.items() if v["complaints"]]


def _in_clause(values):
    """('IN (?,?)', [..]) - or a clause that matches nothing when values is empty."""
    if not values:
        return "IN (NULL)", []
    return f"IN ({','.join('?' * len(values))})", list(values)


def active_staff_for_offices(conn, offices):
    clause, params = _in_clause(offices)
    return conn.execute(
        f"SELECT * FROM users WHERE role='staff' AND status='active' AND office {clause} ORDER BY full_name",
        params,
    ).fetchall()


def valid_assignee(conn, staff_id, offices):
    """True if staff_id is an active staff account belonging to one of the offices."""
    clause, params = _in_clause(offices)
    return conn.execute(
        f"SELECT 1 FROM users WHERE id=? AND role='staff' AND status='active' AND office {clause}",
        [staff_id] + params,
    ).fetchone() is not None


def valid_email(email):
    return re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email or "") is not None


def valid_password(pw):
    # Minimum length/complexity: >=8 chars, at least one letter and one digit
    if not pw or len(pw) < 8:
        return False
    return bool(re.search(r"[A-Za-z]", pw)) and bool(re.search(r"\d", pw))


def _determine_file_kind(file_storage, ext=None):
    """Return the detected file kind using header bytes, including WEBP and common video containers."""
    file_storage.stream.seek(0)
    head = file_storage.stream.read(12)
    file_storage.stream.seek(0)
    if not head:
        return None
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"RIFF") and len(head) >= 12 and head[8:12] == b"WEBP":
        return "webp"
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"RIFF") and len(head) >= 12 and head[8:12] == b"AVI ":
        return "avi"
    if head.startswith(b"\x1A\x45\xDF\xA3"):
        return "webm"
    if head.startswith(b"\x00\x00\x00") or head.startswith(b"\x00\x00\x00"):
        # MP4/MOV file signatures are more reliably checked by the file extension in practice.
        pass
    if ext in {"mp4", "m4v", "mov"}:
        return ext
    return None


def validate_complaint_attachment_files(files):
    selected = []
    total_bytes = 0
    for f in files or []:
        if not f or not getattr(f, "filename", None):
            continue
        name = f.filename.strip()
        if not name:
            continue
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext not in COMPLAINT_ATTACHMENT_EXT:
            raise ValueError("Unsupported file type. Allowed files: JPG, PNG, WEBP, PDF, MP4, MOV, WEBM, AVI, or M4V.")
        f.stream.seek(0, os.SEEK_END)
        size = f.stream.tell()
        f.stream.seek(0)
        if size <= 0:
            raise ValueError(f"{name} is empty and cannot be uploaded.")
        if size > MAX_FILE_SIZE:
            raise ValueError(f"{name} exceeds the 5 MB individual file limit.")
        total_bytes += size
        selected.append(f)
    if len(selected) > COMPLAINT_ATTACHMENT_MAX_FILES:
        raise ValueError(f"You can attach up to {COMPLAINT_ATTACHMENT_MAX_FILES} files only.")
    if total_bytes > COMPLAINT_ATTACHMENT_MAX_TOTAL_BYTES:
        raise ValueError("Combined attachments must not exceed 100 MB.")
    return selected


def normalize_complaint_location(location):
    """Normalize a complaint location so same purok/location comparisons are stable."""
    return dbh.normalize_complaint_location(location)


def open_complaint_statuses():
    return [status for status in COMPLAINT_STATUSES if status not in {"Resolved", "Closed", "Rejected"}]


def resident_duplicate_complaint(conn, resident_id, category, location):
    if not category or not location:
        return None
    normalized = normalize_complaint_location(location)
    if not normalized:
        return None
    rows = conn.execute(
        "SELECT * FROM complaints WHERE resident_id=? AND category=? "
        "AND status NOT IN ('Resolved','Closed','Rejected') ORDER BY created_at DESC",
        (resident_id, category),
    ).fetchall()
    for row in rows:
        if normalize_complaint_location(row["location"]) == normalized:
            return row
    return None


def resident_duplicate_request(conn, resident_id, request_type, purpose):
    """Find a matching request that has not been rejected or completed/released."""
    if not request_type:
        return None
    return conn.execute(
        "SELECT * FROM requests WHERE resident_id=? AND request_type=? "
        "AND LOWER(TRIM(COALESCE(purpose,'')))=LOWER(TRIM(?)) "
        "AND status NOT IN ('Rejected','Completed') "
        "ORDER BY created_at DESC LIMIT 1",
        (resident_id, request_type, purpose or ""),
    ).fetchone()


def community_issue_rows(conn, user_id, include_single=False):
    issues = conn.execute(
        "SELECT ci.*, COUNT(c.id) complaint_count, COUNT(DISTINCT c.resident_id) distinct_residents "
        "FROM community_issues ci JOIN complaints c ON c.community_issue_id=ci.id "
        "GROUP BY ci.id ORDER BY ci.updated_at DESC, ci.id DESC"
    ).fetchall()
    follows = {
        row["community_issue_id"]
        for row in conn.execute(
            "SELECT community_issue_id FROM community_issue_follows WHERE user_id=?",
            (user_id,),
        ).fetchall()
    }
    grouped = []
    for issue in issues:
        if not include_single and issue["distinct_residents"] < 2:
            continue
        grouped.append({
            "id": issue["id"],
            "category": issue["category"],
            "location": issue["location"],
            "normalized_location": issue["normalized_location"],
            "status": issue["status"],
            "created_at": issue["created_at"],
            "updated_at": issue["updated_at"],
            "complaint_count": issue["complaint_count"],
            "distinct_residents": issue["distinct_residents"],
            "summary": f"Residents reported {issue['category']} in {issue['location'] or 'an unspecified location'}.",
            "is_following": issue["id"] in follows,
        })
    return grouped


def related_complaint_clusters(conn, user_id):
    return community_issue_rows(conn, user_id)


def notify_community_issue_followers(conn, issue_id, status, message=None, exclude_user_ids=()):
    excluded = set(exclude_user_ids)
    followers = conn.execute(
        "SELECT user_id FROM community_issue_follows WHERE community_issue_id=? "
        "UNION SELECT resident_id AS user_id FROM complaints WHERE community_issue_id=?",
        (issue_id, issue_id),
    ).fetchall()
    issue = conn.execute("SELECT category FROM community_issues WHERE id=?", (issue_id,)).fetchone()
    if not issue:
        return
    resident_count = conn.execute(
        "SELECT COUNT(DISTINCT resident_id) count FROM complaints WHERE community_issue_id=?",
        (issue_id,),
    ).fetchone()["count"]
    for follower in followers:
        if follower["user_id"] in excluded:
            continue
        report = conn.execute(
            "SELECT id FROM complaints WHERE community_issue_id=? AND resident_id=? "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (issue_id, follower["user_id"]),
        ).fetchone()
        link = (
            url_for("community_issue_detail", issue_id=issue_id)
            if resident_count >= 2 or not report
            else url_for("view_complaint", complaint_id=report["id"])
        )
        notification = f"Community concern ({issue['category']}) status updated to {status}."
        if message:
            notification += f" Update: {message}"
        dbh.notify(
            conn,
            follower["user_id"],
            notification,
            link,
        )


def transition_community_issue(conn, issue_id, status, actor_name, remarks=None, resident_message=None):
    issue = conn.execute("SELECT * FROM community_issues WHERE id=?", (issue_id,)).fetchone()
    if not issue:
        return []
    reports = conn.execute(
        "SELECT id, reference_no, resident_id, status FROM complaints WHERE community_issue_id=?",
        (issue_id,),
    ).fetchall()
    changed = [report for report in reports if report["status"] != status]
    issue_changed = issue["status"] != status
    if issue_changed or resident_message:
        conn.execute(
            "UPDATE community_issues SET status=?, updated_at=datetime('now') WHERE id=?",
            (status, issue_id),
        )
        conn.execute(
            "INSERT INTO community_issue_updates "
            "(community_issue_id, status, changed_by, message) VALUES (?,?,?,?)",
            (issue_id, status, actor_name, resident_message),
        )
    if issue_changed or resident_message:
        notify_community_issue_followers(
            conn, issue_id, status, message=resident_message
        )
    if changed:
        conn.execute(
            "UPDATE complaints SET status=?, updated_at=datetime('now') "
            "WHERE community_issue_id=? AND status<>?",
            (status, issue_id, status),
        )
        for report in changed:
            dbh.log_status(conn, "complaint", report["id"], status, actor_name, remarks)
            if status == "Resolved" and report["status"] != "Rejected":
                restore_resolved_concern_slot(conn, report)
    return reports if issue_changed else changed


def community_issue_manager_data(conn, status_filter="", verification_filter="all", search=""):
    issue_rows = conn.execute(
        "SELECT ci.id, ci.status, ci.updated_at, COUNT(c.id) report_count, "
        "COUNT(DISTINCT c.resident_id) resident_count, "
        "SUM(CASE WHEN c.is_verified=0 THEN 1 ELSE 0 END) pending_verification "
        "FROM community_issues ci JOIN complaints c ON c.community_issue_id=ci.id "
        "GROUP BY ci.id"
    ).fetchall()
    stats = {
        "pending_review": sum(row["pending_verification"] > 0 for row in issue_rows),
        "clustered": sum(row["resident_count"] > 1 for row in issue_rows),
        "in_progress": sum(row["status"] in {"Under Review", "Scheduled"} for row in issue_rows),
        "resolved": sum(row["status"] in {"Resolved", "Closed"} for row in issue_rows),
    }
    clauses, params = [], []
    if status_filter in COMPLAINT_STATUSES:
        clauses.append("ci.status=?")
        params.append(status_filter)
    if verification_filter == "pending":
        clauses.append("SUM(CASE WHEN c.is_verified=0 THEN 1 ELSE 0 END)>0")
    elif verification_filter == "verified":
        clauses.append("SUM(CASE WHEN c.is_verified=0 THEN 1 ELSE 0 END)=0")
    if search:
        clauses.append("(ci.category LIKE ? OR ci.location LIKE ?)")
        params.extend((f"%{search}%", f"%{search}%"))
    query = (
        "SELECT ci.id, ci.category, ci.location, ci.status, ci.created_at, ci.updated_at, "
        "COUNT(c.id) report_count, COUNT(DISTINCT c.resident_id) resident_count, "
        "SUM(CASE WHEN c.is_verified=0 THEN 1 ELSE 0 END) pending_verification "
        "FROM community_issues ci JOIN complaints c ON c.community_issue_id=ci.id "
        "GROUP BY ci.id"
    )
    if clauses:
        query += " HAVING " + " AND ".join(clauses)
    query += " ORDER BY pending_verification DESC, ci.updated_at DESC, ci.id DESC"
    return stats, conn.execute(query, params).fetchall()


def save_upload(file_storage, subdir, allowed_ext=None, max_size=MAX_FILE_SIZE, strict_type=False):
    if not file_storage or file_storage.filename == "":
        return None
    allowed_ext = allowed_ext or ALLOWED_EXT
    filename = file_storage.filename
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in allowed_ext:
        if allowed_ext == PROFILE_IMAGE_EXT:
            allowed_message = "Only JPG and PNG images are allowed."
        elif {"jpg", "jpeg", "png", "webp", "pdf"}.issubset(allowed_ext):
            allowed_message = "Only JPG, PNG, WEBP, and PDF files are allowed."
        else:
            allowed_message = "Only JPG, PNG, WEBP, PDF, MP4, MOV, WEBM, AVI, and M4V files are allowed."
        raise ValueError(allowed_message)
    file_storage.stream.seek(0, os.SEEK_END)
    file_size = file_storage.stream.tell()
    file_storage.stream.seek(0)
    if file_size > max_size:
        raise ValueError(f"File must be no larger than {max_size // (1024 * 1024)} MB.")
    matched_kind = _determine_file_kind(file_storage, ext)
    if not matched_kind:
        raise ValueError("File content does not match its declared type.")
    if strict_type:
        expected = "jpg" if ext == "jpeg" else ext
        if matched_kind != expected:
            raise ValueError("File content does not match its declared type.")
    target_dir = os.path.join(UPLOAD_DIR, subdir)
    os.makedirs(target_dir, exist_ok=True)
    stored_name = f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{random.randint(1000,9999)}_{secure_filename(filename)}"
    file_storage.save(os.path.join(target_dir, stored_name))
    return filename, stored_name


def format_file_size(size):
    if size is None:
        return "Unknown size"
    units = ["B", "KB", "MB", "GB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def notif_count(conn, user_id):
    return conn.execute(
        "SELECT COUNT(*) c FROM notifications WHERE user_id=? AND is_read=0", (user_id,)
    ).fetchone()["c"]


@app.context_processor
def inject_globals():
    user = current_user()
    unread = 0
    new_requests = 0
    new_complaints = 0
    daily_concerns_remaining = 0
    if user:
        conn = dbh.get_db()
        unread = notif_count(conn, user["id"])
        if user["role"] == "resident":
            allowance, _ = concern_allowance(conn, user["id"])
            daily_concerns_remaining = allowance["remaining"]
            conn.commit()
        types = handled_request_types(user)
        if types:
            clause, params = _in_clause(types)
            new_requests = conn.execute(
                f"SELECT COUNT(*) c FROM requests WHERE status='Submitted' AND request_type {clause}",
                params,
            ).fetchone()["c"]
        if can_handle_complaints(user):
            new_complaints = conn.execute(
                "SELECT COUNT(*) c FROM complaints WHERE status='Pending'"
            ).fetchone()["c"]
        conn.close()
    return dict(
        current_user=user, unread_count=unread, now=datetime.now(),
        nav_requests=bool(user and handled_request_types(user)),
        nav_complaints=bool(user and can_handle_complaints(user)),
        nav_new_requests=new_requests, nav_new_complaints=new_complaints,
        nav_daily_concerns_remaining=daily_concerns_remaining,
        office_labels={k: v["label"] for k, v in OFFICES.items()},
    )


# ------------------------------------------------------------------ setup --

@app.route("/")
def index():
    user = current_user()
    if user:
        return redirect(url_for(f"{user['role']}_dashboard"))

    # Public landing page for visitors who are not logged in.
    # Barangay details come from Admin > Settings; the page must never fail
    # just because those settings are missing, so fall back to safe defaults.
    info = {}
    try:
        conn = dbh.get_db()
        for key in ("barangay_name", "barangay_address", "barangay_main_number"):
            info[key] = dbh.get_setting(conn, key, "")
        conn.close()
    except Exception:
        info = {}
    return render_template(
        "landing.html",
        barangay_name=info.get("barangay_name") or "Your Barangay",
        barangay_address=info.get("barangay_address", ""),
        barangay_main_number=info.get("barangay_main_number", ""),
        year=datetime.now().year,
    )


# -------------------------------------------------------------------- auth --

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")
        address = request.form.get("address", "").strip()
        raw_contact = request.form.get("contact_number", "").strip()
        contact = _normalize_ph_mobile(raw_contact) if raw_contact else None

        errors = []
        if not full_name:
            errors.append("Full name is required.")
        if not valid_email(email):
            errors.append("Please enter a valid email address.")
        if not valid_password(password):
            errors.append("Password must be at least 8 characters and include a letter and a number.")
        if password != confirm:
            errors.append("Passwords do not match.")
        if not address:
            errors.append("Address is required.")
        if raw_contact and not contact:
            errors.append("Enter a valid Philippine mobile number, such as 0917 123 4567.")

        conn = dbh.get_db()
        if not errors and conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone():
            errors.append("An account with this email already exists.")

        if errors:
            conn.close()
            for e in errors:
                flash(e, "error")
            return render_template("register.html", form=request.form)

        pw_hash = generate_password_hash(password)
        cur = conn.execute(
            "INSERT INTO users (full_name, email, password_hash, role, address, contact_number) "
            "VALUES (?,?,?,?,?,?)",
            (full_name, email, pw_hash, "resident", address, contact),
        )
        conn.execute(
            "INSERT INTO resident_concern_allowances (user_id, remaining) VALUES (?,?)",
            (cur.lastrowid, COMPLAINT_ALLOWANCE_MAX),
        )
        dbh.log_action(conn, {"id": cur.lastrowid, "full_name": full_name}, "Resident self-registered", target=email)
        conn.commit()
        conn.close()
        flash("Account created successfully. You can now log in.", "success")
        return redirect(url_for("login"))

    return render_template("register.html", form={})


@app.route("/login", methods=["GET", "POST"])
def login():
    if "user_id" in session:
        user = current_user()
        if user:
            return redirect(url_for(f"{user['role']}_dashboard"))
    session.pop("pending_mfa_user", None)
    session.pop("pending_reset_user", None)

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        conn = dbh.get_db()
        user = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()

        if not user or user["status"] != "active":
            conn.close()
            flash("Invalid credentials or inactive account.", "error")
            return render_template("login.html")

        if dbh.is_locked(user):
            conn.close()
            flash("This account is temporarily locked due to repeated failed attempts. Try again later.", "error")
            return render_template("login.html")

        if not check_password_hash(user["password_hash"], password):
            locked = dbh.register_failed_login(conn, user)
            conn.close()
            if locked:
                flash("Too many failed attempts. Account locked for 15 minutes.", "error")
            else:
                flash("Invalid credentials.", "error")
            return render_template("login.html")

        dbh.clear_failed_logins(conn, user["id"])

        conn.close()

        try:
            sent, wait_seconds = issue_login_otp(user)
        except (
            email_service.EmailConfigurationError,
            email_service.smtplib.SMTPException,
            OSError,
            ValueError,
            RuntimeError,
        ):
            app.logger.exception("Could not deliver login verification code.")
            flash(
                "We could not deliver the verification code. Check the email configuration and try again.",
                "error",
            )
            return render_template("login.html"), 503
        challenge_conn = dbh.get_db()
        challenge = challenge_conn.execute(
            "SELECT expires_at, email_otp_hash "
            "FROM admin_otp_challenges WHERE user_id=?", (user["id"],)
        ).fetchone()
        challenge_conn.close()
        if not sent and (
            not challenge
            or not _timestamp(challenge["expires_at"])
            or _timestamp(challenge["expires_at"]) <= datetime.now()
            or not challenge["email_otp_hash"]
        ):
            flash(
                f"Please wait {wait_seconds} seconds before requesting another verification code.",
                "error",
            )
            return render_template("login.html"), 429
        session["pending_mfa_user"] = user["id"]
        if sent:
            flash("An email verification code was sent.", "success")
        else:
            flash("A valid email code was recently sent. Check your email or wait before requesting another.", "info")
        return redirect(url_for("verify_otp"))

    return render_template("login.html")


@app.route("/verify-otp", methods=["GET", "POST"])
def verify_otp():
    if "pending_mfa_user" not in session:
        return redirect(url_for("login"))
    user_id = session["pending_mfa_user"]
    if request.method == "POST":
        email_code = request.form.get("email_otp", "").strip()
        conn = dbh.get_db()
        conn.execute("BEGIN IMMEDIATE")
        challenge = conn.execute(
            "SELECT * FROM admin_otp_challenges WHERE user_id=?", (user_id,)
        ).fetchone()
        now = datetime.now()
        if not challenge or challenge["used_at"] or not challenge["email_otp_hash"]:
            conn.rollback()
            conn.close()
            flash("No active verification code. Request a new code.", "error")
            return redirect(url_for("verify_otp"))
        expires_at = _timestamp(challenge["expires_at"])
        if not expires_at or now >= expires_at:
            conn.rollback()
            conn.close()
            flash("The verification code expired. Request a new code.", "error")
            return redirect(url_for("verify_otp"))
        if challenge["attempts"] >= 5:
            conn.rollback()
            conn.close()
            flash("Too many incorrect attempts. Request a new code.", "error")
            return redirect(url_for("verify_otp"))
        email_valid = bool(
            challenge["email_otp_hash"]
            and len(email_code) == 6 and email_code.isdigit()
            and hmac.compare_digest(_otp_digest(email_code), challenge["email_otp_hash"])
        )
        if not email_valid:
            conn.execute(
                "UPDATE admin_otp_challenges SET attempts=attempts+1 WHERE user_id=?",
                (user_id,),
            )
            conn.commit()
            conn.close()
            flash("Incorrect email verification code.", "error")
            return redirect(url_for("verify_otp"))
        verified_user = conn.execute(
            "SELECT id, role, must_reset_pw "
            "FROM users WHERE id=? AND status='active'",
            (user_id,),
        ).fetchone()
        if not verified_user:
            conn.execute(
                "UPDATE admin_otp_challenges SET email_otp_hash=NULL, used_at=? WHERE user_id=?",
                (now.isoformat(timespec="seconds"), user_id),
            )
            conn.commit()
            conn.close()
            session.pop("pending_mfa_user", None)
            flash("This account is no longer active.", "error")
            return redirect(url_for("login"))
        conn.execute(
            "UPDATE admin_otp_challenges SET email_otp_hash=NULL, used_at=? WHERE user_id=?",
            (now.isoformat(timespec="seconds"), user_id),
        )
        conn.commit()
        conn.close()
        session.pop("pending_mfa_user", None)
        if verified_user["must_reset_pw"]:
            session["pending_reset_user"] = user_id
            return redirect(url_for("first_login_reset"))
        session["user_id"] = user_id
        session["role"] = verified_user["role"]
        return redirect(url_for(f"{verified_user['role']}_dashboard"))
    conn = dbh.get_db()
    context = otp_page_context(conn, user_id)
    conn.close()
    return render_template("verify_otp.html", **context)


@app.route("/verify-otp/resend", methods=["POST"])
def resend_admin_otp():
    user_id = session.get("pending_mfa_user")
    if not user_id:
        return redirect(url_for("login"))
    conn = dbh.get_db()
    user = conn.execute(
        "SELECT * FROM users WHERE id=? AND status='active'", (user_id,)
    ).fetchone()
    conn.close()
    if not user:
        session.pop("pending_mfa_user", None)
        return redirect(url_for("login"))
    try:
        sent, wait_seconds = issue_login_otp(user)
    except (
        email_service.EmailConfigurationError,
        email_service.smtplib.SMTPException,
        OSError,
        ValueError,
        RuntimeError,
    ):
        app.logger.exception("Could not redeliver login verification code.")
        flash("We could not deliver a new verification code. Check the email configuration and try again.", "error")
    else:
        if sent:
            flash("A new verification code was sent. The previous code is no longer valid.", "success")
        else:
            flash(f"Please wait {wait_seconds} seconds before requesting another code.", "error")
    return redirect(url_for("verify_otp"))


@app.route("/first-login-reset", methods=["GET", "POST"])
def first_login_reset():
    user_id = session.get("pending_reset_user")
    if not user_id:
        return redirect(url_for("login"))
    conn = dbh.get_db()
    user = conn.execute(
        "SELECT * FROM users WHERE id=? AND role IN ('staff','admin') "
        "AND status='active' AND must_reset_pw=1",
        (user_id,),
    ).fetchone()
    conn.close()
    if not user:
        session.pop("pending_reset_user", None)
        return redirect(url_for("login"))
    if request.method == "POST":
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")
        if not valid_password(password):
            flash("Password must be at least 8 characters and include a letter and a number.", "error")
            return render_template("first_login_reset.html")
        if password != confirm:
            flash("Passwords do not match.", "error")
            return render_template("first_login_reset.html")
        conn = dbh.get_db()
        conn.execute("BEGIN IMMEDIATE")
        user = conn.execute(
            "SELECT * FROM users WHERE id=? AND role IN ('staff','admin') "
            "AND status='active' AND must_reset_pw=1",
            (user_id,),
        ).fetchone()
        if not user:
            conn.rollback()
            conn.close()
            session.pop("pending_reset_user", None)
            return redirect(url_for("login"))
        conn.execute(
            "UPDATE users SET password_hash=?, must_reset_pw=0 WHERE id=?",
            (generate_password_hash(password), user_id),
        )
        dbh.log_action(conn, user, "Changed initial password on first login")
        conn.commit()
        conn.close()
        session.pop("pending_reset_user", None)
        session["user_id"] = user_id
        session["role"] = user["role"]
        flash("Password set. Welcome to BarangayConnect.", "success")
        return redirect(url_for(f"{user['role']}_dashboard"))
    return render_template("first_login_reset.html")


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        if not valid_email(email):
            flash("Please enter a valid email address.", "error")
            return render_template("forgot_password.html", email=email), 400
        conn = dbh.get_db()
        conn.execute("BEGIN IMMEDIATE")
        user = conn.execute(
            "SELECT * FROM users WHERE email=? AND status='active'",
            (email,),
        ).fetchone()
        if user:
            rate = conn.execute(
                "SELECT * FROM password_reset_limits WHERE user_id=?", (user["id"],)
            ).fetchone()
            now = datetime.now()
            window_started = _timestamp(rate["window_started_at"]) if rate else None
            last_sent = _timestamp(rate["last_sent_at"]) if rate else None
            if window_started and now - window_started < timedelta(hours=1):
                sends = rate["sends_in_window"]
            else:
                sends = 0
                window_started = now
            min_wait = configured_minutes("PASSWORD_RESET_RESEND_SECONDS", 60)
            max_sends = configured_minutes("PASSWORD_RESET_MAX_SENDS_PER_HOUR", 5)
            wait_seconds = (
                max(0, int((last_sent + timedelta(seconds=min_wait) - now).total_seconds()))
                if last_sent else 0
            )
            if sends >= max_sends or wait_seconds:
                conn.rollback()
                conn.close()
                flash(
                    "If that address belongs to an active account and email delivery is available, "
                    "a reset link will arrive shortly. "
                    "If no email arrives, contact the barangay office.",
                    "info",
                )
                return redirect(url_for("forgot_password"))
            conn.execute(
                "INSERT INTO password_reset_limits (user_id,last_sent_at,window_started_at,sends_in_window) "
                "VALUES (?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET "
                "last_sent_at=excluded.last_sent_at, window_started_at=excluded.window_started_at, "
                "sends_in_window=excluded.sends_in_window",
                (
                    user["id"], now.isoformat(timespec="seconds"),
                    window_started.isoformat(timespec="seconds"), sends + 1,
                ),
            )
            token = secrets.token_urlsafe(32)
            now = datetime.now()
            expires_at = now + timedelta(minutes=configured_minutes("PASSWORD_RESET_TTL_MINUTES", 30))
            timestamp = now.isoformat(timespec="seconds")
            conn.execute(
                "INSERT INTO password_reset_tokens (user_id,token_hash,expires_at,created_at) "
                "VALUES (?,?,?,?)",
                (
                    user["id"], _token_digest(token), expires_at.isoformat(timespec="seconds"), timestamp,
                ),
            )
            token_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.commit()
        conn.close()
        if user:
            try:
                reset_url = _password_reset_link(token)
                email_service.send_email(
                    user["email"],
                    "Reset your BarangayConnect password",
                    "Use the secure link below to choose a new password:\n\n"
                    f"{reset_url}\n\nThis link expires in "
                    f"{configured_minutes('PASSWORD_RESET_TTL_MINUTES', 30)} minutes and can only be used once.\n\n"
                    "If you did not request a reset, you can ignore this email.",
                )
            except (email_service.EmailConfigurationError, email_service.smtplib.SMTPException, OSError, ValueError) as exc:
                detail = str(exc) if isinstance(exc, email_service.EmailConfigurationError) else ""
                smtp_code = getattr(exc, "smtp_code", None)
                app.logger.error(
                    "Password reset email delivery failed (type=%s, smtp_code=%s, detail=%s).",
                    type(exc).__name__,
                    smtp_code,
                    detail,
                )
                conn = dbh.get_db()
                conn.execute(
                    "UPDATE password_reset_tokens SET used_at=? WHERE id=? AND used_at IS NULL",
                    (datetime.now().isoformat(timespec="seconds"), token_id),
                )
                conn.commit()
                conn.close()
            else:
                conn = dbh.get_db()
                timestamp = datetime.now().isoformat(timespec="seconds")
                conn.execute(
                    "UPDATE password_reset_tokens SET used_at=? "
                    "WHERE user_id=? AND id<>? AND used_at IS NULL",
                    (timestamp, user["id"], token_id),
                )
                conn.execute(
                    "UPDATE password_reset_codes SET code_hash=NULL,used_at=? "
                    "WHERE user_id=? AND used_at IS NULL",
                    (timestamp, user["id"]),
                )
                dbh.log_action(conn, user, "Password reset email sent")
                conn.commit()
                conn.close()
        flash(
            "If that address belongs to an active account and email delivery is available, "
            "a reset link will arrive shortly. "
            "If no email arrives, contact the barangay office.",
            "info",
        )
        return redirect(url_for("forgot_password"))
    return render_template("forgot_password.html")


@app.route("/reset-password", methods=["GET", "POST"])
def legacy_reset_password():
    flash("Password recovery now uses secure email links. Request a new link to continue.", "info")
    return redirect(url_for("forgot_password"))


@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    conn = dbh.get_db()
    row = conn.execute(
        "SELECT t.*,u.status AS user_status FROM password_reset_tokens t "
        "JOIN users u ON u.id=t.user_id WHERE t.token_hash=?",
        (_token_digest(token),),
    ).fetchone()
    if (
        not row
        or row["user_status"] != "active"
        or row["used_at"]
        or _timestamp(row["expires_at"]) <= datetime.now()
    ):
        conn.close()
        return render_template("reset_password.html", token_valid=False), 400
    if request.method == "POST":
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")
        if not valid_password(password):
            conn.close()
            flash("Password must be at least 8 characters and include a letter and a number.", "error")
            return render_template("reset_password.html", token_valid=True, token=token)
        if password != confirm:
            conn.close()
            flash("Passwords do not match.", "error")
            return render_template("reset_password.html", token_valid=True, token=token)
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT t.*,u.status AS user_status FROM password_reset_tokens t "
            "JOIN users u ON u.id=t.user_id WHERE t.token_hash=?",
            (_token_digest(token),),
        ).fetchone()
        if (
            not row
            or row["user_status"] != "active"
            or row["used_at"]
            or _timestamp(row["expires_at"]) <= datetime.now()
        ):
            conn.rollback()
            conn.close()
            return render_template("reset_password.html", token_valid=False), 400
        conn.execute(
            "UPDATE users SET password_hash=?,must_reset_pw=0 WHERE id=?",
            (generate_password_hash(password), row["user_id"]),
        )
        timestamp = datetime.now().isoformat(timespec="seconds")
        conn.execute(
            "UPDATE password_reset_tokens SET used_at=? WHERE user_id=? AND used_at IS NULL",
            (timestamp, row["user_id"]),
        )
        conn.execute(
            "UPDATE password_reset_codes SET code_hash=NULL,used_at=? "
            "WHERE user_id=? AND used_at IS NULL",
            (timestamp, row["user_id"]),
        )
        user = conn.execute("SELECT * FROM users WHERE id=?", (row["user_id"],)).fetchone()
        dbh.log_action(conn, user, "Password reset using emailed link")
        conn.commit()
        conn.close()
        flash("Your password was changed. You can now log in.", "success")
        return redirect(url_for("login"))
    conn.close()
    return render_template("reset_password.html", token_valid=True, token=token)


@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("index"))


# --------------------------------------------------------------- resident --

@app.route("/dashboard")
@login_required("resident")
def resident_dashboard(user):
    conn = dbh.get_db()
    requests_ = conn.execute(
        "SELECT * FROM requests WHERE resident_id=? ORDER BY created_at DESC LIMIT 5", (user["id"],)
    ).fetchall()
    complaints_ = conn.execute(
        "SELECT * FROM complaints WHERE resident_id=? ORDER BY created_at DESC LIMIT 5", (user["id"],)
    ).fetchall()
    active_requests = conn.execute(
        "SELECT COUNT(*) c FROM requests WHERE resident_id=? AND status NOT IN ('Completed','Rejected')",
        (user["id"],)
    ).fetchone()["c"]
    pending_complaints = conn.execute(
        "SELECT COUNT(*) c FROM complaints WHERE resident_id=? AND status NOT IN ('Resolved','Closed','Rejected')",
        (user["id"],)
    ).fetchone()["c"]
    notes = conn.execute(
        "SELECT * FROM notifications WHERE user_id=? ORDER BY created_at DESC LIMIT 5", (user["id"],)
    ).fetchall()
    announcements = conn.execute(
        "SELECT * FROM announcements ORDER BY created_at DESC LIMIT 3"
    ).fetchall()
    conn.close()
    return render_template(
        "resident/dashboard.html", requests=requests_, complaints=complaints_,
        active_requests=active_requests, pending_complaints=pending_complaints, notes=notes,
        announcements=announcements,
    )


@app.route("/requests")
@login_required("resident")
def my_requests(user):
    conn = dbh.get_db()
    status = request.args.get("status", "")
    q = "SELECT * FROM requests WHERE resident_id=?"
    params = [user["id"]]
    if status:
        q += " AND status=?"
        params.append(status)
    q += " ORDER BY created_at DESC"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return render_template("resident/request_list.html", rows=rows, statuses=REQUEST_STATUSES, active_status=status)


@app.route("/requests/new", methods=["GET", "POST"])
@login_required("resident")
def new_request(user):
    if request.method == "POST":
        rtype = request.form.get("request_type")
        purpose_raw = request.form.get("purpose", "").strip()
        details = request.form.get("details", "").strip()
        contact_number = request.form.get("contact_number", "").strip()
        request_address = request.form.get("request_address", "").strip()
        civil_status = request.form.get("civil_status", "").strip()
        age_raw = request.form.get("age", "").strip()
        claiming_date = request.form.get("claiming_date", "").strip()
        has_representative = request.form.get("has_representative") == "on"
        representative_name = request.form.get("representative_name", "").strip()
        representative_id_type = request.form.get("representative_id_type", "").strip()
        registered_voter = request.form.get("registered_voter") == "on"

        errors = []
        if rtype not in REQUEST_TYPES:
            errors.append("Please choose a valid request type.")

        # Purpose + fee: Indigency and Barangay Clearance each use their own
        # purpose dropdown with an associated fee.
        computed_fee = None
        purpose = purpose_raw
        if rtype == "Certificate of Indigency":
            if purpose_raw not in INDIGENCY_PURPOSES:
                errors.append("Please choose a valid purpose for your Certificate of Indigency.")
            computed_fee = 0
        elif rtype == "Certificate of Residency":
            if not purpose_raw:
                errors.append("Please state the purpose of your Certificate of Residency.")
            elif len(purpose_raw) > 120:
                errors.append("Purpose is too long (max 120 characters).")
            computed_fee = 0 if registered_voter else RESIDENCY_FEE
        elif rtype == "Barangay Clearance":
            if purpose_raw not in BARANGAY_CLEARANCE_FEE_MAP:
                errors.append("Please choose a valid purpose for your Barangay Clearance.")
            else:
                computed_fee = BARANGAY_CLEARANCE_FEE_MAP[purpose_raw]

        # Number of copies only applies to Certificate of Indigency; lock to 1 otherwise.
        if rtype == "Certificate of Indigency":
            try:
                num_copies = int(request.form.get("num_copies", "1"))
            except ValueError:
                num_copies = 0
            if num_copies < 1 or num_copies > 20:
                errors.append("Number of copies must be between 1 and 20.")
        else:
            num_copies = 1

        try:
            age = int(age_raw)
            if age < 1 or age > 120:
                errors.append("Please provide a valid age.")
        except ValueError:
            age = None
            errors.append("Please provide a valid age.")
        if civil_status not in CIVIL_STATUS_OPTIONS:
            errors.append("Please select your civil status.")

        if not contact_number:
            errors.append("Please provide a contact number.")
        if not request_address:
            errors.append("Please provide your house no./street/purok.")
        if not request.files.get("valid_id") or request.files.get("valid_id").filename == "":
            errors.append("Please upload a valid ID.")
        if rtype == "Certificate of Indigency" and purpose_raw in INDIGENCY_REQUIREMENTS:
            expected_reqs = INDIGENCY_REQUIREMENTS[purpose_raw]
            requirement_files = request.files.getlist("requirement_upload")
            if len(requirement_files) < len(expected_reqs) or any(
                f.filename == "" for f in requirement_files[:len(expected_reqs)]
            ):
                errors.append("Please upload all the required documents listed for your selected purpose.")
        if has_representative and not representative_name:
            errors.append("Please provide the representative's full name.")
        if has_representative and representative_id_type not in VALID_ID_TYPES:
            errors.append("Please select the representative's valid ID type.")
        if has_representative and (not request.files.get("authorization_letter") or request.files.get("authorization_letter").filename == ""):
            errors.append("An authorization letter is required when someone else will claim this on your behalf.")
        if has_representative and (not request.files.get("representative_valid_id") or request.files.get("representative_valid_id").filename == ""):
            errors.append("Please upload a photo of the representative's valid ID.")

        if errors:
            for e in errors:
                flash(e, "error")
            return render_template(
                "resident/request_form.html", types=REQUEST_TYPES, form=request.form,
                civil_status_options=CIVIL_STATUS_OPTIONS, id_types=VALID_ID_TYPES,
                indigency_purposes=INDIGENCY_PURPOSES, indigency_requirements=INDIGENCY_REQUIREMENTS,
                clearance_purposes=BARANGAY_CLEARANCE_PURPOSES, residency_fee=RESIDENCY_FEE,
            )

        conn = dbh.get_db()
        conn.execute("BEGIN IMMEDIATE")
        duplicate_request = resident_duplicate_request(conn, user["id"], rtype, purpose)
        if duplicate_request:
            conn.rollback()
            conn.close()
            flash(
                "You already have an active request for this document with the same purpose. "
                "Please wait until your current request is completed before submitting another one.",
                "duplicate_request_block",
            )
            return redirect(url_for("new_request"))

        ref = dbh.gen_reference("REQ")
        cur = conn.execute(
            "INSERT INTO requests (reference_no, resident_id, request_type, purpose, details, contact_number, "
            "request_address, civil_status, age, computed_fee, num_copies, claiming_date, has_representative, "
            "representative_name, representative_id_type, registered_voter) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ref, user["id"], rtype, purpose, details, contact_number, request_address, civil_status, age,
             computed_fee, num_copies, claiming_date or None, int(has_representative),
             representative_name or None, representative_id_type or None,
             int(registered_voter and rtype == "Certificate of Residency")),
        )
        req_id = cur.lastrowid
        try:
            saved = save_upload(request.files.get("valid_id"), "requests")
            orig, stored = saved
            conn.execute(
                "INSERT INTO request_files (request_id, file_type, filename, stored_name) VALUES (?,?,?,?)",
                (req_id, "valid_id", orig, stored),
            )
            if rtype == "Certificate of Indigency" and purpose in INDIGENCY_REQUIREMENTS:
                expected_reqs = INDIGENCY_REQUIREMENTS[purpose]
                requirement_files = request.files.getlist("requirement_upload")
                for label, f in zip(expected_reqs, requirement_files):
                    saved = save_upload(f, "requests")
                    if saved:
                        orig, stored = saved
                        conn.execute(
                            "INSERT INTO request_files (request_id, file_type, label, filename, stored_name) VALUES (?,?,?,?,?)",
                            (req_id, "supporting_document", label, orig, stored),
                        )
            if has_representative:
                saved = save_upload(request.files.get("authorization_letter"), "requests")
                orig, stored = saved
                conn.execute(
                    "INSERT INTO request_files (request_id, file_type, filename, stored_name) VALUES (?,?,?,?)",
                    (req_id, "authorization_letter", orig, stored),
                )
                saved = save_upload(request.files.get("representative_valid_id"), "requests")
                orig, stored = saved
                conn.execute(
                    "INSERT INTO request_files (request_id, file_type, filename, stored_name) VALUES (?,?,?,?)",
                    (req_id, "representative_id", orig, stored),
                )
            for f in request.files.getlist("attachments")[:3]:
                saved = save_upload(f, "requests")
                if saved:
                    orig, stored = saved
                    conn.execute(
                        "INSERT INTO request_files (request_id, file_type, filename, stored_name) VALUES (?,?,?,?)",
                        (req_id, "supporting_document", orig, stored),
                    )
        except ValueError as e:
            conn.rollback()
            conn.close()
            flash(str(e), "error")
            return render_template(
                "resident/request_form.html", types=REQUEST_TYPES, form=request.form,
                civil_status_options=CIVIL_STATUS_OPTIONS, id_types=VALID_ID_TYPES,
                indigency_purposes=INDIGENCY_PURPOSES, indigency_requirements=INDIGENCY_REQUIREMENTS,
                clearance_purposes=BARANGAY_CLEARANCE_PURPOSES, residency_fee=RESIDENCY_FEE,
            )

        dbh.log_action(conn, user, "Submitted document request", target=ref)
        dbh.log_status(conn, "request", req_id, "Submitted", user["full_name"])
        dbh.notify(conn, user["id"], f"Your request {ref} ({rtype}) has been submitted.", url_for("view_request", request_id=req_id))
        # Let the office that handles this document type know there is something new.
        for staff_row in active_staff_for_offices(conn, offices_for_request_type(rtype)):
            dbh.notify(conn, staff_row["id"], f"New {rtype} request {ref} was submitted.", url_for("view_request", request_id=req_id))
        conn.commit()
        conn.close()
        flash(f"Request submitted. Reference number: {ref}", "success")
        return redirect(url_for("my_requests"))

    default_form = {"contact_number": user["contact_number"] or "", "request_address": user["address"] or "", "num_copies": "1"}
    return render_template(
        "resident/request_form.html", types=REQUEST_TYPES, form=default_form,
        civil_status_options=CIVIL_STATUS_OPTIONS, id_types=VALID_ID_TYPES,
        indigency_purposes=INDIGENCY_PURPOSES, indigency_requirements=INDIGENCY_REQUIREMENTS,
        clearance_purposes=BARANGAY_CLEARANCE_PURPOSES, residency_fee=RESIDENCY_FEE,
    )


@app.route("/requests/<int:request_id>")
@login_required("resident", "staff", "admin")
def view_request(user, request_id):
    conn = dbh.get_db()
    row = conn.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
    if not row or (user["role"] == "resident" and row["resident_id"] != user["id"]):
        conn.close()
        abort(404)
    if user["role"] == "staff" and not can_handle_request_type(user, row["request_type"]):
        conn.close()
        abort(403)
    resident = conn.execute("SELECT * FROM users WHERE id=?", (row["resident_id"],)).fetchone()
    files = conn.execute("SELECT * FROM request_files WHERE request_id=?", (request_id,)).fetchall()
    staff_list = active_staff_for_offices(conn, offices_for_request_type(row["request_type"]))
    history = conn.execute(
        "SELECT * FROM status_history WHERE entity_type='request' AND entity_id=? ORDER BY created_at ASC",
        (request_id,),
    ).fetchall()
    barangay_address = dbh.get_setting(conn, "barangay_address", "")
    conn.close()
    return render_template(
        "request_detail.html", r=row, resident=resident, files=files,
        statuses=REQUEST_STATUSES, staff_list=staff_list, history=history,
        fee=row["computed_fee"],
        claim_location=CLAIM_LOCATIONS.get(row["request_type"], DEFAULT_CLAIM_LOCATION),
        payment_location=PAYMENT_LOCATIONS.get(row["request_type"]),
        barangay_address=barangay_address,
    )


@app.route("/complaints")
@login_required("resident")
def my_complaints(user):
    conn = dbh.get_db()
    status = request.args.get("status", "")
    q = "SELECT * FROM complaints WHERE resident_id=?"
    params = [user["id"]]
    if status:
        q += " AND status=?"
        params.append(status)
    q += " ORDER BY CASE priority WHEN 'High' THEN 0 ELSE 1 END, created_at DESC"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return render_template("resident/complaint_list.html", rows=rows, statuses=COMPLAINT_STATUSES, active_status=status)


@app.route("/community-reports")
@login_required("resident")
def community_reports(user):
    conn = dbh.get_db()
    issues = community_issue_rows(conn, user["id"])
    conn.close()
    return render_template("resident/community_reports.html", issues=issues)


@app.route("/community-reports/<int:issue_id>")
@login_required("resident")
def community_issue_detail(user, issue_id):
    conn = dbh.get_db()
    issue = conn.execute(
        "SELECT ci.*, COUNT(c.id) complaint_count, COUNT(DISTINCT c.resident_id) distinct_residents "
        "FROM community_issues ci JOIN complaints c ON c.community_issue_id=ci.id "
        "WHERE ci.id=? GROUP BY ci.id",
        (issue_id,),
    ).fetchone()
    if not issue:
        conn.close()
        abort(404)
    following = conn.execute(
        "SELECT 1 FROM community_issue_follows WHERE user_id=? AND community_issue_id=?",
        (user["id"], issue_id),
    ).fetchone() is not None
    is_reporter = conn.execute(
        "SELECT 1 FROM complaints WHERE resident_id=? AND community_issue_id=? LIMIT 1",
        (user["id"], issue_id),
    ).fetchone() is not None
    if issue["distinct_residents"] < 2 and not following and not is_reporter:
        conn.close()
        abort(404)
    updates = conn.execute(
        "SELECT status, created_at FROM community_issue_updates "
        "WHERE community_issue_id=? ORDER BY created_at ASC, id ASC",
        (issue_id,),
    ).fetchall()
    conn.close()
    return render_template(
        "resident/community_issue_detail.html", issue=issue, updates=updates, following=following
    )


@app.route("/community-reports/<int:issue_id>/follow", methods=["POST"])
@login_required("resident")
def follow_community_issue(user, issue_id):
    conn = dbh.get_db()
    issue = conn.execute(
        "SELECT id FROM community_issues WHERE id=? "
        "AND EXISTS (SELECT 1 FROM complaints WHERE community_issue_id=? GROUP BY community_issue_id "
        "HAVING COUNT(DISTINCT resident_id)>=2)",
        (issue_id, issue_id),
    ).fetchone()
    if not issue:
        conn.close()
        abort(404)
    conn.execute(
        "INSERT OR IGNORE INTO community_issue_follows (user_id, community_issue_id) VALUES (?,?)",
        (user["id"], issue_id),
    )
    conn.commit()
    conn.close()
    flash("You are now following this community report.", "success")
    return redirect(url_for("community_issue_detail", issue_id=issue_id))


@app.route("/community-reports/<int:issue_id>/unfollow", methods=["POST"])
@login_required("resident")
def unfollow_community_issue(user, issue_id):
    conn = dbh.get_db()
    conn.execute(
        "DELETE FROM community_issue_follows WHERE user_id=? AND community_issue_id=?",
        (user["id"], issue_id),
    )
    conn.commit()
    conn.close()
    return redirect(url_for("community_issue_detail", issue_id=issue_id))


def require_community_issue_access(user):
    if user["role"] == "staff" and not can_handle_complaints(user):
        abort(403)


def management_issue_url(user, issue_id=None):
    endpoint = "community_issue_management_detail" if issue_id is not None else "community_issue_management"
    return url_for(endpoint, issue_id=issue_id) if issue_id is not None else url_for(endpoint)


@app.route("/management/community-issues")
@login_required("staff", "admin")
def community_issue_management(user):
    require_community_issue_access(user)
    status_filter = request.args.get("status", "")
    verification_filter = request.args.get("verification", "all")
    if verification_filter not in {"all", "pending", "verified"}:
        verification_filter = "all"
    search = request.args.get("q", "").strip()[:100]
    conn = dbh.get_db()
    stats, issues = community_issue_manager_data(
        conn, status_filter, verification_filter, search
    )
    conn.close()
    return render_template(
        "management/community_issues.html", stats=stats, issues=issues,
        statuses=COMPLAINT_STATUSES, active_status=status_filter,
        verification_filter=verification_filter, search=search,
    )


@app.route("/management/community-issues/<int:issue_id>")
@login_required("staff", "admin")
def community_issue_management_detail(user, issue_id):
    require_community_issue_access(user)
    conn = dbh.get_db()
    issue = conn.execute(
        "SELECT ci.*, COUNT(c.id) report_count, COUNT(DISTINCT c.resident_id) resident_count, "
        "SUM(CASE WHEN c.is_verified=0 THEN 1 ELSE 0 END) pending_verification "
        "FROM community_issues ci JOIN complaints c ON c.community_issue_id=ci.id "
        "WHERE ci.id=? GROUP BY ci.id",
        (issue_id,),
    ).fetchone()
    if not issue:
        conn.close()
        abort(404)
    reports = conn.execute(
        "SELECT c.id, c.reference_no, c.description, c.priority, c.status, c.location, "
        "c.created_at, c.is_verified, c.verified_at, u.full_name resident_name "
        "FROM complaints c JOIN users u ON u.id=c.resident_id "
        "WHERE c.community_issue_id=? ORDER BY c.created_at ASC, c.id ASC",
        (issue_id,),
    ).fetchall()
    updates = conn.execute(
        "SELECT status, changed_by, message, created_at FROM community_issue_updates "
        "WHERE community_issue_id=? ORDER BY created_at DESC, id DESC",
        (issue_id,),
    ).fetchall()
    conn.close()
    return render_template(
        "management/community_issue_detail.html", issue=issue, reports=reports,
        updates=updates, statuses=COMPLAINT_STATUSES,
        management_url=management_issue_url(user),
    )


@app.route("/management/community-issues/<int:issue_id>/status", methods=["POST"])
@login_required("staff", "admin")
def update_community_issue_status(user, issue_id):
    require_community_issue_access(user)
    new_status = request.form.get("status", "")
    resident_message = request.form.get("resident_message", "").strip()
    if new_status not in COMPLAINT_STATUSES:
        flash("Choose a valid community issue status.", "error")
        return redirect(management_issue_url(user, issue_id))
    if len(resident_message) > 500:
        flash("Resident update must be 500 characters or fewer.", "error")
        return redirect(management_issue_url(user, issue_id))
    conn = dbh.get_db()
    issue = conn.execute("SELECT * FROM community_issues WHERE id=?", (issue_id,)).fetchone()
    if not issue:
        conn.close()
        abort(404)
    if issue["status"] == new_status and not resident_message:
        conn.close()
        flash("No changes were made. Add a resident message or choose a different status.", "info")
        return redirect(management_issue_url(user, issue_id))
    changed_reports = transition_community_issue(
        conn, issue_id, new_status, user["full_name"], resident_message=resident_message or None
    )
    dbh.log_action(
        conn, user, f"Updated community issue status to {new_status}",
        target=f"Community issue #{issue_id}",
        details=resident_message or f"{len(changed_reports)} linked report(s) synchronized.",
    )
    conn.commit()
    conn.close()
    flash("Community issue and linked reports updated. Residents were notified.", "success")
    return redirect(management_issue_url(user, issue_id))


@app.route("/management/community-issues/<int:issue_id>/verify", methods=["POST"])
@login_required("staff", "admin")
def verify_community_issue_reports(user, issue_id):
    require_community_issue_access(user)
    report_id = request.form.get("report_id", "").strip()
    conn = dbh.get_db()
    issue = conn.execute("SELECT id FROM community_issues WHERE id=?", (issue_id,)).fetchone()
    if not issue:
        conn.close()
        abort(404)
    if report_id:
        try:
            report_id = int(report_id)
        except ValueError:
            conn.close()
            abort(400)
        reports = conn.execute(
            "SELECT id, reference_no FROM complaints "
            "WHERE id=? AND community_issue_id=? AND is_verified=0",
            (report_id, issue_id),
        ).fetchall()
    else:
        reports = conn.execute(
            "SELECT id, reference_no FROM complaints WHERE community_issue_id=? AND is_verified=0",
            (issue_id,),
        ).fetchall()
    conn.executemany(
        "UPDATE complaints SET is_verified=1, verified_by=?, verified_at=datetime('now') WHERE id=?",
        [(user["id"], report["id"]) for report in reports],
    )
    for report in reports:
        dbh.log_action(
            conn, user, "Verified community concern report", target=report["reference_no"],
            details=f"Community issue #{issue_id}",
        )
    conn.commit()
    conn.close()
    flash(f"{len(reports)} report(s) verified.", "success")
    return redirect(management_issue_url(user, issue_id))


@app.route("/complaints/new", methods=["GET", "POST"])
@login_required("resident")
def new_complaint(user):
    conn = dbh.get_db()
    if request.method == "POST":
        conn.execute("BEGIN IMMEDIATE")
    allowance, reset_seconds = concern_allowance(conn, user["id"])
    daily_remaining = allowance["remaining"]
    daily_limit_reached = daily_remaining == 0
    if request.method == "GET":
        conn.commit()
        conn.close()
        return render_template(
            "resident/complaint_form.html",
            categories=COMPLAINT_CATEGORIES,
            form={},
            daily_remaining=daily_remaining,
            daily_limit=COMPLAINT_DAILY_LIMIT,
            daily_limit_reached=daily_limit_reached,
            reset_seconds=reset_seconds,
        )

    if daily_limit_reached:
        conn.rollback()
        conn.close()
        return render_template(
            "resident/complaint_form.html",
            categories=COMPLAINT_CATEGORIES,
            form={},
            daily_remaining=0,
            daily_limit=COMPLAINT_DAILY_LIMIT,
            daily_limit_reached=True,
            reset_seconds=reset_seconds,
        )

    category = request.form.get("category")
    description = request.form.get("description", "").strip()
    location = request.form.get("location", "").strip()
    if category not in COMPLAINT_CATEGORIES:
        conn.close()
        flash("Please choose a valid category.", "error")
        return render_template("resident/complaint_form.html", categories=COMPLAINT_CATEGORIES, form=request.form,
                               daily_remaining=daily_remaining, daily_limit=COMPLAINT_DAILY_LIMIT,
                               daily_limit_reached=False, reset_seconds=reset_seconds)
    if not description:
        conn.close()
        flash("Please describe your complaint.", "error")
        return render_template("resident/complaint_form.html", categories=COMPLAINT_CATEGORIES, form=request.form,
                               daily_remaining=daily_remaining, daily_limit=COMPLAINT_DAILY_LIMIT,
                               daily_limit_reached=False, reset_seconds=reset_seconds)
    if not location:
        conn.close()
        flash("Please specify the location where this occurred.", "error")
        return render_template("resident/complaint_form.html", categories=COMPLAINT_CATEGORIES, form=request.form,
                               daily_remaining=daily_remaining, daily_limit=COMPLAINT_DAILY_LIMIT,
                               daily_limit_reached=False, reset_seconds=reset_seconds)

    duplicate_complaint = resident_duplicate_complaint(conn, user["id"], category, location)
    if duplicate_complaint:
        conn.rollback()
        conn.close()
        flash("You already have an active concern for this category and location.", "duplicate_concern_block")
        return redirect(url_for("new_complaint"))

    detected_priority, priority_reason = prioritymod.classify(category, description, location)
    normalized_location = dbh.normalize_community_issue_location(location)
    issue = conn.execute(
        "SELECT * FROM community_issues WHERE category=? AND normalized_location=? "
        "AND status NOT IN ('Resolved','Closed','Rejected') ORDER BY id DESC LIMIT 1",
        (category, normalized_location),
    ).fetchone()
    if issue is None:
        cursor = conn.execute(
            "INSERT INTO community_issues "
            "(category, location, normalized_location, status) VALUES (?,?,?,'Pending')",
            (category, location, normalized_location),
        )
        issue_id = cursor.lastrowid
        issue_status = "Pending"
        conn.execute(
            "INSERT INTO community_issue_updates (community_issue_id, status) VALUES (?,?)",
            (issue_id, issue_status),
        )
    else:
        issue_id = issue["id"]
        issue_status = issue["status"]

    ref = dbh.gen_reference("CMP")
    cur = conn.execute(
        "INSERT INTO complaints (reference_no, resident_id, category, description, location, priority, "
        "priority_reason, status, community_issue_id) VALUES (?,?,?,?,?,?,?,?,?)",
        (ref, user["id"], category, description, location, detected_priority, priority_reason, issue_status, issue_id),
    )
    c_id = cur.lastrowid
    conn.execute(
        "INSERT OR IGNORE INTO community_issue_follows (user_id, community_issue_id) VALUES (?,?)",
        (user["id"], issue_id),
    )
    try:
        submitted_files = validate_complaint_attachment_files(request.files.getlist("attachments"))
        for f in submitted_files:
            saved = save_upload(f, "complaints", allowed_ext=COMPLAINT_ATTACHMENT_EXT, max_size=MAX_FILE_SIZE)
            if saved:
                orig, stored = saved
                conn.execute(
                    "INSERT INTO complaint_files (complaint_id, filename, stored_name) VALUES (?,?,?)",
                    (c_id, orig, stored),
                )
    except ValueError as e:
        conn.rollback()
        conn.close()
        flash(str(e), "error")
        return render_template("resident/complaint_form.html", categories=COMPLAINT_CATEGORIES, form=request.form,
                               daily_remaining=daily_remaining, daily_limit=COMPLAINT_DAILY_LIMIT,
                               daily_limit_reached=False, reset_seconds=reset_seconds)

    dbh.log_action(conn, user, "Filed complaint", target=ref, details=f"Auto-priority: {detected_priority}")
    dbh.log_status(conn, "complaint", c_id, issue_status, user["full_name"])
    dbh.notify(conn, user["id"], f"Your complaint {ref} ({category}) has been filed.", url_for("view_complaint", complaint_id=c_id))
    if detected_priority == "High":
        for staff_row in conn.execute("SELECT id FROM users WHERE status='active' AND (role='admin' OR (role='staff' AND office='bpso'))").fetchall():
            dbh.notify(conn, staff_row["id"], f"High-priority complaint {ref} ({category}) needs attention.", url_for("view_complaint", complaint_id=c_id))
    remaining_after_submit = daily_remaining - 1
    submitted_at = datetime.now()
    cooldown_started = submitted_at.isoformat(timespec="seconds") if remaining_after_submit == 0 else None
    cooldown_expires = (
        (submitted_at + timedelta(hours=24)).isoformat(timespec="seconds")
        if remaining_after_submit == 0 else None
    )
    conn.execute(
        "UPDATE resident_concern_allowances SET remaining=?, total_submitted=total_submitted+1, "
        "cooldown_started_at=?, cooldown_expires_at=? WHERE user_id=?",
        (remaining_after_submit, cooldown_started, cooldown_expires, user["id"]),
    )
    conn.execute(
        "INSERT INTO concern_allowance_events "
        "(user_id, complaint_id, event_type, slots, created_at) VALUES (?,?,'submitted',-1,?)",
        (user["id"], c_id, submitted_at.isoformat(timespec="seconds")),
    )
    issue_resident_count = conn.execute(
        "SELECT COUNT(DISTINCT resident_id) count FROM complaints WHERE community_issue_id=?",
        (issue_id,),
    ).fetchone()["count"]
    conn.commit()
    conn.close()
    flash(f"{ref}|{remaining_after_submit}|{issue_resident_count}|{issue_status}|{issue_id}", "concern_success")
    return redirect(url_for("resident_dashboard"))


@app.route("/complaints/<int:complaint_id>")
@login_required("resident", "staff", "admin")
def view_complaint(user, complaint_id):
    conn = dbh.get_db()
    row = conn.execute("SELECT * FROM complaints WHERE id=?", (complaint_id,)).fetchone()
    if not row or (user["role"] == "resident" and row["resident_id"] != user["id"]):
        conn.close()
        abort(404)
    if user["role"] == "staff" and not can_handle_complaints(user):
        conn.close()
        abort(403)
    resident = conn.execute("SELECT * FROM users WHERE id=?", (row["resident_id"],)).fetchone()
    files = conn.execute("SELECT * FROM complaint_files WHERE complaint_id=?", (complaint_id,)).fetchall()
    evidence = []
    for f in files:
        path = os.path.join(UPLOAD_DIR, "complaints", f["stored_name"])
        size = os.path.getsize(path) if os.path.isfile(path) else 0
        ext = (f["filename"] or "").rsplit(".", 1)[-1].lower() if "." in (f["filename"] or "") else ""
        evidence.append({
            **dict(f),
            "extension": ext,
            "size_bytes": size,
            "size_label": format_file_size(size),
            "is_video": ext in {"mp4", "mov", "webm", "avi", "m4v"},
            "is_image": ext in {"jpg", "jpeg", "png", "webp"},
            "is_pdf": ext == "pdf",
        })
    staff_list = active_staff_for_offices(conn, offices_for_complaints())
    history = conn.execute(
        "SELECT * FROM status_history WHERE entity_type='complaint' AND entity_id=? ORDER BY created_at ASC",
        (complaint_id,),
    ).fetchall()
    conn.close()
    return render_template(
        "complaint_detail.html", c=row, resident=resident, files=files, evidence=evidence,
        statuses=COMPLAINT_STATUSES, staff_list=staff_list, priorities=PRIORITY_LEVELS, history=history,
    )


@app.route("/notifications")
@login_required("resident", "staff", "admin")
def notifications(user):
    conn = dbh.get_db()
    rows = conn.execute("SELECT * FROM notifications WHERE user_id=? ORDER BY created_at DESC", (user["id"],)).fetchall()
    conn.close()
    return render_template("notifications.html", rows=rows)


@app.route("/notifications/mark-read", methods=["POST"])
@login_required("resident", "staff", "admin")
def mark_notifications_read(user):
    conn = dbh.get_db()
    conn.execute("UPDATE notifications SET is_read=1 WHERE user_id=?", (user["id"],))
    conn.commit()
    conn.close()
    return redirect(request.referrer or url_for("notifications"))


@app.route("/profile", methods=["GET", "POST"])
@login_required("resident", "staff", "admin")
def profile(user):
    conn = dbh.get_db()
    if request.method == "POST":
        photo = request.files.get("profile_picture")
        if not photo or photo.filename == "":
            flash("Please choose an image to upload.", "error")
            conn.close()
            return redirect(url_for("profile"))
        try:
            saved = save_upload(
                photo, "profiles", allowed_ext=PROFILE_IMAGE_EXT,
                max_size=PROFILE_IMAGE_MAX_SIZE, strict_type=True,
            )
        except ValueError as e:
            flash(str(e), "error")
            conn.close()
            return redirect(url_for("profile"))
        _, stored_name = saved
        conn.execute("UPDATE users SET profile_picture=? WHERE id=?", (stored_name, user["id"]))
        dbh.log_action(conn, user, "Updated profile picture")
        conn.commit()
        conn.close()
        flash("Profile picture updated.", "success")
        return redirect(url_for("profile"))

    conn.close()
    return render_template(
        "profile.html",
        profile_image_max_size=PROFILE_IMAGE_MAX_SIZE,
        profile_image_max_size_mb=PROFILE_IMAGE_MAX_SIZE // (1024 * 1024),
    )


@app.route("/profile/change-password", methods=["POST"])
@login_required("resident", "staff", "admin")
def change_password(user):
    current_pw = request.form.get("current_password", "")
    new_pw = request.form.get("new_password", "")
    confirm_pw = request.form.get("confirm_password", "")

    if not check_password_hash(user["password_hash"], current_pw):
        flash("Current password is incorrect.", "error")
        return redirect(url_for("profile"))
    if not valid_password(new_pw):
        flash("New password must be at least 8 characters and include a letter and a number.", "error")
        return redirect(url_for("profile"))
    if new_pw != confirm_pw:
        flash("New passwords do not match.", "error")
        return redirect(url_for("profile"))

    conn = dbh.get_db()
    conn.execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(new_pw), user["id"]))
    dbh.log_action(conn, user, "Changed own password")
    conn.commit()
    conn.close()
    flash("Password updated.", "success")
    return redirect(url_for("profile"))


@app.route("/profile/address", methods=["POST"])
@login_required("resident")
def update_address(user):
    address = request.form.get("address", "").strip()
    if not address:
        flash("Address is required.", "error")
        return redirect(url_for("profile"))

    conn = dbh.get_db()
    conn.execute("UPDATE users SET address=? WHERE id=?", (address, user["id"]))
    dbh.log_action(conn, user, "Updated address")
    conn.commit()
    conn.close()
    flash("Address updated.", "success")
    return redirect(url_for("profile"))


# ------------------------------------------------------------------ staff --

@app.route("/staff/dashboard")
@login_required("staff")
def staff_dashboard(user):
    conn = dbh.get_db()
    types = handled_request_types(user)
    can_requests = bool(types)
    can_complaints = can_handle_complaints(user)
    todays_requests = pending_approvals = urgent_complaints = total_complaints = 0
    complaint_stats, queue, my_complaint_queue = [], [], []
    if can_requests:
        clause, tparams = _in_clause(types)
        today = datetime.now().strftime("%Y-%m-%d")
        todays_requests = conn.execute(
            f"SELECT COUNT(*) c FROM requests WHERE request_type {clause} AND date(created_at)=?", tparams + [today]
        ).fetchone()["c"]
        pending_approvals = conn.execute(
            f"SELECT COUNT(*) c FROM requests WHERE request_type {clause} "
            "AND status IN ('Submitted','Under Review','Processing')", tparams
        ).fetchone()["c"]
        queue = conn.execute(
            f"SELECT * FROM requests WHERE request_type {clause} "
            "AND status IN ('Submitted','Under Review','Processing') ORDER BY created_at ASC LIMIT 8", tparams
        ).fetchall()
    related_clusters = []
    if can_complaints:
        urgent_complaints = conn.execute(
            "SELECT COUNT(*) c FROM complaints WHERE priority='High' AND status NOT IN ('Resolved','Closed','Rejected')"
        ).fetchone()["c"]
        status_counts = dict(conn.execute("SELECT status, COUNT(*) c FROM complaints GROUP BY status").fetchall())
        total_complaints = sum(status_counts.values())
        complaint_stats = [
            {
                "status": st,
                "count": status_counts.get(st, 0),
                "pct": round(status_counts.get(st, 0) / total_complaints * 100) if total_complaints else 0,
                "color": COMPLAINT_STATUS_COLORS[st],
            }
            for st in COMPLAINT_STATUSES
        ]
        my_complaint_queue = conn.execute(
            "SELECT * FROM complaints WHERE status NOT IN ('Resolved','Closed','Rejected') "
            "ORDER BY CASE priority WHEN 'High' THEN 0 ELSE 1 END, created_at ASC LIMIT 8"
        ).fetchall()
        related_clusters = related_complaint_clusters(conn, user["id"])
    conn.close()
    office_cfg = OFFICES.get(user["office"])
    return render_template(
        "staff/dashboard.html", todays_requests=todays_requests, pending_approvals=pending_approvals,
        urgent_complaints=urgent_complaints, complaint_stats=complaint_stats, total_complaints=total_complaints,
        queue=queue, my_complaint_queue=my_complaint_queue,
        can_requests=can_requests, can_complaints=can_complaints,
        office_label=office_cfg["label"] if office_cfg else None,
        handled_types=types, related_clusters=related_clusters,
    )


@app.route("/staff/requests")
@login_required("staff")
def staff_requests(user):
    types = handled_request_types(user)
    if not types:
        abort(403)
    conn = dbh.get_db()
    status = request.args.get("status", "")
    clause, params = _in_clause(types)
    q = f"SELECT r.*, u.full_name resident_name FROM requests r JOIN users u ON u.id=r.resident_id WHERE r.request_type {clause}"
    if status:
        q += " AND r.status=?"
        params.append(status)
    q += " ORDER BY r.created_at DESC"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return render_template("staff/request_queue.html", rows=rows, statuses=REQUEST_STATUSES,
                           active_status=status, handled_types=types)


@app.route("/staff/complaints")
@login_required("staff")
def staff_complaints(user):
    if not can_handle_complaints(user):
        abort(403)
    conn = dbh.get_db()
    status = request.args.get("status", "")
    priority_filter = request.args.get("priority", "")
    q = "SELECT c.*, u.full_name resident_name, u.address resident_address, u.contact_number resident_contact FROM complaints c JOIN users u ON u.id=c.resident_id"
    clauses, params = [], []
    if status:
        clauses.append("c.status=?")
        params.append(status)
    if priority_filter:
        clauses.append("c.priority=?")
        params.append(priority_filter)
    if clauses:
        q += " WHERE " + " AND ".join(clauses)
    q += " ORDER BY CASE c.priority WHEN 'High' THEN 0 ELSE 1 END, c.created_at DESC"
    rows = conn.execute(q, params).fetchall()
    related = related_complaint_clusters(conn, user["id"])
    conn.close()
    return render_template("staff/complaint_queue.html", rows=rows, statuses=COMPLAINT_STATUSES,
                            active_status=status, priorities=PRIORITY_LEVELS, active_priority=priority_filter,
                            related_clusters=related)


@app.route("/requests/<int:request_id>/update", methods=["POST"])
@login_required("staff", "admin")
def update_request_status(user, request_id):
    conn = dbh.get_db()
    row = conn.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
    if not row:
        conn.close()
        abort(404)
    if user["role"] == "staff" and not can_handle_request_type(user, row["request_type"]):
        conn.close()
        abort(403)
    new_status = request.form.get("status")
    remarks = request.form.get("remarks", "").strip()
    assigned = request.form.get("assigned_staff_id") or None
    if new_status not in REQUEST_STATUSES:
        conn.close()
        flash("Invalid status.", "error")
        return redirect(url_for("view_request", request_id=request_id))
    if assigned and not valid_assignee(conn, assigned, offices_for_request_type(row["request_type"])):
        conn.close()
        flash("That staff member does not handle this type of request.", "error")
        return redirect(url_for("view_request", request_id=request_id))

    conn.execute(
        "UPDATE requests SET status=?, remarks=?, assigned_staff_id=?, updated_at=datetime('now') WHERE id=?",
        (new_status, remarks, assigned, request_id),
    )
    dbh.log_action(conn, user, f"Updated request status to {new_status}", target=row["reference_no"], details=remarks)
    if new_status != row["status"]:
        dbh.log_status(conn, "request", request_id, new_status, user["full_name"], remarks or None)
    dbh.notify(
        conn, row["resident_id"],
        f"Your request {row['reference_no']} status changed to {new_status}.",
        url_for("view_request", request_id=request_id),
    )
    conn.commit()
    conn.close()
    flash("Request updated.", "success")
    return redirect(url_for("view_request", request_id=request_id))


@app.route("/complaints/<int:complaint_id>/update", methods=["POST"])
@login_required("staff", "admin")
def update_complaint_status(user, complaint_id):
    conn = dbh.get_db()
    row = conn.execute("SELECT * FROM complaints WHERE id=?", (complaint_id,)).fetchone()
    if not row:
        conn.close()
        abort(404)
    if user["role"] == "staff" and not can_handle_complaints(user):
        conn.close()
        abort(403)
    new_status = request.form.get("status")
    remarks = request.form.get("remarks", "").strip()
    assigned = request.form.get("assigned_staff_id") or None
    if new_status not in COMPLAINT_STATUSES:
        conn.close()
        flash("Invalid status.", "error")
        return redirect(url_for("view_complaint", complaint_id=complaint_id))
    if assigned and not valid_assignee(conn, assigned, offices_for_complaints()):
        conn.close()
        flash("That staff member does not handle complaints.", "error")
        return redirect(url_for("view_complaint", complaint_id=complaint_id))

    conn.execute(
        "UPDATE complaints SET remarks=?, assigned_staff_id=?, updated_at=datetime('now') WHERE id=?",
        (remarks, assigned, complaint_id),
    )
    dbh.log_action(conn, user, f"Updated complaint status to {new_status}", target=row["reference_no"], details=remarks)
    if row["community_issue_id"]:
        transition_community_issue(conn, row["community_issue_id"], new_status, user["full_name"], remarks or None)
    else:
        conn.execute(
            "UPDATE complaints SET status=?, updated_at=datetime('now') WHERE id=?",
            (new_status, complaint_id),
        )
        if new_status != row["status"]:
            dbh.log_status(conn, "complaint", complaint_id, new_status, user["full_name"], remarks or None)
            if new_status == "Resolved" and row["status"] != "Rejected":
                restore_resolved_concern_slot(conn, row)
        dbh.notify(
            conn, row["resident_id"],
            f"Your complaint {row['reference_no']} status changed to {new_status}.",
            url_for("view_complaint", complaint_id=complaint_id),
        )
    conn.commit()
    conn.close()
    flash("Complaint updated.", "success")
    return redirect(url_for("view_complaint", complaint_id=complaint_id))


BULK_COMPLAINT_ACTIONS = {
    # target status -> current statuses that are skipped (already finished)
    "Resolved": {"Resolved", "Closed", "Rejected"},
    "Closed": {"Closed", "Rejected"},
}
BULK_COMPLAINT_MAX = 200


@app.route("/complaints/bulk", methods=["POST"])
@login_required("staff", "admin")
def bulk_update_complaints(user):
    """Resolve or close many complaints at once. Same side effects as the single
    update (status history, audit log, resident notification) for every complaint,
    all in one transaction."""
    if user["role"] == "staff" and not can_handle_complaints(user):
        abort(403)
    back = "admin_complaints" if user["role"] == "admin" else "staff_complaints"
    back_args = {k: request.form.get(k) for k in ("status", "priority") if request.form.get(k)}

    action = request.form.get("action", "")
    remarks = request.form.get("remarks", "").strip()
    try:
        ids = sorted({int(i) for i in request.form.getlist("complaint_ids")})
    except ValueError:
        abort(400)
    if action not in BULK_COMPLAINT_ACTIONS:
        flash("Choose Resolve or Close.", "error")
        return redirect(url_for(back, **back_args))
    if not ids:
        flash("Select at least one complaint first.", "error")
        return redirect(url_for(back, **back_args))
    if len(ids) > BULK_COMPLAINT_MAX:
        flash(f"Too many selected at once (max {BULK_COMPLAINT_MAX}).", "error")
        return redirect(url_for(back, **back_args))

    conn = dbh.get_db()
    rows = conn.execute(
        f"SELECT * FROM complaints WHERE id IN ({','.join('?' * len(ids))})", ids
    ).fetchall()
    skip_states = BULK_COMPLAINT_ACTIONS[action]
    done = skipped = 0
    processed_issues = set()
    for row in rows:
        if row["community_issue_id"]:
            issue_id = row["community_issue_id"]
            if issue_id in processed_issues:
                continue
            processed_issues.add(issue_id)
            issue_status = conn.execute(
                "SELECT status FROM community_issues WHERE id=?", (issue_id,)
            ).fetchone()
            if not issue_status or issue_status["status"] in skip_states:
                skipped += sum(1 for selected in rows if selected["community_issue_id"] == issue_id)
                continue
            if remarks:
                selected_ids = [
                    selected["id"] for selected in rows if selected["community_issue_id"] == issue_id
                ]
                conn.execute(
                    f"UPDATE complaints SET remarks=? WHERE id IN ({','.join('?' * len(selected_ids))})",
                    [remarks, *selected_ids],
                )
            affected = transition_community_issue(conn, issue_id, action, user["full_name"], remarks or None)
            for report in affected:
                dbh.log_action(
                    conn, user, f"Updated complaint status to {action}", target=report["reference_no"],
                    details=(remarks + " " if remarks else "") + "(bulk action)",
                )
            done += len(affected)
            continue
        if row["status"] in skip_states:
            skipped += 1
            continue
        conn.execute(
            "UPDATE complaints SET status=?, remarks=COALESCE(?, remarks), updated_at=datetime('now') WHERE id=?",
            (action, remarks or None, row["id"]),
        )
        if action == "Resolved":
            restore_resolved_concern_slot(conn, row)
        dbh.log_action(conn, user, f"Updated complaint status to {action}", target=row["reference_no"],
                       details=(remarks + " " if remarks else "") + "(bulk action)")
        dbh.log_status(conn, "complaint", row["id"], action, user["full_name"], remarks or None)
        dbh.notify(conn, row["resident_id"],
                   f"Your complaint {row['reference_no']} status changed to {action}.",
                   url_for("view_complaint", complaint_id=row["id"]))
        done += 1
    skipped += len(ids) - len(rows)          # ids that no longer exist
    conn.commit()
    conn.close()

    word = "resolved" if action == "Resolved" else "closed"
    msg = f"{done} complaint(s) {word}."
    if skipped:
        msg += f" {skipped} skipped (already finished or not found)."
    flash(msg, "success" if done else "error")
    return redirect(url_for(back, **back_args))


def _residents_query(user):
    conn = dbh.get_db()
    is_admin = user["role"] == "admin"
    q = request.args.get("q", "").strip()
    address = request.args.get("address", "").strip()
    purok = request.args.get("purok", "").strip()          # "3", "none", or "" (all)
    status = request.args.get("status", "").strip()        # "active" / "inactive" / "" (all) - admin only
    query = "SELECT * FROM users WHERE role='resident'"
    params = []
    if q:
        query += " AND (full_name LIKE ? OR email LIKE ?)"
        params += [f"%{q}%", f"%{q}%"]
    if address:
        query += " AND COALESCE(address,'') LIKE ?"
        params.append(f"%{address}%")
    if is_admin and status in ("active", "inactive"):
        query += " AND status=?"
        params.append(status)
    else:
        status = ""
    query += " ORDER BY full_name ASC"
    rows = conn.execute(query, params).fetchall()
    puroks = dbh.purok_options(conn)
    conn.close()
    if purok == "none":
        rows = [r for r in rows if dbh.purok_of(r["address"]) is None]
    elif purok in puroks:
        rows = [r for r in rows if dbh.purok_of(r["address"]) == purok]
    else:
        purok = ""
    template = "admin/residents.html" if is_admin else "staff/residents.html"
    return render_template(template, rows=rows, q=q, address=address, purok=purok,
                           puroks=puroks, status=status)


@app.route("/staff/residents")
@login_required("staff", "admin")
def view_residents(user):
    return _residents_query(user)


# ------------------------------------------------------------------ admin --

def _build_trend_chart(conn, weeks=8):
    """Weekly request/complaint counts for the last N weeks, plus ready-made
    SVG polyline coordinates for a simple line chart (no JS charting lib)."""
    now = datetime.now()
    buckets = []
    for i in range(weeks - 1, -1, -1):
        week_start = now - timedelta(weeks=i + 1)
        week_end = now - timedelta(weeks=i)
        req_count = conn.execute(
            "SELECT COUNT(*) c FROM requests WHERE created_at >= ? AND created_at < ?",
            (week_start.strftime("%Y-%m-%d %H:%M:%S"), week_end.strftime("%Y-%m-%d %H:%M:%S")),
        ).fetchone()["c"]
        comp_count = conn.execute(
            "SELECT COUNT(*) c FROM complaints WHERE created_at >= ? AND created_at < ?",
            (week_start.strftime("%Y-%m-%d %H:%M:%S"), week_end.strftime("%Y-%m-%d %H:%M:%S")),
        ).fetchone()["c"]
        buckets.append({"label": week_end.strftime("%b %d"), "requests": req_count, "complaints": comp_count})

    chart_w, chart_h, pad_l, pad_b, pad_t = 700, 200, 30, 26, 14
    plot_w, plot_h = chart_w - pad_l - 10, chart_h - pad_b - pad_t
    max_val = max([b["requests"] for b in buckets] + [b["complaints"] for b in buckets] + [1])
    step_x = plot_w / (len(buckets) - 1) if len(buckets) > 1 else 0

    def points_for(key):
        pts = []
        for idx, b in enumerate(buckets):
            x = pad_l + idx * step_x
            y = pad_t + plot_h - (b[key] / max_val * plot_h)
            pts.append(f"{x:.1f},{y:.1f}")
        return " ".join(pts)

    return {
        "buckets": buckets,
        "request_points": points_for("requests"),
        "complaint_points": points_for("complaints"),
        "chart_w": chart_w, "chart_h": chart_h, "pad_l": pad_l, "pad_b": pad_b, "pad_t": pad_t,
        "plot_w": plot_w, "plot_h": plot_h, "step_x": step_x, "max_val": max_val,
    }


@app.route("/admin/dashboard")
@login_required("admin")
def admin_dashboard(user):
    conn = dbh.get_db()
    total_residents = conn.execute("SELECT COUNT(*) c FROM users WHERE role='resident'").fetchone()["c"]
    active_staff = conn.execute("SELECT COUNT(*) c FROM users WHERE role='staff' AND status='active'").fetchone()["c"]
    uncovered_offices = [
        cfg["label"] for key, cfg in OFFICES.items()
        if not conn.execute(
            "SELECT 1 FROM users WHERE role='staff' AND status='active' AND office=?", (key,)
        ).fetchone()
    ]
    pending_requests = conn.execute(
        "SELECT COUNT(*) c FROM requests WHERE status NOT IN ('Completed','Rejected')"
    ).fetchone()["c"]
    open_complaints = conn.execute(
        "SELECT COUNT(*) c FROM complaints WHERE status NOT IN ('Resolved','Closed','Rejected')"
    ).fetchone()["c"]
    urgent_complaints = conn.execute(
        "SELECT COUNT(*) c FROM complaints WHERE priority='High' AND status NOT IN ('Resolved','Closed','Rejected')"
    ).fetchone()["c"]
    comp_status_counts = dict(conn.execute("SELECT status, COUNT(*) c FROM complaints GROUP BY status").fetchall())
    total_complaints_all = sum(comp_status_counts.values())
    complaint_stats = [
        {
            "status": s,
            "count": comp_status_counts.get(s, 0),
            "pct": round(comp_status_counts.get(s, 0) / total_complaints_all * 100) if total_complaints_all else 0,
            "color": COMPLAINT_STATUS_COLORS[s],
        }
        for s in COMPLAINT_STATUSES
    ]
    req_status_counts = dict(conn.execute("SELECT status, COUNT(*) c FROM requests GROUP BY status").fetchall())
    total_requests_all = sum(req_status_counts.values())
    request_stats = [
        {
            "status": s,
            "count": req_status_counts.get(s, 0),
            "pct": round(req_status_counts.get(s, 0) / total_requests_all * 100) if total_requests_all else 0,
            "color": REQUEST_STATUS_COLORS[s],
        }
        for s in REQUEST_STATUSES
    ]
    recent = conn.execute(
        """
        SELECT * FROM (
          SELECT created_at, ('Request ' || reference_no || ' — ' || request_type) AS activity, status, 'Normal' AS priority
          FROM requests
          UNION ALL
          SELECT created_at, ('Complaint ' || reference_no || ' — ' || category) AS activity, status, priority
          FROM complaints
        ) ORDER BY created_at DESC LIMIT 10
        """
    ).fetchall()
    related_clusters = related_complaint_clusters(conn, user["id"])
    trend = _build_trend_chart(conn)
    conn.close()
    return render_template(
        "admin/dashboard.html", total_residents=total_residents, active_staff=active_staff,
        pending_requests=pending_requests, open_complaints=open_complaints,
        urgent_complaints=urgent_complaints, complaint_stats=complaint_stats,
        total_complaints=total_complaints_all, request_stats=request_stats,
        total_requests=total_requests_all, recent=recent, trend=trend,
        uncovered_offices=uncovered_offices, related_clusters=related_clusters,
    )


@app.route("/admin/residents")
@login_required("admin")
def admin_residents(user):
    return _residents_query(user)


@app.route("/admin/staff", methods=["GET", "POST"])
@login_required("admin")
def admin_staff(user):
    conn = dbh.get_db()
    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        email = request.form.get("email", "").strip().lower()
        office = request.form.get("office", "").strip()
        raw_contact = request.form.get("contact_number", "").strip()
        contact_number = _normalize_ph_mobile(raw_contact) if raw_contact else None

        if not full_name or not valid_email(email):
            flash("Please provide a valid name and email.", "error")
        elif raw_contact and not contact_number:
            flash("Enter a valid Philippine mobile number, such as 0917 123 4567.", "error")
        elif office not in OFFICES:
            flash("Please choose the staff member's office (Treasurer, Secretary or BPSO).", "error")
        elif conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone():
            flash("An account with this email already exists.", "error")
        else:
            temp_pw = dbh.gen_temp_password()
            conn.execute(
                "INSERT INTO users (full_name, email, password_hash, role, office, position, "
                "contact_number, must_reset_pw, created_by) VALUES (?,?,?,?,?,?,?,1,?)",
                (full_name, email, generate_password_hash(temp_pw), "staff", office,
                 OFFICES[office]["label"], contact_number, user["id"]),
            )
            dbh.log_action(conn, user, "Created staff account", target=email, details=f"office={office}")
            conn.commit()
            flash(f"Staff account created for {full_name}. Temporary password: {temp_pw}", "success")
        conn.close()
        return redirect(url_for("admin_staff"))

    rows = conn.execute(
        "SELECT u.*,"
        " (SELECT COUNT(*) FROM requests r WHERE r.assigned_staff_id=u.id "
        "AND r.status NOT IN ('Completed','Rejected')) AS active_request_count,"
        " (SELECT COUNT(*) FROM complaints c WHERE c.assigned_staff_id=u.id "
        "AND c.status NOT IN ('Resolved','Closed','Rejected')) AS active_complaint_count,"
        " (SELECT COUNT(*) FROM requests r WHERE r.assigned_staff_id=u.id "
        "AND r.status NOT IN ('Completed','Rejected')) +"
        " (SELECT COUNT(*) FROM complaints c WHERE c.assigned_staff_id=u.id "
        "AND c.status NOT IN ('Resolved','Closed','Rejected')) AS active_work_count "
        "FROM users u WHERE u.role='staff' ORDER BY u.created_at DESC"
    ).fetchall()
    coverage = {
        key: conn.execute(
            "SELECT COUNT(*) c FROM users WHERE role='staff' AND status='active' AND office=?", (key,)
        ).fetchone()["c"]
        for key in OFFICES
    }
    conn.close()
    return render_template("admin/staff.html", rows=rows, offices=OFFICES, coverage=coverage)


@app.route("/admin/users/<int:account_id>/mobile", methods=["POST"])
@login_required("admin", "staff")
def update_account_mobile(user, account_id):
    conn = dbh.get_db()
    account = conn.execute(
        "SELECT id, full_name, email, role FROM users WHERE id=?", (account_id,)
    ).fetchone()
    if not account:
        conn.close()
        abort(404)
    if user["role"] == "staff" and account["role"] != "resident":
        conn.close()
        abort(403)
    return_endpoint = {
        "resident": "admin_residents",
        "staff": "admin_staff",
        "admin": "admin_administrators",
    }[account["role"]]
    if user["role"] == "staff":
        return_endpoint = "view_residents"
    contact_number = _normalize_ph_mobile(request.form.get("contact_number", "").strip())
    if not contact_number:
        conn.close()
        flash("Enter a valid Philippine mobile number, such as 0917 123 4567.", "error")
        return redirect(url_for(return_endpoint))
    conn.execute(
        "UPDATE users SET contact_number=? WHERE id=?", (contact_number, account_id)
    )
    conn.execute(
        "UPDATE admin_otp_challenges SET email_otp_hash=NULL, used_at=? WHERE user_id=?",
        (datetime.now().isoformat(timespec="seconds"), account_id),
    )
    dbh.log_action(
        conn, user, "Updated account mobile number", target=account["email"],
        details=f"role={account['role']}",
    )
    conn.commit()
    conn.close()
    flash(f"Mobile number updated for {account['full_name']}.", "success")
    return redirect(url_for(return_endpoint))


@app.route("/admin/staff/<int:staff_id>/office", methods=["POST"])
@login_required("admin")
def set_staff_office(user, staff_id):
    office = request.form.get("office", "").strip()
    conn = dbh.get_db()
    row = conn.execute("SELECT * FROM users WHERE id=? AND role='staff'", (staff_id,)).fetchone()
    if not row:
        conn.close()
        abort(404)
    if office not in OFFICES:
        conn.close()
        flash("Please choose a valid office.", "error")
        return redirect(url_for("admin_staff"))
    conn.execute("UPDATE users SET office=?, position=? WHERE id=?", (office, OFFICES[office]["label"], staff_id))
    dbh.log_action(conn, user, "Changed staff office", target=row["email"], details=f"office={row['office']} -> {office}")
    conn.commit()
    conn.close()
    flash(f"{row['full_name']} is now assigned to the {OFFICES[office]['label']} office.", "success")
    return redirect(url_for("admin_staff"))


@app.route("/admin/staff/<int:staff_id>/toggle", methods=["POST"])
@login_required("admin")
def toggle_staff(user, staff_id):
    conn = dbh.get_db()
    row = conn.execute("SELECT * FROM users WHERE id=? AND role='staff'", (staff_id,)).fetchone()
    if not row:
        conn.close()
        abort(404)
    new_status = "inactive" if row["status"] == "active" else "active"
    conn.execute("UPDATE users SET status=? WHERE id=?", (new_status, staff_id))
    dbh.log_action(conn, user, f"Set staff account to {new_status}", target=row["email"])
    conn.commit()
    conn.close()
    flash(f"Staff account {new_status}.", "success")
    return redirect(url_for("admin_staff"))


@app.route("/admin/residents/<int:resident_id>/toggle", methods=["POST"])
@login_required("admin")
def toggle_resident(user, resident_id):
    conn = dbh.get_db()
    row = conn.execute("SELECT * FROM users WHERE id=? AND role='resident'", (resident_id,)).fetchone()
    if not row:
        conn.close()
        abort(404)
    new_status = "inactive" if row["status"] == "active" else "active"
    conn.execute("UPDATE users SET status=? WHERE id=?", (new_status, resident_id))
    dbh.log_action(conn, user, f"Set resident account to {new_status}", target=row["email"],
                   details="Marked inactive, e.g. moved away or requested closure" if new_status == "inactive" else None)
    conn.commit()
    conn.close()
    flash(f"Resident account {new_status}.", "success")
    return redirect(url_for("admin_residents"))


@app.route("/admin/staff/<int:staff_id>/reset-password", methods=["POST"])
@login_required("admin")
def reset_staff_password(user, staff_id):
    conn = dbh.get_db()
    row = conn.execute("SELECT * FROM users WHERE id=? AND role='staff'", (staff_id,)).fetchone()
    if not row:
        conn.close()
        abort(404)
    temp_pw = dbh.gen_temp_password()
    conn.execute(
        "UPDATE users SET password_hash=?, must_reset_pw=1 WHERE id=?",
        (generate_password_hash(temp_pw), staff_id),
    )
    dbh.log_action(conn, user, "Reset staff password", target=row["email"])
    conn.commit()
    conn.close()
    flash(f"Password reset for {row['full_name']}. New temporary password: {temp_pw}", "success")
    return redirect(url_for("admin_staff"))


@app.route("/admin/administrators", methods=["GET", "POST"])
@login_required("admin")
def admin_administrators(user):
    conn = dbh.get_db()
    if request.method == "POST":
        if not user["is_super_admin"]:
            conn.close()
            abort(403)
        full_name = request.form.get("full_name", "").strip()
        email = request.form.get("email", "").strip().lower()
        recovery_email = request.form.get("recovery_email", "").strip().lower()
        password = request.form.get("password", "")
        raw_contact = request.form.get("contact_number", "").strip()
        contact_number = _normalize_ph_mobile(raw_contact) if raw_contact else None

        if not full_name or not valid_email(email) or not valid_email(recovery_email):
            flash("Please provide a valid name, email, and recovery email.", "error")
        elif raw_contact and not contact_number:
            flash("Enter a valid Philippine mobile number, such as 0917 123 4567.", "error")
        elif not valid_password(password):
            flash("Password must be at least 8 characters and include a letter and a number.", "error")
        elif conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone():
            flash("An account with this email already exists.", "error")
        else:
            conn.execute(
                "INSERT INTO users (full_name, email, recovery_email, password_hash, role, "
                "contact_number, created_by) VALUES (?,?,?,?,?,?,?)",
                (
                    full_name,
                    email,
                    recovery_email,
                    generate_password_hash(password),
                    "admin",
                    contact_number,
                    user["id"],
                ),
            )
            dbh.log_action(conn, user, "Created additional administrator account", target=email)
            conn.commit()
            flash(f"Administrator account created for {full_name}.", "success")
        conn.close()
        return redirect(url_for("admin_administrators"))

    rows = conn.execute(
        "SELECT id, full_name, email, role, is_super_admin, created_at, contact_number "
        "FROM users WHERE role='admin' ORDER BY created_at ASC"
    ).fetchall()
    conn.close()
    return render_template("admin/administrators.html", rows=rows)


@app.route("/admin/requests")
@login_required("admin")
def admin_requests(user):
    conn = dbh.get_db()
    status = request.args.get("status", "")
    q = "SELECT r.*, u.full_name resident_name FROM requests r JOIN users u ON u.id=r.resident_id"
    params = []
    if status:
        q += " WHERE r.status=?"
        params.append(status)
    q += " ORDER BY r.created_at DESC"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return render_template("admin/requests.html", rows=rows, statuses=REQUEST_STATUSES, active_status=status)


@app.route("/admin/complaints")
@login_required("admin")
def admin_complaints(user):
    conn = dbh.get_db()
    status = request.args.get("status", "")
    priority_filter = request.args.get("priority", "")
    q = "SELECT c.*, u.full_name resident_name, u.address resident_address, u.contact_number resident_contact FROM complaints c JOIN users u ON u.id=c.resident_id"
    clauses, params = [], []
    if status:
        clauses.append("c.status=?")
        params.append(status)
    if priority_filter:
        clauses.append("c.priority=?")
        params.append(priority_filter)
    if clauses:
        q += " WHERE " + " AND ".join(clauses)
    q += " ORDER BY CASE c.priority WHEN 'High' THEN 0 ELSE 1 END, c.created_at DESC"
    rows = conn.execute(q, params).fetchall()
    related = related_complaint_clusters(conn, user["id"])
    conn.close()
    return render_template("admin/complaints.html", rows=rows, statuses=COMPLAINT_STATUSES,
                            active_status=status, priorities=PRIORITY_LEVELS, active_priority=priority_filter,
                            related_clusters=related)


@app.route("/admin/audit-logs")
@login_required("admin")
def audit_logs(user):
    conn = dbh.get_db()
    rows = conn.execute("SELECT * FROM audit_logs ORDER BY created_at DESC LIMIT 300").fetchall()
    conn.close()
    return render_template("admin/audit_logs.html", rows=rows)


SETTINGS_FIELDS = [
    ("barangay_name", "Barangay name"),
    ("barangay_address", "Barangay address"),
    ("barangay_main_number", "Main line"),
    ("barangay_base_number", "100 Base"),
    ("barangay_annex_number", "Annex"),
    ("barangay_annex2_number", "Annex II"),
    ("traffic_number", "Traffic"),
    ("bhert_number", "BHERT"),
    ("mrcf_number", "MRCF"),
    ("dogpound_number", "Dogpound"),
    ("pnp_number", "PNP"),
    ("bfp_number", "BFP"),
    ("bdrrm_number", "BDRRM"),
]


@app.route("/admin/settings", methods=["GET", "POST"])
@login_required("admin")
def admin_settings(user):
    conn = dbh.get_db()
    if request.method == "POST":
        for key, _label in SETTINGS_FIELDS:
            dbh.set_setting(conn, key, request.form.get(key, "").strip())
        dbh.log_action(conn, user, "Updated barangay settings")
        conn.commit()
        conn.close()
        flash("Settings saved.", "success")
        return redirect(url_for("admin_settings"))

    values = {key: dbh.get_setting(conn, key, "") for key, _label in SETTINGS_FIELDS}
    conn.close()
    backups = [dbh.backup_info(n) for n in dbh.list_backups()]
    return render_template("admin/settings.html", fields=SETTINGS_FIELDS, values=values,
                           backups=backups, backup_keep=dbh.BACKUP_KEEP,
                           backup_every=dbh.AUTO_BACKUP_EVERY_HOURS)


@app.route("/admin/settings/backup", methods=["POST"])
@login_required("admin")
def create_backup(user):
    """Take a fresh backup right now and download it."""
    path = dbh.backup_database()
    conn = dbh.get_db()
    dbh.log_action(conn, user, "Downloaded database backup", target=os.path.basename(path))
    conn.commit()
    conn.close()
    return send_file(path, as_attachment=True, download_name=os.path.basename(path))


@app.route("/admin/settings/backup/<name>")
@login_required("admin")
def download_backup(user, name):
    """Download one of the existing backups (name must be one we created)."""
    if name not in dbh.list_backups():
        abort(404)
    conn = dbh.get_db()
    dbh.log_action(conn, user, "Downloaded database backup", target=name)
    conn.commit()
    conn.close()
    return send_file(os.path.join(dbh.backup_dir(), name), as_attachment=True, download_name=name)


@app.route("/admin/announcements", methods=["GET", "POST"])
@login_required("admin")
def admin_announcements(user):
    conn = dbh.get_db()
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        message = request.form.get("message", "").strip()
        if not title or not message:
            conn.close()
            flash("Please provide both a title and a message.", "error")
            return redirect(url_for("admin_announcements"))

        cur = conn.execute(
            "INSERT INTO announcements (title, message, posted_by_name) VALUES (?,?,?)",
            (title, message, user["full_name"]),
        )
        ann_id = cur.lastrowid
        residents = conn.execute("SELECT id FROM users WHERE role='resident' AND status='active'").fetchall()
        for r in residents:
            dbh.notify(conn, r["id"], f"Barangay Announcement: {title}", url_for("resident_dashboard"))
        dbh.log_action(conn, user, "Posted announcement", target=title, details=f"Sent to {len(residents)} residents")
        conn.commit()
        conn.close()
        flash(f"Announcement posted to {len(residents)} residents.", "success")
        return redirect(url_for("admin_announcements"))

    rows = conn.execute("SELECT * FROM announcements ORDER BY created_at DESC LIMIT 50").fetchall()
    conn.close()
    return render_template("admin/announcements.html", rows=rows)


REPORT_RANGES = {
    "today": "Today",
    "week": "This Week",
    "month": "This Month",
    "last_month": "Last Month",
    "year": "This Year",
    "custom": "Custom Range",
    "all": "All Time",
}


def _month_start(value):
    return value.replace(day=1)


def _next_month(value):
    return (value.replace(day=28) + timedelta(days=4)).replace(day=1)


def _analytics_filters(args):
    range_key = args.get("range", "year")
    if range_key not in REPORT_RANGES:
        range_key = "year"
    filters = {
        "range": range_key,
        "start_date": args.get("start_date", "").strip(),
        "end_date": args.get("end_date", "").strip(),
        "category": args.get("category", "").strip(),
        "document_type": args.get("document_type", "").strip(),
        "status": args.get("status", "").strip(),
    }
    if filters["category"] not in COMPLAINT_CATEGORIES:
        filters["category"] = ""
    if filters["document_type"] not in REQUEST_TYPES:
        filters["document_type"] = ""
    if filters["status"] not in set(COMPLAINT_STATUSES + REQUEST_STATUSES):
        filters["status"] = ""
    return filters


def _analytics_bounds(filters, today=None):
    today = today or datetime.now().date()
    range_key = filters["range"]
    if range_key == "today":
        start = today
        end = today + timedelta(days=1)
    elif range_key == "week":
        start = today - timedelta(days=today.weekday())
        end = start + timedelta(days=7)
    elif range_key == "month":
        start = _month_start(today)
        end = _next_month(start)
    elif range_key == "last_month":
        end = _month_start(today)
        start = (end - timedelta(days=1)).replace(day=1)
    elif range_key == "year":
        start = today.replace(month=1, day=1)
        end = today.replace(year=today.year + 1, month=1, day=1)
    elif range_key == "custom":
        try:
            start = datetime.strptime(filters["start_date"], "%Y-%m-%d").date()
            last_day = datetime.strptime(filters["end_date"], "%Y-%m-%d").date()
        except ValueError:
            raise ValueError("Choose valid start and end dates for the custom range.")
        if last_day < start:
            raise ValueError("The end date must be on or after the start date.")
        end = last_day + timedelta(days=1)
    else:
        return None, None
    return start, end


def _report_where(user, table, filters, bounds):
    clauses = []
    params = []
    start, end = bounds
    if start:
        clauses.append("created_at>=?")
        params.append(start.strftime("%Y-%m-%d 00:00:00"))
    if end:
        clauses.append("created_at<?")
        params.append(end.strftime("%Y-%m-%d 00:00:00"))
    if filters["status"]:
        clauses.append("status=?")
        params.append(filters["status"])

    if table == "complaints":
        if filters["category"]:
            clauses.append("category=?")
            params.append(filters["category"])
        if user["role"] == "staff" and not can_handle_complaints(user):
            clauses.append("0")
    else:
        if filters["document_type"]:
            clauses.append("request_type=?")
            params.append(filters["document_type"])
        if user["role"] == "staff":
            allowed_types = handled_request_types(user)
            if not allowed_types:
                clauses.append("0")
            else:
                clauses.append("request_type IN (" + ",".join("?" for _ in allowed_types) + ")")
                params.extend(allowed_types)
    return " WHERE " + (" AND ".join(clauses) if clauses else "1=1"), params


def _report_count(conn, user, table, filters, bounds, extra=""):
    where, params = _report_where(user, table, filters, bounds)
    row = conn.execute(
        f"SELECT COUNT(*) AS count FROM {table}{where}{extra}", params
    ).fetchone()
    return row["count"]


def _report_groups(conn, user, table, field, filters, bounds, previous_bounds=None):
    where, params = _report_where(user, table, filters, bounds)
    rows = conn.execute(
        f"SELECT {field} AS label, COUNT(*) AS count FROM {table}{where} "
        f"GROUP BY {field} ORDER BY count DESC, label ASC LIMIT 10",
        params,
    ).fetchall()
    values = [dict(label=row["label"] or "Unspecified", count=row["count"]) for row in rows]
    denominator = _report_count(conn, user, table, filters, bounds)

    previous = {}
    if previous_bounds:
        prev_where, prev_params = _report_where(user, table, filters, previous_bounds)
        previous_rows = conn.execute(
            f"SELECT {field} AS label, COUNT(*) AS count FROM {table}{prev_where} "
            f"GROUP BY {field}",
            prev_params,
        ).fetchall()
        previous = {row["label"] or "Unspecified": row["count"] for row in previous_rows}
    for row in values:
        row["percentage"] = round(row["count"] / denominator * 100, 1) if denominator else 0
        row["previous"] = previous.get(row["label"], 0)
    scale = max((max(row["count"], row["previous"]) for row in values), default=0)
    for row in values:
        row["current_width"] = round(row["count"] / scale * 100) if scale else 0
        row["previous_width"] = round(row["previous"] / scale * 100) if scale else 0
    return values


def _report_statuses(conn, user, table, filters, bounds):
    where, params = _report_where(user, table, filters, bounds)
    rows = conn.execute(
        f"SELECT status, COUNT(*) AS count FROM {table}{where} GROUP BY status",
        params,
    ).fetchall()
    counts = {row["status"]: row["count"] for row in rows}
    statuses = COMPLAINT_STATUSES if table == "complaints" else REQUEST_STATUSES
    total = sum(counts.values())
    return [
        {
            "label": status,
            "count": counts.get(status, 0),
            "percentage": round(counts.get(status, 0) / total * 100, 1) if total else 0,
        }
        for status in statuses
    ]


def _report_monthly(conn, user, table, filters, bounds):
    where, params = _report_where(user, table, filters, bounds)
    rows = conn.execute(
        f"SELECT strftime('%Y-%m', created_at) AS month, COUNT(*) AS count "
        f"FROM {table}{where} GROUP BY month ORDER BY month DESC LIMIT 12",
        params,
    ).fetchall()
    return [
        {"month": row["month"], "label": datetime.strptime(row["month"], "%Y-%m").strftime("%b %Y"),
         "count": row["count"]}
        for row in reversed(rows) if row["month"]
    ]


def _analytics_month_keys(bounds, monthly_rows):
    start, end = bounds
    if start and end:
        cursor = start.replace(day=1)
        last = (end - timedelta(days=1)).replace(day=1)
        keys = []
        while cursor <= last:
            keys.append(cursor.strftime("%Y-%m"))
            cursor = _next_month(cursor)
        return keys[-12:]
    existing = [row["month"] for row in monthly_rows if row["month"]]
    if not existing:
        return []
    last = datetime.strptime(max(existing), "%Y-%m").date().replace(day=1)
    first = datetime.strptime(min(existing), "%Y-%m").date().replace(day=1)
    cursor = max(first, (last.replace(day=1) - timedelta(days=335)).replace(day=1))
    keys = []
    while cursor <= last:
        keys.append(cursor.strftime("%Y-%m"))
        cursor = _next_month(cursor)
    return keys[-12:]


def _analytics_line_points(series, maximum):
    if not series:
        return ""
    denominator = max(1, len(series) - 1)
    maximum = max(1, maximum)
    return " ".join(
        f"{40 + index * 520 / denominator:.1f},"
        f"{145 - row['count'] / maximum * 115:.1f}"
        for index, row in enumerate(series)
    )


def _report_month_count(conn, user, filters, bounds):
    month_values = []
    for table in ("complaints", "requests"):
        where, params = _report_where(user, table, filters, bounds)
        row = conn.execute(
            f"SELECT MIN(created_at) AS first, MAX(created_at) AS last FROM {table}{where}",
            params,
        ).fetchone()
        if row["first"] and row["last"]:
            month_values.append((
                datetime.fromisoformat(row["first"][:10]),
                datetime.fromisoformat(row["last"][:10]),
            ))
    if not month_values:
        return 1
    first = min(value[0] for value in month_values)
    last = max(value[1] for value in month_values)
    return max(1, (last.year - first.year) * 12 + last.month - first.month + 1)


def _analytics_comparison(conn, user, filters, bounds):
    start, end = bounds
    if not start or not end:
        return None
    duration = end - start
    previous_bounds = (start - duration, start)
    current_complaints = _report_count(conn, user, "complaints", filters, bounds)
    previous_complaints = _report_count(conn, user, "complaints", filters, previous_bounds)
    current_requests = _report_count(conn, user, "requests", filters, bounds)
    previous_requests = _report_count(conn, user, "requests", filters, previous_bounds)

    def change(current, previous):
        if previous == 0:
            return None
        return round((current - previous) / previous * 100, 1)

    return {
        "previous_start": previous_bounds[0],
        "previous_end": previous_bounds[1] - timedelta(days=1),
        "complaints": current_complaints,
        "previous_complaints": previous_complaints,
        "complaint_change": change(current_complaints, previous_complaints),
        "requests": current_requests,
        "previous_requests": previous_requests,
        "request_change": change(current_requests, previous_requests),
    }


def _build_report(conn, user, filters):
    bounds = _analytics_bounds(filters)
    previous_bounds = None
    comparison = _analytics_comparison(conn, user, filters, bounds)
    if comparison:
        previous_bounds = (
            comparison["previous_start"],
            comparison["previous_end"] + timedelta(days=1),
        )

    total_requests = _report_count(conn, user, "requests", filters, bounds)
    total_complaints = _report_count(conn, user, "complaints", filters, bounds)
    approved_requests = _report_count(
        conn, user, "requests", filters, bounds, " AND status='Approved'"
    )
    pending_requests = _report_count(
        conn, user, "requests", filters, bounds,
        " AND status IN ('Submitted','Under Review','Processing')",
    )
    verified_complaints = _report_count(
        conn, user, "complaints", filters, bounds, " AND is_verified=1"
    )
    pending_complaints = _report_count(
        conn, user, "complaints", filters, bounds, " AND status='Pending'"
    )
    resolved_complaints = _report_count(
        conn, user, "complaints", filters, bounds, " AND status IN ('Resolved','Closed')"
    )
    rejected = _report_count(
        conn, user, "requests", filters, bounds, " AND status='Rejected'"
    ) + _report_count(
        conn, user, "complaints", filters, bounds, " AND status='Rejected'"
    )

    complaint_categories = _report_groups(
        conn, user, "complaints", "category", filters, bounds, previous_bounds
    )
    document_types = _report_groups(
        conn, user, "requests", "request_type", filters, bounds, previous_bounds
    )
    complaint_statuses = _report_statuses(conn, user, "complaints", filters, bounds)
    request_statuses = _report_statuses(conn, user, "requests", filters, bounds)
    raw_complaint_months = _report_monthly(conn, user, "complaints", filters, bounds)
    raw_request_months = _report_monthly(conn, user, "requests", filters, bounds)
    month_keys = _analytics_month_keys(
        bounds, raw_complaint_months + raw_request_months
    )
    complaint_month_lookup = {row["month"]: row["count"] for row in raw_complaint_months}
    request_month_lookup = {row["month"]: row["count"] for row in raw_request_months}
    complaint_months = [
        {"month": key, "label": datetime.strptime(key, "%Y-%m").strftime("%b %y"),
         "count": complaint_month_lookup.get(key, 0)}
        for key in month_keys
    ]
    request_months = [
        {"month": key, "label": datetime.strptime(key, "%Y-%m").strftime("%b %y"),
         "count": request_month_lookup.get(key, 0)}
        for key in month_keys
    ]
    monthly_max = max(
        max((row["count"] for row in complaint_months), default=0),
        max((row["count"] for row in request_months), default=0),
    )

    start, end = bounds
    month_count = _report_month_count(conn, user, filters, bounds)
    category_leader = complaint_categories[0] if complaint_categories else None
    document_leader = document_types[0] if document_types else None
    peak_complaint_month = max(complaint_months, key=lambda row: row["count"], default=None)
    peak_request_month = max(request_months, key=lambda row: row["count"], default=None)
    filter_description = [
        ("Complaint category", filters["category"] or "All"),
        ("Document type", filters["document_type"] or "All"),
        ("Status", filters["status"] or "All"),
    ]

    return {
        "filters": filters,
        "range_label": REPORT_RANGES[filters["range"]],
        "start_date": start,
        "end_date": end - timedelta(days=1) if end else None,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "filter_description": filter_description,
        "total_requests": total_requests,
        "total_complaints": total_complaints,
        "total_transactions": total_requests + total_complaints,
        "approved_requests": approved_requests,
        "pending_requests": pending_requests,
        "verified_complaints": verified_complaints,
        "pending_complaints": pending_complaints,
        "resolved_complaints": resolved_complaints,
        "rejected": rejected,
        "average_complaints": round(total_complaints / month_count, 1),
        "average_requests": round(total_requests / month_count, 1),
        "complaint_categories": complaint_categories,
        "document_types": document_types,
        "complaint_statuses": complaint_statuses,
        "request_statuses": request_statuses,
        "complaint_months": complaint_months,
        "request_months": request_months,
        "complaint_month_max": max((row["count"] for row in complaint_months), default=0),
        "request_month_max": max((row["count"] for row in request_months), default=0),
        "monthly_max": monthly_max,
        "complaint_line_points": _analytics_line_points(complaint_months, monthly_max),
        "request_line_points": _analytics_line_points(request_months, monthly_max),
        "comparison": comparison,
        "category_leader": category_leader,
        "document_leader": document_leader,
        "increasing_complaints": [
            row for row in complaint_categories if row["count"] > row["previous"]
        ],
        "increasing_documents": [
            row for row in document_types if row["count"] > row["previous"]
        ],
        "peak_complaint_month": peak_complaint_month,
        "peak_request_month": peak_request_month,
        "has_data": bool(total_requests or total_complaints),
        "complaint_doughnut": _status_doughnut(
            complaint_statuses, COMPLAINT_STATUS_COLORS
        ),
        "request_doughnut": _status_doughnut(
            request_statuses, REQUEST_STATUS_COLORS
        ),
    }


def _status_doughnut(rows, colors):
    total = sum(row["count"] for row in rows)
    if not total:
        return "conic-gradient(#E7ECF3 0 100%)"
    stops = []
    accumulated = 0.0
    for row in rows:
        count = row["count"]
        if not count:
            continue
        end = accumulated + count / total * 100
        stops.append(
            f"{colors[row['label']]} {accumulated:.2f}% {end:.2f}%"
        )
        accumulated = end
    return "conic-gradient(" + ", ".join(stops) + ")"


@app.route("/admin/reports")
@app.route("/reports")
@login_required("staff", "admin")
def admin_reports(user):
    if user["role"] == "staff" and not (
        handled_request_types(user) or can_handle_complaints(user)
    ):
        abort(403)
    filters = _analytics_filters(request.args)
    filter_error = None
    conn = dbh.get_db()
    try:
        report = _build_report(conn, user, filters)
    except ValueError as exc:
        filter_error = str(exc)
        filters.update(range="year", start_date="", end_date="")
        report = _build_report(conn, user, filters)
    finally:
        conn.close()
    export_urls = {
        fmt: url_for("export_report", **filters, format=fmt)
        for fmt in ("csv", "pdf", "xlsx")
    }
    return render_template(
        "admin/reports.html", report=report, filter_error=filter_error,
        export_urls=export_urls,
        complaint_categories=COMPLAINT_CATEGORIES,
        request_types=handled_request_types(user) if user["role"] == "staff" else REQUEST_TYPES,
        complaint_statuses=COMPLAINT_STATUSES,
        request_statuses=REQUEST_STATUSES,
        complaint_access=can_handle_complaints(user),
        complaint_status_colors=COMPLAINT_STATUS_COLORS,
        request_status_colors=REQUEST_STATUS_COLORS,
    )


@app.route("/admin/reports/export")
@app.route("/reports/export")
@login_required("staff", "admin")
def export_report(user):
    if user["role"] == "staff" and not (
        handled_request_types(user) or can_handle_complaints(user)
    ):
        abort(403)
    filters = _analytics_filters(request.args)
    fmt = request.args.get("format", "csv")
    if fmt not in {"csv", "pdf", "xlsx"}:
        abort(400)
    conn = dbh.get_db()
    try:
        report = _build_report(conn, user, filters)
    except ValueError as exc:
        conn.close()
        abort(400, description=str(exc))
    conn.close()

    sections = [
        ("Complaint Categories", report["complaint_categories"]),
        ("Document Types", report["document_types"]),
        ("Complaint Statuses", report["complaint_statuses"]),
        ("Request Statuses", report["request_statuses"]),
        ("Monthly Complaints", report["complaint_months"]),
        ("Monthly Requests", report["request_months"]),
    ]
    summary = [
        ("Total Complaints", report["total_complaints"]),
        ("Total Document Requests", report["total_requests"]),
        ("Total Transactions", report["total_transactions"]),
        ("Verified Complaints", report["verified_complaints"]),
        ("Pending Complaints", report["pending_complaints"]),
        ("Resolved Complaints", report["resolved_complaints"]),
        ("Approved Requests", report["approved_requests"]),
        ("Pending Requests", report["pending_requests"]),
        ("Rejected Requests and Complaints", report["rejected"]),
        ("Average Complaints per Month", report["average_complaints"]),
        ("Average Requests per Month", report["average_requests"]),
    ]
    filename_base = f"barangayconnect_analytics_{filters['range']}"

    if fmt == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["BarangayConnect Analytics Report"])
        writer.writerow(["Period", report["range_label"]])
        writer.writerow(["Generated", report["generated_at"]])
        writer.writerow(["Start Date", report["start_date"] or "All time"])
        writer.writerow(["End Date", report["end_date"] or ""])
        writer.writerow(["Complaint Category", filters["category"] or "All"])
        writer.writerow(["Document Type", filters["document_type"] or "All"])
        writer.writerow(["Status", filters["status"] or "All"])
        writer.writerow([])
        writer.writerow(["Summary Metric", "Value"])
        writer.writerows(summary)
        for section_name, rows in sections:
            writer.writerow([])
            writer.writerow([section_name])
            writer.writerow(["Name", "Count", "Percentage", "Previous Period Count"])
            for row in rows:
                writer.writerow([
                    row.get("label", row.get("month", "")), row["count"],
                    row.get("percentage", ""), row.get("previous", ""),
                ])
        return send_file(
            io.BytesIO(buf.getvalue().encode("utf-8-sig")), mimetype="text/csv",
            as_attachment=True, download_name=f"{filename_base}.csv",
        )

    if fmt == "pdf":
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.lib.units import inch
        from reportlab.platypus import (
            Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
        )
        buf = io.BytesIO()
        doc = SimpleDocTemplate(buf, pagesize=letter, rightMargin=0.55 * inch,
                                leftMargin=0.55 * inch, topMargin=0.55 * inch,
                                bottomMargin=0.55 * inch)
        styles = getSampleStyleSheet()
        story = [
            Paragraph("BarangayConnect Analytics Report", styles["Title"]),
            Paragraph(
                f"{report['range_label']} · "
                f"{report['start_date'] or 'All time'} to {report['end_date'] or 'Present'}",
                styles["Normal"],
            ),
            Paragraph(f"Generated: {report['generated_at']}", styles["Normal"]),
            Paragraph(
                "Filters: " + " · ".join(f"{label}: {value}" for label, value in report["filter_description"]),
                styles["Normal"],
            ),
            Spacer(1, 12),
        ]
        summary_table = Table([["Summary Metric", "Value"]] + [
            [label, str(value)] for label, value in summary
        ], repeatRows=1, hAlign="LEFT")
        summary_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#17345D")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#D7DFEA")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F3F6FA")]),
            ("PADDING", (0, 0), (-1, -1), 6),
        ]))
        story.extend([summary_table, Spacer(1, 14)])
        for section_name, rows in sections:
            story.append(Paragraph(section_name, styles["Heading2"]))
            data = [["Name", "Count", "Percentage", "Previous Period"]]
            data.extend([
                [
                    str(row.get("label", row.get("month", ""))),
                    str(row["count"]), f"{row.get('percentage', '')}%",
                    str(row.get("previous", "")),
                ]
                for row in rows
            ])
            table = Table(data, repeatRows=1, hAlign="LEFT")
            table.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2451B0")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#D7DFEA")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F3F6FA")]),
                ("PADDING", (0, 0), (-1, -1), 5),
            ]))
            story.extend([table, Spacer(1, 10)])
        doc.build(story)
        buf.seek(0)
        return send_file(buf, mimetype="application/pdf", as_attachment=True,
                         download_name=f"{filename_base}.pdf")

    from openpyxl import Workbook
    wb = Workbook()
    summary_ws = wb.active
    summary_ws.title = "Summary"
    summary_ws.append(["BarangayConnect Analytics Report", report["range_label"]])
    summary_ws.append(["Generated", report["generated_at"]])
    summary_ws.append(["Start Date", str(report["start_date"] or "All time")])
    summary_ws.append(["End Date", str(report["end_date"] or "")])
    for label, value in report["filter_description"]:
        summary_ws.append([label, value])
    summary_ws.append([])
    summary_ws.append(["Metric", "Value"])
    for label, value in summary:
        summary_ws.append([label, value])
    for section_name, rows in sections:
        ws = wb.create_sheet(section_name[:31])
        ws.append(["Name", "Count", "Percentage", "Previous Period Count"])
        for row in rows:
            ws.append([
                row.get("label", row.get("month", "")), row["count"],
                row.get("percentage", ""), row.get("previous", ""),
            ])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(
        buf, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True, download_name=f"{filename_base}.xlsx",
    )


@app.route("/search")
@login_required("resident", "staff", "admin")
def search(user):
    q = request.args.get("q", "").strip()
    conn = dbh.get_db()
    req_rows, comp_rows = [], []
    if q:
        like = f"%{q}%"
        if user["role"] == "resident":
            req_rows = conn.execute(
                "SELECT * FROM requests WHERE resident_id=? AND "
                "(reference_no LIKE ? OR request_type LIKE ? OR status LIKE ?) ORDER BY created_at DESC",
                (user["id"], like, like, like),
            ).fetchall()
            comp_rows = conn.execute(
                "SELECT * FROM complaints WHERE resident_id=? AND "
                "(reference_no LIKE ? OR category LIKE ? OR status LIKE ?) ORDER BY created_at DESC",
                (user["id"], like, like, like),
            ).fetchall()
        else:
            types = handled_request_types(user)
            if types:
                clause, tparams = _in_clause(types)
                req_rows = conn.execute(
                    "SELECT r.*, u.full_name resident_name FROM requests r JOIN users u ON u.id=r.resident_id "
                    f"WHERE r.request_type {clause} AND "
                    "(r.reference_no LIKE ? OR r.request_type LIKE ? OR r.status LIKE ? OR u.full_name LIKE ?) "
                    "ORDER BY r.created_at DESC",
                    tparams + [like, like, like, like],
                ).fetchall()
            if can_handle_complaints(user):
                comp_rows = conn.execute(
                    "SELECT c.*, u.full_name resident_name FROM complaints c JOIN users u ON u.id=c.resident_id "
                    "WHERE c.reference_no LIKE ? OR c.category LIKE ? OR c.status LIKE ? OR u.full_name LIKE ? "
                    "ORDER BY c.created_at DESC",
                    (like, like, like, like),
                ).fetchall()
    conn.close()
    return render_template("search.html", q=q, req_rows=req_rows, comp_rows=comp_rows)


@app.route("/uploads/<subdir>/<stored_name>")
@login_required("resident", "staff", "admin")
def serve_upload(user, subdir, stored_name):
    if subdir not in {"requests", "complaints", "profiles"}:
        abort(404)

    safe_name = os.path.basename(stored_name)
    path = os.path.abspath(os.path.join(UPLOAD_DIR, subdir, safe_name))
    if os.path.commonpath([UPLOAD_DIR, path]) != os.path.abspath(UPLOAD_DIR):
        abort(403)

    record = None
    conn = dbh.get_db()
    if subdir == "requests":
        record = conn.execute(
            "SELECT f.*, r.resident_id, r.request_type FROM request_files f JOIN requests r ON r.id=f.request_id WHERE f.stored_name=?",
            (safe_name,),
        ).fetchone()
    elif subdir == "complaints":
        record = conn.execute(
            "SELECT f.*, c.resident_id, c.reference_no FROM complaint_files f JOIN complaints c ON c.id=f.complaint_id WHERE f.stored_name=?",
            (safe_name,),
        ).fetchone()
    elif subdir == "profiles":
        record = conn.execute("SELECT id, profile_picture AS stored_name, id AS resident_id FROM users WHERE profile_picture=?", (safe_name,)).fetchone()
    conn.close()

    if not record:
        abort(403)

    if user["role"] == "resident":
        if record["resident_id"] != user["id"]:
            abort(403)
    elif user["role"] == "staff":
        if subdir == "requests":
            if not can_handle_request_type(user, record["request_type"]):
                abort(403)
        elif subdir == "complaints":
            if not can_handle_complaints(user):
                abort(403)
        elif subdir == "profiles":
            if record["resident_id"] != user["id"]:
                abort(403)
    elif user["role"] == "admin":
        pass

    if not os.path.isfile(path):
        abort(404)

    if record and subdir in {"requests", "complaints"}:
        filename = record["filename"]
        return send_file(path, as_attachment=True, download_name=filename)
    return send_file(path)


# ------------------------------------------------------------- JSON API ---
# Thin JSON wrappers mirroring the PRD's Backend/API Requirements, for use
# by external/mobile clients or automated testing.

@app.route("/api/auth/login", methods=["POST"])
def api_login():
    data = request.get_json(force=True, silent=True) or {}
    conn = dbh.get_db()
    user = conn.execute("SELECT * FROM users WHERE email=?", (data.get("email", "").lower(),)).fetchone()
    if not user or user["status"] != "active" or not check_password_hash(user["password_hash"], data.get("password", "")):
        conn.close()
        return jsonify(error="Invalid credentials"), 401
    conn.close()
    return jsonify(id=user["id"], full_name=user["full_name"], role=user["role"])


@app.route("/api/dashboard/<role>")
@login_required("resident", "staff", "admin")
def api_dashboard(user, role):
    if user["role"] != role:
        abort(403)
    conn = dbh.get_db()
    if role == "resident":
        data = dict(
            active_requests=conn.execute(
                "SELECT COUNT(*) c FROM requests WHERE resident_id=? AND status NOT IN ('Completed','Rejected')",
                (user["id"],)).fetchone()["c"],
            pending_complaints=conn.execute(
                "SELECT COUNT(*) c FROM complaints WHERE resident_id=? AND status NOT IN ('Resolved','Closed','Rejected')",
                (user["id"],)).fetchone()["c"],
        )
    elif role == "staff":
        data = {}
        types = handled_request_types(user)
        if types:
            clause, tparams = _in_clause(types)
            data["pending_approvals"] = conn.execute(
                f"SELECT COUNT(*) c FROM requests WHERE request_type {clause} "
                "AND status IN ('Submitted','Under Review','Processing')", tparams
            ).fetchone()["c"]
        if can_handle_complaints(user):
            data["open_complaints"] = conn.execute(
                "SELECT COUNT(*) c FROM complaints WHERE status NOT IN ('Resolved','Closed','Rejected')"
            ).fetchone()["c"]
    else:
        data = dict(
            total_residents=conn.execute("SELECT COUNT(*) c FROM users WHERE role='resident'").fetchone()["c"],
            pending_requests=conn.execute(
                "SELECT COUNT(*) c FROM requests WHERE status NOT IN ('Completed','Rejected')").fetchone()["c"],
            open_complaints=conn.execute(
                "SELECT COUNT(*) c FROM complaints WHERE status NOT IN ('Resolved','Closed','Rejected')").fetchone()["c"],
        )
    conn.close()
    return jsonify(data)


@app.route("/hotlines")
@login_required("resident", "staff", "admin")
def hotlines(user):
    conn = dbh.get_db()
    values = {key: dbh.get_setting(conn, key, "") for key, _label in SETTINGS_FIELDS}
    conn.close()
    return render_template("hotlines.html", values=values)


@app.route("/assistant")
@login_required("resident")
def assistant(user):
    conn = dbh.get_db()
    rows = conn.execute(
        "SELECT * FROM chat_messages WHERE user_id=? ORDER BY created_at ASC", (user["id"],)
    ).fetchall()
    conn.close()
    return render_template("resident/assistant.html", rows=rows, api_enabled=bool(chatbot.ANTHROPIC_API_KEY))


@app.route("/api/assistant/history")
@login_required("resident")
def api_assistant_history(user):
    conn = dbh.get_db()
    rows = conn.execute(
        "SELECT role, message, created_at FROM chat_messages WHERE user_id=? ORDER BY created_at ASC", (user["id"],)
    ).fetchall()
    conn.close()
    return jsonify(messages=[{"role": r["role"], "message": r["message"]} for r in rows])


@app.route("/api/assistant/message", methods=["POST"])
@login_required("resident")
def api_assistant_message(user):
    data = request.get_json(force=True, silent=True) or {}
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify(error="Empty message"), 400
    if len(message) > 1000:
        message = message[:1000]

    conn = dbh.get_db()
    conn.execute(
        "INSERT INTO chat_messages (user_id, role, message) VALUES (?,?,?)",
        (user["id"], "user", message),
    )
    conn.commit()

    history_rows = conn.execute(
        "SELECT role, message FROM chat_messages WHERE user_id=? ORDER BY created_at ASC LIMIT 20",
        (user["id"],),
    ).fetchall()
    history = [{"role": r["role"], "content": r["message"]} for r in history_rows[:-1]]

    reply = chatbot.get_reply(conn, user, message, history)

    conn.execute(
        "INSERT INTO chat_messages (user_id, role, message) VALUES (?,?,?)",
        (user["id"], "assistant", reply),
    )
    conn.commit()
    conn.close()
    return jsonify(reply=reply)


@app.errorhandler(403)
def forbidden(e):
    return render_template("error.html", code=403, message="You don't have permission to view this page."), 403


@app.errorhandler(404)
def not_found(e):
    return render_template("error.html", code=404, message="Page not found."), 404


@app.errorhandler(RequestEntityTooLarge)
def request_entity_too_large(e):
    if request.endpoint == "profile":
        flash(f"Profile picture must be {PROFILE_IMAGE_MAX_SIZE // (1024 * 1024)} MB or smaller.", "error")
        return redirect(url_for("profile"))
    return e.get_response()


if __name__ == "__main__":
    if not os.path.isfile(dbh.DB_PATH):
        dbh.init_db()
    else:
        dbh.migrate_db()
    app.run(debug=os.environ.get("FLASK_DEBUG", "0") == "1", host="127.0.0.1", port=5000, use_reloader=False)
