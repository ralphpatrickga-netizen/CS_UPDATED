# Product Requirement Document (PRD) — v2.0
## BarangayConnect: Web-Based Citizen Request and Complaint Management System

**Status:** Implemented (working reference build). This version supersedes the original
v1.0 PRD. Section 0 below summarizes what changed and why; the remaining sections are
the full spec as actually built and tested.

---

## 0. Changelog from v1.0

| # | Change | Reason |
|---|--------|--------|
| 1 | **Tech stack**: Flask + SQLite instead of Django + MySQL | The v1.0 target stack (Django/MySQL) could not be installed in the build environment (no internet access). Flask + SQLite is the closest available stack, fully tested end-to-end. The schema is plain SQL (not ORM-specific), so porting to MySQL/Django later is straightforward — see §1.1. |
| 2 | **Administrator creation restricted to the Founding Administrator** | Originally any Administrator could create another Administrator. Changed so only the first-seeded ("Founding") Admin account can create additional Admins, to keep a single accountable owner for the system's highest-privilege role and reduce blast radius if a regular admin account is compromised. |
| 3 | **Resident AI Assistant added** (new — not in v1.0) | A floating, draggable chat widget (resident-facing) that looks up request/complaint status directly from the database and answers common FAQs, with an optional upgrade path to Claude for open-ended questions. Not in the original scope; added on request. |
| 4 | **Emergency Hotlines page added** (new — not in v1.0) | A static, informational reference page (911, PNP, BFP, Red Cross, etc.) on the resident side. Explicitly informational only — does not conflict with the v1.0 "no emergency hotline/dispatch" limitation, which refers to live dispatch/routing, not a static contact list. |
| 5 | **Document Request form expanded** | Added fields that reflect real barangay intake requirements: contact number, house no./street/purok, number of copies, preferred claiming date, a **required** Valid ID upload (separate from optional supporting documents), and an optional "claiming via representative" section (representative name, ID type, required authorization letter upload). |
| 6 | **Request status flow: unchanged** | Considered renaming statuses to match a more granular real-world flow (Pending/Under Review/For Verification/Ready for Pickup/Released/Rejected), but kept the original `Submitted → Under Review → Processing → Approved/Rejected → Completed` flow so residents' existing tracking expectations and all current data/reports stay consistent. |
| 7 | **Payment processing: confirmed out of scope** | No online payment gateway is implemented, per the v1.0 "no online payment processing" limitation. Any applicable fees are paid in person when claiming the document — the system tracks status only, not payment. |

---

## 1. Project Meta & System Architecture

- **Project Name:** BarangayConnect — Web-Based Citizen Request and Complaint Management System
- **Target Audience:** Residents (Citizens), Barangay Staff, Barangay Administrator
- **Design Paradigm:** Mobile-first, clean, simple, accessible layout suited for non-technical residents and barangay personnel. Navy/cyan civic design system, 15px rounded corners, shared sidebar+topbar shell across all three dashboards.

### 1.1 Tech Stack

| Layer | v1.0 Target | v2.0 As-Built | Migration Note |
|-------|-------------|----------------|-----------------|
| Frontend | HTML, CSS, JavaScript | HTML, CSS, JavaScript (server-rendered via Jinja2) | No change needed |
| Backend | Django | **Flask** | Routes/permission checks/business logic map 1:1 to Django views; porting is mechanical, not a rewrite |
| Database | MySQL | **SQLite** | Schema is plain SQL (`schema.sql`), not ORM-bound. Swap the `sqlite3` connector in `db.py` for `PyMySQL`/`mysql-connector-python`, adjust the handful of SQLite-specific functions (`datetime('now')`, `AUTOINCREMENT`) |
| Server | Apache | Flask dev server (for local/demo use) | For production, run behind gunicorn/uwsgi + Apache/Nginx as originally specified |
| Version Control | GitHub | GitHub | No change |

- **Scope:** Single-barangay deployment covering online submission of requests/complaints, request tracking, complaint management, resident record management, admin-controlled staff provisioning, reporting, administration, a resident-facing AI assistant, and an emergency hotlines reference page.
- **Limitations (confirmed, unchanged from v1.0):** Single barangay only; requires internet connection; **no online payment processing**; no integration with national government databases; **no emergency hotline/dispatch** (a *static informational* hotline list is in scope — see §3.9 — but live dispatch/routing is not); SMS notifications optional and dependent on third-party services. SMTP configuration is required at deployment for registration verification and password-reset delivery.

---

## 2. System Actors & Role-Based Access Control (RBAC)

- **Resident (Citizen):** Registers via a public, self-service sign-up form. Can log in, submit document requests, file complaints, upload supporting files, track request status, receive notifications, use the AI Assistant, view the Emergency Hotlines page, and view request history.
- **Barangay Staff:** Cannot self-register. Account is created only by the Administrator from the Admin Dashboard → Staff Management page. Logs in with Admin-issued credentials, reviews and processes requests, verifies information, updates request status, handles complaints, views resident information, and prints reports. Each staff account belongs to exactly one **office**, which decides what it can access: **Barangay Treasurer** (Barangay Clearance and its fees), **Barangay Secretary** (Certificate of Indigency and Certificate of Residency), or **BPSO** (complaints). A staff member cannot view or update another office's requests or complaints (HTTP 403); staff with no office assigned have no access until an Administrator assigns one.
- **Barangay Administrator:** Full access to user management, staff management, dashboard analytics, complaint management, request management, certificate management, reports, system settings, audit logs, and backup management.
  - **Founding Administrator (v2.0 addition):** the first-seeded admin account, flagged `is_super_admin`. **Only this account may create additional Administrator accounts.** All other Administrators retain every other admin privilege (staff/resident/request/complaint management, reports, audit logs) but the "Create Administrator" action is hidden from their UI and rejected (HTTP 403) if attempted directly.

- **Account Creation Matrix**
  - Resident — public self-registration form — created by anyone (the resident)
  - Staff — created from Admin Dashboard — created by any Administrator
  - Administrator — seeded on first setup; additional admins created in-app — **created by the Founding Administrator only** *(changed from v1.0, where any existing Administrator could create another)*

---

## 3. Core Functional Specifications

### 3.1 Resident Registration & Login (AUTH-01)
*(unchanged from v1.0)*
- Public sign-up form and centered login card; "Forgot Password" link on the login screen.
- Client-side validation for email format and password strength (min. 8 characters, at least one letter and one digit).
- `POST /register` — creates a Resident account after validating unique email.
- `POST /login` — validates credentials, checks role, issues a session.
- `POST /forgot-password` — sends a time-limited, single-use password-reset link by SMTP to the registered address of an active resident, staff, or administrator account. The token is stored as a SHA-256 digest.
- Passwords hashed (PBKDF2 via Werkzeug) before storage; account lockout for 15 minutes after 5 failed login attempts.

### 3.2 Admin & Staff Account Provisioning (PROV-02)
- Admin Dashboard → Staff Management page with a "Create Staff" form (full name, email, **office: Treasurer / Secretary / BPSO**).
- Settings → Manage Administrators page — **visible to all Admins, but the creation form and its endpoint are restricted to the Founding Administrator only** *(v2.0 change)*. Non-founding Admins see an explanatory message instead of the form.
- No public "Register as Staff" or "Register as Admin" route exists anywhere in the UI.
- `POST /admin/staff` — Admin-only; creates a Staff account and auto-generates a temporary password.
- `POST /admin/staff/<id>/toggle` — activates/deactivates a staff account.
- `POST /admin/staff/<id>/reset-password` — Admin resets a staff member's password directly.
- `POST /admin/administrators` — **Founding-Administrator-only** *(v2.0 change)*; creates an additional Administrator account with a required recovery email.
- New staff must set a permanent password on first login. Normal login for all roles uses email and password without OTP. Resident self-registration requires a time-limited, one-time email verification code before login.

### 3.3 Document Request Submission (REQ-03) — *expanded in v2.0*
- **Document type:** Barangay Clearance, Certificate of Residency, Business Clearance, Certificate of Indigency, Other.
  - *As built:* the offered types are Certificate of Indigency, Certificate of Residency and Barangay Clearance (Business is a Barangay Clearance purpose).
  - **Certificate of Residency fee flow:** ₱50, free for registered voters. The resident pays first at the Barangay Treasurer Office and keeps the official receipt, then claims the certificate at the Barangay Secretary Office and shows it. The system only tells the resident where to pay and claim; it does not track payment (see Limitations).
- **Form fields (v2.0):**
  - Purpose of request
  - Contact number *(new)*
  - House No. / Street / Purok *(new — pre-filled from the resident's profile address when available)*
  - Number of copies *(new, 1–20)*
  - Preferred claiming date *(new, optional)*
  - Additional remarks (optional)
  - **Valid ID upload — required** *(new; separated from general supporting-document attachments)*
  - Other supporting documents (optional, up to 3 files)
  - **"Someone else will claim this on my behalf" section** *(new, optional)*: representative's full name, representative's valid ID type, and a **required** authorization letter upload when this section is used.
- All uploads validated by extension **and** file signature/magic bytes (JPG/PNG/PDF only, 5MB max per file) — a renamed `.exe` is rejected even if it claims a `.pdf` extension.
- `POST /requests` (server-rendered equivalent: `/requests/new`) — validates payload and saves the request; rejects submission if the required Valid ID is missing, or if a representative is indicated without a name and authorization letter.
- `POST /requests/upload` equivalent — securely saves attachments, tagged by type (`valid_id`, `authorization_letter`, `supporting_document`) for clear display on the request detail page.
- `GET /requests/{id}` — fetches request details and current status for the owning Resident (or Staff/Admin for processing).
- **State Management (unchanged from v1.0):** `Submitted → Under Review → Processing → Approved/Rejected → Completed`; each update records date, time, assigned staff, and remarks. *(Considered renaming to Pending/Under Review/For Verification/Ready for Pickup/Released/Rejected — kept the original flow so resident-facing tracking stays consistent — see Changelog #6.)*

### 3.4 Complaint Filing & Management (COMP-04)
*(unchanged from v1.0)*
- Complaint form with category selector (Noise Complaint, Neighbor Dispute, Illegal Parking, Sanitation, Public Safety, Domestic Concern, Others) and attachment uploader.
- Status badge display for Pending, Under Review, Scheduled, Resolved, Closed, Rejected.
- `POST /complaints`, `PATCH /complaints/{id}/status` (Staff/Admin-only), `GET /complaints?status={status}`.

### 3.5 Dashboards (DASH-05)
*(unchanged from v1.0 — as implemented)*
- Resident Dashboard — active requests, pending complaints, notifications, history.
- Staff Dashboard — today's requests, pending approvals, complaint statistics, processing queue.
- Admin Dashboard — residents, requests, complaints, recent activity table, stat cards.
- All three share one sidebar+topbar+card/table component shell, per the original UI reference (dark navy sidebar, cyan accent for headings/active states/badges, 15px rounded corners, subtle hover scale on stat cards).

### 3.6 Notifications (NOTIF-09)
*(unchanged from v1.0)*
- In-app notification center for request submission, approval/rejection, complaint updates/resolution.
- `GET /notifications`, `POST /notifications/mark-read`.

### 3.7 Reports & Analytics (RPT-10)
*(unchanged from v1.0)*
- Daily, Weekly, Monthly, Yearly report generator — total requests, complaints, approval/resolution rates, new residents, staff activity.
- Export to PDF and Excel (.xlsx).

### 3.8 Search & Filtering (SRCH-11)
*(unchanged from v1.0)*
- Global search bar plus filter chips for status; searches by resident name, reference number, category, and status.

### 3.9 Resident AI Assistant *(new in v2.0 — not in original PRD)*
- **UI/UX:** A floating, draggable circular chat bubble present on every resident-facing page (not a single dedicated page), styled to match the design system. Position persists across page navigation within the same browser. Clicking (not dragging) opens a chat panel.
- **First-open experience:** shows a greeting plus tappable **quick-reply suggestion chips** (e.g. "How do I get a Barangay Clearance?", "How do I file a complaint?") that send the message on tap, similar to a Facebook Page inbox's default quick replies — no typing required to get started.
- **Answer sources, in priority order:**
  1. **Reference-number lookup** — if the message contains a request/complaint reference number (e.g. `REQ-2026-ABCDE`), the assistant queries the resident's own records directly and returns the real, current status — not a generated guess.
  2. **FAQ matching** — canned answers for common questions (document requirements, how to file a complaint, password recovery, hotline redirection).
  3. **Optional Claude API** — if an `ANTHROPIC_API_KEY` environment variable is configured (requires internet access at deploy time), open-ended questions that don't match the above are answered by Claude, grounded with a short system prompt describing BarangayConnect's services. This is fully optional; the assistant is useful with zero configuration.
- **Data:** Full conversation history is persisted per resident (`chat_messages` table) and reloaded on next visit.
- **Backend:** `GET /api/assistant/history`, `POST /api/assistant/message`.

### 3.10 Emergency Hotlines *(new in v2.0 — not in original PRD)*
- **UI/UX:** Static reference page, linked from the resident sidebar, listing national emergency numbers (911, PNP 117, BFP, Philippine Red Cross, NBI, DSWD, mental health crisis line) plus placeholder fields for the specific barangay's own contact numbers (barangay hall/tanod, RHU, VAWC desk) for the Administrator to fill in.
- **Explicitly informational only** — no click-to-dispatch, no call routing, no integration with any emergency response system. This keeps it inside the v1.0 "no emergency hotline/dispatch" limitation rather than violating it.

---

## 4. Non-Functional & Global Rules
*(unchanged from v1.0 unless noted)*

- **Performance:** Response time below 3 seconds; supports multiple concurrent users.
- **Authentication & Account Recovery:** Email verification (time-limited OTP with resend rate limits) required for new resident registration, but not normal login; password policy (min. 8 characters, letter + digit) enforced at registration and password-change for all roles; active accounts use a self-service, single-use emailed password reset link with a configurable expiry and request rate limits; rate limiting/account lockout after repeated failed attempts on any role.
- **Input & File Validation:** All form fields sanitized and validated server-side (parameterized SQL throughout — no string-built queries); file uploads restricted by type (JPG, PNG, PDF only) and maximum size (5MB); uploaded file content validated against its declared type via magic-byte inspection to block disguised malicious files.
- **Audit Logs & Accountability:** Audit logs are append-only (the application layer never issues `UPDATE`/`DELETE` against the audit table), including for Administrator actions; logs capture who performed an action, what was changed, and when — especially staff/admin account creation, edits, and status changes on requests/complaints.
- **Data Privacy:** Aligns with the Philippine Data Privacy Act (RA 10173); personal data access limited to what each role needs.
- **Reliability:** Error logging; database file can be backed up as a single file (SQLite) — encrypt at rest for production use.
- **Usability:** Responsive, mobile-friendly (sidebar collapses to a slide-out drawer under 900px), simple navigation, accessible forms.
- **Compatibility:** Chrome, Edge, Firefox, Safari.
- **Payment:** Confirmed out of scope. Any applicable government/processing fees are paid in person when the resident claims the physical document; the system does not track or process payments.
- **Success Metrics:** Residents submit requests successfully; complaints tracked and resolved efficiently; faster processing than a manual system; reduced paperwork and record loss; accurate automatic reports; easy-to-use and reliable system; zero unauthorized Staff/Admin account creations (verified via audit log); Founding-Administrator-only admin provisioning (verified via 403 responses in audit log for any bypass attempt).

---

## 5. Out of Scope (confirmed)

- Online payment processing of any kind.
- Emergency dispatch/live call routing (a static hotline reference page is in scope — §3.9).
- Integration with national government databases.
- Real SMS delivery (optional, third-party-dependent, not implemented in this build).
- Registration email verification and password-reset delivery require SMTP credentials and a public HTTPS application URL to be configured through environment variables.
