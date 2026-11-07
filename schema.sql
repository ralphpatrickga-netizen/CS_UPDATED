-- BarangayConnect database schema
-- Written for SQLite (used for local dev/demo). Column types map directly onto
-- MySQL equivalents (INTEGER->INT, TEXT->VARCHAR/TEXT, TIMESTAMP->DATETIME) if
-- you migrate to MySQL for production, per the PRD's target stack.

CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name       TEXT NOT NULL,
    email           TEXT NOT NULL UNIQUE,
    password_hash   TEXT NOT NULL,
    role            TEXT NOT NULL CHECK (role IN ('resident', 'staff', 'admin')),
    office          TEXT CHECK (office IN ('treasurer', 'secretary', 'bpso')),  -- staff only: which office they belong to (decides what they can access)
    position        TEXT,                 -- staff display label (derived from office)
    status          TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'inactive')),
    is_super_admin  INTEGER NOT NULL DEFAULT 0,   -- only the Founding Admin can create other admins
    must_reset_pw   INTEGER NOT NULL DEFAULT 0,  -- staff must set permanent password on first login
    failed_logins   INTEGER NOT NULL DEFAULT 0,
    locked_until    TEXT,
    address         TEXT,
    contact_number  TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    created_by      INTEGER,              -- admin who created this account (staff/admin), NULL for self-registered residents
    profile_picture TEXT,                 -- stored filename of uploaded profile photo, NULL if none
    recovery_email  TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_users_single_founding_admin
    ON users(is_super_admin)
    WHERE role='admin' AND is_super_admin=1;

CREATE TABLE IF NOT EXISTS requests (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    reference_no    TEXT NOT NULL UNIQUE,
    resident_id     INTEGER NOT NULL REFERENCES users(id),
    request_type    TEXT NOT NULL,        -- Barangay Clearance, Certificate of Indigency, Certificate of Residency
    purpose         TEXT,
    details         TEXT,
    contact_number  TEXT,                 -- contact number for this request (may differ from profile)
    request_address TEXT,                 -- house no./street/purok at time of request
    civil_status    TEXT,                 -- Single/Married/Widowed/Separated/Divorced
    age             INTEGER,
    computed_fee    INTEGER,              -- fee snapshot at time of filing (varies by purpose for some types)
    num_copies      INTEGER NOT NULL DEFAULT 1,
    claiming_date   TEXT,                 -- preferred claiming date (optional)
    has_representative    INTEGER NOT NULL DEFAULT 0,
    representative_name   TEXT,
    representative_id_type TEXT,          -- type of valid ID the representative will present
    registered_voter INTEGER NOT NULL DEFAULT 0,  -- Certificate of Residency only: 1 = free for registered voters
    status          TEXT NOT NULL DEFAULT 'Submitted'
                    CHECK (status IN ('Submitted','Under Review','Processing','Approved','Rejected','Completed')),
    assigned_staff_id INTEGER REFERENCES users(id),
    remarks         TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS request_files (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id      INTEGER NOT NULL REFERENCES requests(id),
    file_type       TEXT NOT NULL DEFAULT 'supporting_document'
                    CHECK (file_type IN ('valid_id','authorization_letter','representative_id','supporting_document')),
    label           TEXT,                 -- human-readable caption, e.g. a specific requirement name
    filename        TEXT NOT NULL,
    stored_name     TEXT NOT NULL,
    uploaded_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS complaints (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    reference_no    TEXT NOT NULL UNIQUE,
    resident_id     INTEGER NOT NULL REFERENCES users(id),
    category        TEXT NOT NULL,        -- Noise Complaint, Neighbor Dispute, Illegal Parking, etc.
    description     TEXT NOT NULL,
    location         TEXT,
    priority        TEXT NOT NULL DEFAULT 'Normal' CHECK (priority IN ('High','Normal')),
    priority_reason TEXT,                 -- why the system flagged this (keywords matched, etc.)
    priority_is_manual INTEGER NOT NULL DEFAULT 0,  -- 1 once staff/admin overrides the auto-detected value
    status          TEXT NOT NULL DEFAULT 'Pending'
                    CHECK (status IN ('Pending','Under Review','Scheduled','Resolved','Closed','Rejected')),
    assigned_staff_id INTEGER REFERENCES users(id),
    remarks         TEXT,
    community_issue_id INTEGER REFERENCES community_issues(id),
    is_verified     INTEGER NOT NULL DEFAULT 0,
    verified_by     INTEGER REFERENCES users(id),
    verified_at     TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS community_issues (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    category            TEXT NOT NULL,
    location            TEXT NOT NULL,
    normalized_location TEXT NOT NULL,
    status              TEXT NOT NULL CHECK (status IN ('Pending','Under Review','Scheduled','Resolved','Closed','Rejected')),
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS community_issue_updates (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    community_issue_id  INTEGER NOT NULL REFERENCES community_issues(id) ON DELETE CASCADE,
    status              TEXT NOT NULL CHECK (status IN ('Pending','Under Review','Scheduled','Resolved','Closed','Rejected')),
    changed_by          TEXT,
    message             TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS community_issue_follows (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id             INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    community_issue_id  INTEGER NOT NULL REFERENCES community_issues(id) ON DELETE CASCADE,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(user_id, community_issue_id)
);

CREATE TABLE IF NOT EXISTS complaint_files (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    complaint_id    INTEGER NOT NULL REFERENCES complaints(id),
    filename        TEXT NOT NULL,
    stored_name     TEXT NOT NULL,
    uploaded_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS notifications (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(id),
    message         TEXT NOT NULL,
    link            TEXT,
    is_read         INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Append-only audit log. Rows are never updated or deleted by the application layer.
CREATE TABLE IF NOT EXISTS audit_logs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id        INTEGER REFERENCES users(id),
    actor_name      TEXT,
    action          TEXT NOT NULL,
    target          TEXT,
    details         TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(id),
    role            TEXT NOT NULL CHECK (role IN ('user','assistant')),
    message         TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Simple key-value store for admin-configurable settings (barangay contact numbers, etc.)
CREATE TABLE IF NOT EXISTS settings (
    key             TEXT PRIMARY KEY,
    value           TEXT,
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Dated status timeline for requests and complaints (shown to residents as a
-- real history instead of a generic arrow diagram).
CREATE TABLE IF NOT EXISTS status_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type     TEXT NOT NULL CHECK (entity_type IN ('request','complaint')),
    entity_id       INTEGER NOT NULL,
    status          TEXT NOT NULL,
    changed_by_name TEXT,                 -- 'Resident' for the initial filing, else staff/admin full name
    remarks         TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_status_history_entity ON status_history(entity_type, entity_id);

CREATE TABLE IF NOT EXISTS password_reset_tokens (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash      TEXT NOT NULL UNIQUE,
    expires_at      TEXT NOT NULL,
    used_at         TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_password_reset_tokens_user ON password_reset_tokens(user_id);

CREATE TABLE IF NOT EXISTS password_reset_codes (
    user_id         INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    code_hash       TEXT,
    expires_at      TEXT NOT NULL,
    attempts        INTEGER NOT NULL DEFAULT 0,
    used_at         TEXT,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS password_reset_limits (
    user_id             INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    last_sent_at        TEXT,
    window_started_at   TEXT,
    sends_in_window     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS admin_otp_challenges (
    user_id             INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    email_otp_hash      TEXT,
    mode                TEXT NOT NULL DEFAULT 'email_only',
    expires_at          TEXT,
    sent_at             TEXT,
    attempts            INTEGER NOT NULL DEFAULT 0,
    window_started_at   TEXT,
    sends_in_window     INTEGER NOT NULL DEFAULT 0,
    used_at             TEXT
);

CREATE TABLE IF NOT EXISTS resident_concern_allowances (
    user_id             INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    remaining           INTEGER NOT NULL DEFAULT 5 CHECK (remaining BETWEEN 0 AND 5),
    total_submitted     INTEGER NOT NULL DEFAULT 0,
    cooldown_started_at TEXT,
    cooldown_expires_at TEXT
);

CREATE TABLE IF NOT EXISTS concern_allowance_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    complaint_id    INTEGER REFERENCES complaints(id) ON DELETE CASCADE,
    event_type      TEXT NOT NULL CHECK (event_type IN ('submitted','resolved_restoration','cooldown_refill')),
    slots           INTEGER NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_concern_allowance_event_once
    ON concern_allowance_events(complaint_id, event_type)
    WHERE complaint_id IS NOT NULL;

-- Admin broadcast announcements to all residents (e.g. holiday closures, new requirements).
CREATE TABLE IF NOT EXISTS announcements (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    title           TEXT NOT NULL,
    message         TEXT NOT NULL,
    posted_by_name  TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_requests_resident ON requests(resident_id);
CREATE INDEX IF NOT EXISTS idx_requests_status ON requests(status);
CREATE INDEX IF NOT EXISTS idx_requests_created_type_status ON requests(created_at, request_type, status);
CREATE INDEX IF NOT EXISTS idx_complaints_resident ON complaints(resident_id);
CREATE INDEX IF NOT EXISTS idx_complaints_status ON complaints(status);
CREATE INDEX IF NOT EXISTS idx_complaints_created_category_status ON complaints(created_at, category, status);
CREATE INDEX IF NOT EXISTS idx_complaints_community_issue ON complaints(community_issue_id);
CREATE INDEX IF NOT EXISTS idx_complaints_verification ON complaints(community_issue_id, is_verified);
CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_id);
CREATE INDEX IF NOT EXISTS idx_community_issue_follows_issue ON community_issue_follows(community_issue_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_community_issues_active_key
    ON community_issues(category, normalized_location)
    WHERE status IN ('Pending','Under Review','Scheduled');
