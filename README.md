# BarangayConnect

A working citizen request and complaint management system for a single barangay —
resident self-registration, document requests, complaint filing, staff processing
queues, an admin dashboard with reports/audit logs, in-app notifications, and search.

## ⚠️ About the tech stack (read this first)

The PRD asked for Django + MySQL. This build runs on **Flask + SQLite** instead,
for one practical reason: this sandbox has **no internet access**, so `pip install
django` and MySQL drivers could not be downloaded here — only the packages already
present in the image (Flask, openpyxl, reportlab...) could be used. Rather than hand
you an untested Django scaffold, this is a fully working, actually-tested app on the
closest available stack.

The database layer is plain SQL (`schema.sql` + parameterized queries), not
Django's ORM, so porting to MySQL later is mechanical: swap `sqlite3` for
`mysql-connector-python`/`PyMySQL` in `db.py`, adjust the small number of
SQLite-specific functions (`datetime('now')`, `AUTOINCREMENT`), and the schema
maps over almost 1:1. If you specifically need Django, this is a solid reference
implementation to port from — every route, permission check, and status flow in
the PRD is already implemented and working here.

## What's implemented

- **Resident self-registration & login** (AUTH-01) — client + server-side validation,
  hashed passwords, rate limiting / lockout after 5 failed attempts. Address is
  required at registration (it pre-fills document requests) and can be updated
  any time from My Profile, along with contact number.
- **Admin-only staff & administrator provisioning** (PROV-02) — no public staff/admin
  signup route exists anywhere; staff get a temp password and must set a permanent
  one on first login; admin login requires a time-limited email OTP with resend
  limits, server-side verification, and one-time use.
- **Email-based password recovery** — residents receive a time-limited, single-use
  six-digit code by SMTP, then enter that code and their new password on the
  reset page without logging in first. Codes are stored as keyed digests and
  limited to five attempts; passwords continue to use Werkzeug's password hashing.
- **Document requests** (REQ-03) — **Certificate of Indigency**,
  **Certificate of Residency** and **Barangay Clearance** are offered (simplified
  from the original five types per the client's request); realistic barangay-style form (civil status,
  age, contact number, house no./street/purok — pre-filled from the
  resident's profile — preferred claiming date, required valid ID upload,
  optional representative/authorized-claimant section with an authorization
  letter *and* a photo of the representative's own valid ID, chosen from a
  dropdown of common ID types); file upload with type/size/
  magic-byte validation (JPG/PNG/PDF, 5MB max); full status lifecycle with
  timestamped remarks. The purpose field only appears after a document type
  is chosen — there's no generic free-text purpose anymore. Number of copies only applies to Certificate of
  Indigency, since the other document types are single-copy by nature.
- **Complaints** (COMP-04) — category selector, a *required* location field,
  attachments, status badges, staff assignment, and **automatic priority
  detection** (🟠 High / 🔵 Normal). Staff and Admin views also show
  the complainant's registered address and contact number pulled from their
  profile. The
  system scans each complaint's category and description for danger/urgency signals
  (violence, fire, weapons, injury, threats, etc.) and flags it the moment it's filed —
  no configuration needed. High-priority complaints surface at the top of the staff/admin
  queue and trigger immediate visibility for BPSO staff and admins. Staff/Admin can
  always update complaint status and remarks from the detail page; priority remains
  system-detected for consistent triage. Residents have five database-backed
  concern submissions; exhausting the allowance starts a 24-hour cooldown, and
  a concern's first transition to Resolved restores one slot (up to five).
  Repeat active concerns for the same category/location
  are blocked until the prior concern is resolved, closed, or rejected.
- **Community Reports** — matching concerns by category and normalized location
  creates one shared community issue while preserving each resident's individual
  concern record. Residents can view multi-resident issues, follow/unfollow them,
  and receive in-app notifications when staff changes the shared status. Public
  reports show category, location, report/resident counts, dates, status, and a
  generic summary only; reporter identities, descriptions, and attachments remain
  private. Reporters are automatically following their issue. Staff assigned to
  complaints and admins have a dedicated Clustered Issues workspace to verify each
  report, update shared status, and send an optional message to reporters/followers.
- **Staff offices** — every staff account belongs to exactly one office, and the
  office decides what they can see and process (defined once in `OFFICES` in `app.py`):

  | Office | Handles |
  |--------|---------|
  | Barangay Treasurer | Barangay Clearance (and its fees) |
  | Barangay Secretary | Certificate of Indigency, Certificate of Residency |
  | BPSO (Public Safety) | Complaints |

  Enforced on the server, not just hidden in the menu: queues, detail pages,
  status updates, search, dashboard numbers, the JSON API and uploaded files all
  return **403** for another office's work. Staff with no office see nothing
  until an admin assigns one (fail closed). Admins pick the office when creating
  staff, can change it later from Manage Staff, and see a warning if an office
  has nobody active. The "Assign to staff" list only offers staff of the
  responsible office (and is validated server-side). New requests notify the
  responsible office; urgent complaints notify BPSO and admins. Admins keep
  full access.
- **Three dashboards** (DASH-05) — Resident / Staff / Admin, sharing one sidebar+
  topbar+card shell as specified, with the stat cards / recent activity table
  described in the PRD's UI reference.
- **In-app notifications** (NOTIF-09) on every status change.
- **Reports & analytics** (RPT-10) — daily/weekly/monthly/yearly, exportable as
  Excel (.xlsx) and PDF.
- **Global search & filters** (SRCH-11).
- **Resident AI Assistant** — a chat widget on the resident side that looks up
  the status of a request/complaint straight from the database when you paste
  its reference number, and answers common FAQs (document requirements, how
  to file a complaint, password recovery). Works offline with zero setup. If
  you set an `ANTHROPIC_API_KEY` environment variable (see below), open-ended
  questions that don't match those cases are instead answered by Claude.
- **Append-only audit log** of account provisioning and status changes.
- **Founding-admin-only administrator creation** — only the original seeded
  Administrator account can create additional Administrator accounts. Other
  admins can still manage staff, residents, requests, complaints, and reports,
  but the "Create Administrator" form is hidden and the endpoint returns 403
  for them — this keeps a single accountable owner for the system's
  highest-privilege role.
- **Emergency Hotlines page** (resident sidebar) — a static reference page
  listing national emergency numbers (911, PNP, BFP, Red Cross, etc.) plus the
  barangay's own address and full contact roster (Main line, 100 Base, Annex,
  Annex II, BDRRM, BHERT, MRCF, Traffic, Dogpound, local PNP/BFP), all
  editable by an Admin from Settings (falls back to "Not yet set" until
  configured — seeded with Barangay San Isidro's real published numbers by
  default). Informational only, not a dispatch system, per the PRD's stated
  limitations.
- **Profile pictures** — every account (Resident, Staff, Admin) has a "My
  Profile" page to upload a photo (JPG/PNG, 20MB max, with a local image preview). It shows in the topbar
  avatar everywhere, and Admins see residents' and staff's photos directly in
  the Manage Residents / Manage Staff tables (falls back to an initial-letter
  avatar if no photo is uploaded).
- **Admin Settings page** — lets any Admin set the barangay's name, address,
  and full contact roster, which then populate the Emergency Hotlines page
  (and the barangay address on Claim Slips) automatically.
- The **Administrators** page/nav link is only shown to the Founding Admin —
  other Admins don't see it in their sidebar at all, since they have no
  access to it anyway.
- Complaint priority (🟠 High / 🔵 Normal) is **system-determined only** —
  Staff/Admin can update a complaint's status and remarks, but the auto-detected
  priority itself remains aligned with the system's classification logic.
- **Purpose-based fees** — Certificate of Indigency and Barangay Clearance
  each replace the generic "Purpose" text field with a dropdown of real
  purposes (matching the barangay's actual fee schedule for Clearance, and a
  free-of-charge purpose list with its own attachment requirements for
  Indigency). The fee updates live as the resident picks a purpose.
  **Certificate of Residency** uses a free-text purpose and a "registered voter"
  checkbox: ₱50, or free for registered voters. All of this is informational
  only, matching the PRD's "no online payment processing" limitation.
- **Certificate of Residency payment flow** — the resident pays first at the
  **Barangay Treasurer Office** and keeps the official receipt, then claims the
  certificate at the **Barangay Secretary Office** and shows the receipt. The
  system does not track payment; it tells the resident (form, request page,
  claim slip and assistant) where to pay and where to claim.
- **Claim Slip** — once a request is Approved or Completed, a claim slip
  appears on its detail page (reference number, document type, fee, claiming
  location, and preferred claiming date), with a Print button that isolates
  just the slip — nothing else on the page prints. The claiming location
  differs by document type (e.g. Barangay Secretary Office for Indigency,
  Barangay Treasurer Office for Clearance), configured in `app.py`.
- **Status Timeline** — request and complaint detail pages show a real, dated
  history of every status change (who changed it and when) instead of a
  generic arrow diagram.
- **Change Password** — every account (Resident, Staff, Admin) can change
  their own password from My Profile, on top of the existing Admin-driven
  staff password reset and the resident self-service "Forgot Password" flow.
- **Resident account deactivation** (Admin-only) — for cases like a resident
  moving away or asking to close their account. This is a manual action: the
  system has no way to automatically detect that someone moved, so an Admin
  marks it once they learn about it (e.g. the resident requests a clearance
  for moving, or tells the barangay in person). Deactivating blocks their
  login without deleting their records; Staff can still view the resident
  list but cannot change this status themselves.
- **Broadcast announcements** (Admin) — an Admin can post a title + message
  that is delivered as a notification to every active resident and shown in a
  "Barangay Announcements" panel at the top of each resident's dashboard
  (e.g. holiday closures, new requirements). A full history is kept on the
  Admin's Announcements page, and each post is recorded in the audit log.
- **8-week trend chart** (Admin dashboard) — a line chart of requests and
  complaints filed per week over the last eight weeks, so the Admin can spot
  busy periods and see whether volume is rising or falling. It's drawn as
  plain inline SVG, so it needs no charting library or internet connection.
- The sidebar navigation is text-only (no emoji icons) on all roles.
- Role-based access control enforced server-side on every route (not just hidden
  UI), 403/404 pages, resident data scoped to its owner.

Not implemented (out of scope per the PRD's own "Limitations" section): SMS and
payment processing. Real password-reset and admin OTP email delivery require
the SMTP settings below.

## Requirements

- Python 3.10+
- pip

## Setup

```bash
# 1. Unzip and enter the project
cd barangayconnect

# 2. (Recommended) create a virtual environment
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. (Optional, for a fresh demo database only) set demo passwords without
#    putting them in shell history, then seed demo data. seed.py resets the DB.
#    Do NOT run seed.py on a database whose users/data you need to preserve.
$env:DEMO_ADMIN_PASSWORD = [Net.NetworkCredential]::new("", (Read-Host -AsSecureString "Demo admin password")).Password
$env:DEMO_STAFF_PASSWORD = [Net.NetworkCredential]::new("", (Read-Host -AsSecureString "Demo staff password")).Password
$env:DEMO_RESIDENT_PASSWORD = [Net.NetworkCredential]::new("", (Read-Host -AsSecureString "Demo resident password")).Password
python3 seed.py
$env:FOUNDING_ADMIN_NAME = "Ralph Patrick Ga"
$env:FOUNDING_ADMIN_PASSWORD = [Net.NetworkCredential]::new("", (Read-Host -AsSecureString "Founding Admin initial password")).Password
python3 setup_founding_admin.py
Remove-Item Env:DEMO_ADMIN_PASSWORD, Env:DEMO_STAFF_PASSWORD, Env:DEMO_RESIDENT_PASSWORD, Env:FOUNDING_ADMIN_PASSWORD

# 5. For an existing database, skip seed.py. Set only FOUNDING_ADMIN_NAME and
#    FOUNDING_ADMIN_PASSWORD above and run setup_founding_admin.py. The setup
#    updates the existing Founding Admin row in place and refuses ambiguous
#    accounts or an email already owned by someone else.
#
# 6. Set email environment variables before running the app. Copy the
#    placeholders in .env.example into your shell or deployment environment.
#    Do not commit real SMTP credentials.
#
#    Windows PowerShell example:
#    $env:SECRET_KEY = "replace-with-a-long-random-secret"
#    $env:SMTP_HOST = "smtp.example.com"
#    $env:SMTP_PORT = "587"
#    $env:SMTP_USERNAME = "your-smtp-username"
#    $env:SMTP_PASSWORD = "your-smtp-password"
#    $env:SMTP_FROM = "BarangayConnect <no-reply@example.com>"
#    $env:SMTP_USE_TLS = "true"
#    $env:SEMAPHORE_API_KEY = "your-semaphore-api-key"
#    $env:LOGIN_SMS_ENABLED = "false"
#
#    Configure optional expiration/resend settings from .env.example as needed.
#    Login currently requires an email code only. To require separate email and
#    SMS codes later, set LOGIN_SMS_ENABLED=true after configuring Semaphore.
#    An admin can
#    update existing users' numbers from Admin > Residents, Staff, and
#    Administrators; staff can update resident numbers from Staff > Residents.
#    The Founding Admin without a number can verify email once
#    and then register and verify a mobile number during login.

# 7. (Optional) enable Claude-powered answers in the Resident Assistant for
#    open-ended questions. Skip this — the assistant works fine without it.
export ANTHROPIC_API_KEY="sk-ant-..."      # Windows: set ANTHROPIC_API_KEY=sk-ant-...

# 8. Run the app
python3 app.py
```

Open **http://127.0.0.1:5000** in your browser.

`setup_founding_admin.py` always uses `ralphpatrickga@gmail.com` for the
Founding Admin login and recovery email. It hashes the administrator-supplied
initial password, requires a change after email-OTP verification, and records
the setup in the audit log. The Founding Admin email is also the address used
for login and admin email OTP. Admin password recovery is sent to the stored
recovery email; after setup, that is the Founding Admin email.

`seed.py` is destructive demo tooling: it deletes and rebuilds the local
database. Use it only when you intentionally want to discard all current data.
The seeded demo admin is a regular Administrator, not the Founding Admin.

To fill in your own barangay's contact numbers on the Emergency Hotlines page,
log in as an Admin and go to Settings — no code editing needed.

## Test accounts

| Role     | Email                             | Password source | Notes |
|----------|------------------------------------|-----------------|-------|
| Admin    | admin@barangayconnect.ph          | `DEMO_ADMIN_PASSWORD` | Regular Admin; login requires email OTP. SMTP must be configured. |
| Staff    | marco.staff@barangayconnect.ph    | `DEMO_STAFF_PASSWORD` | Treasurer |
| Staff    | ramon.staff@barangayconnect.ph    | `DEMO_STAFF_PASSWORD` | Secretary |
| Staff    | liza.staff@barangayconnect.ph     | `DEMO_STAFF_PASSWORD` | BPSO |
| Resident | juan.resident@example.com         | `DEMO_RESIDENT_PASSWORD` | |
| Resident | maria.resident@example.com        | `DEMO_RESIDENT_PASSWORD` | |
| Resident | pedro.resident@example.com         | `DEMO_RESIDENT_PASSWORD` | |

The demo passwords are supplied as environment variables and are not printed.
The Founding Admin is `ralphpatrickga@gmail.com`; configure its initial password
with `FOUNDING_ADMIN_PASSWORD` and change it after first OTP-verified login.


You can also register a brand-new resident account from the login page — staff and
admin accounts can only be created from inside the Admin Dashboard.

## Running the tests

```
python3 -m unittest tests.test_office_access tests.test_new_features tests.test_email_and_allowance -v
```

They use a temporary database (your `instance/` data is not touched) and check every
office against every request type and complaint route, uploads, search, the API,
assignment rules, the Certificate of Residency flow and the upgrade of an older
database.

**Upgrading an existing database:** `python3 app.py` upgrades an older
`instance/barangayconnect.db` automatically on start (adds each staff member's
office where it can be worked out from their old position/category, and the
registered-voter column). Any staff it cannot map have no access until an admin picks
their office under Manage Staff. Or just re-run `python3 seed.py` for fresh demo data.

## Project structure

```
barangayconnect/
├── app.py                 # All routes, RBAC, business logic
├── db.py                  # SQLite connection + helper functions
├── schema.sql             # Database schema
├── seed.py                # Demo data + test account generator
├── requirements.txt
├── tests/                  # unittest coverage for access, features, email, and allowance
├── templates/              # Jinja2 templates (server-rendered HTML)
│   ├── base.html            # Shared sidebar/topbar shell
│   ├── resident/ staff/ admin/
│   └── ...
├── static/css/style.css   # Design system (navy/cyan civic theme)
├── uploads/                # Resident-submitted attachments (created at runtime)
└── instance/               # SQLite database file (created at runtime)
```

## Notes on security choices made for you

- Passwords are hashed with Werkzeug's `generate_password_hash` (PBKDF2).
- All form input is validated server-side, not just in the browser.
- Uploaded files are checked by extension **and** by their first bytes (magic
  numbers), so a `.exe` renamed to `.pdf` is rejected.
- SQL uses parameterized queries throughout — no string-built SQL.
- Sessions use Flask's signed cookies; set a real `SECRET_KEY` environment
  variable before deploying anywhere public (a dev default is used otherwise).
- The audit log is insert-only — the app never issues `UPDATE`/`DELETE` against
  `audit_logs`.

## Deploying for real use

This ships with Flask's built-in dev server, which is fine for trying it out but
says so itself: not for production. For an actual barangay deployment, put it
behind a real WSGI server (gunicorn/uwsgi) and Apache/Nginx as the PRD's stack
specifies, move to MySQL, and set a strong `SECRET_KEY`.

## Backups, bulk actions, resident filters

- **Database backup** (Admin → Settings): "Back up now & download", plus an automatic backup roughly
  every 24 hours (newest 14 kept) in `instance/backups/`. The backup covers the database only —
  copy `uploads/` separately.
- **Bulk resolve/close** (Complaints pages, admin and BPSO staff): tick complaints, then Resolve or Close selected.
- **Manage Residents filters**: status, purok, and address search.
- **Staff/admin navigation badges**: Requests counts newly submitted requests visible to that role/office; Concerns counts pending concerns.
- **Resident concern limit**: each account has five persisted slots. The 24-hour
  cooldown begins only when the fifth available slot is consumed; it ends 24
  hours later with five new slots. A concern's first transition to Resolved
  restores one slot, up to five, and clears an active cooldown when a slot is
  restored.
- **Active duplicate blocking**: residents may submit an item once, but a matching request (document type and purpose) or concern (category and location) is blocked while the earlier one is active; filing is allowed again after completion or resolution.
- Tests: `python3 -m unittest tests.test_office_access tests.test_new_features tests.test_email_and_allowance -v`
