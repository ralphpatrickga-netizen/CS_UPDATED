"""
Access-control tests for the per-office staff model.

Run from the project folder:   python3 -m unittest tests.test_office_access -v

Uses a temporary database, so it never touches instance/barangayconnect.db.
"""
import contextlib
import io
import os
import re
import secrets
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import db as dbh  # noqa: E402

_tmpdir = tempfile.mkdtemp()
dbh.DB_PATH = os.path.join(_tmpdir, "test.db")

import app as A  # noqa: E402
import seed  # noqa: E402

STAFF_PW = "Staff@1234"
OFFICES = {  # office -> (email, request types it may handle, may handle complaints)
    "treasurer": ("marco.staff@barangayconnect.ph", {"Barangay Clearance"}, False),
    "secretary": ("ramon.staff@barangayconnect.ph", {"Certificate of Indigency", "Certificate of Residency"}, False),
    "bpso": ("liza.staff@barangayconnect.ph", set(), True),
}
ALL_TYPES = {"Certificate of Indigency", "Certificate of Residency", "Barangay Clearance"}


def login(client, email, password):
    conn = dbh.get_db()
    user = conn.execute("SELECT id, role FROM users WHERE email=?", (email,)).fetchone()
    conn.close()
    if user:
        with client.session_transaction() as session:
            session["user_id"] = user["id"]
            session["role"] = user["role"]
    return client.get("/")


def admin_client():
    c = A.app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = 1
        s["role"] = "admin"
    return c


class OfficeAccessTests(unittest.TestCase):
    UPLOAD_DIR = os.path.join(ROOT, "uploads", "requests")

    @classmethod
    def setUpClass(cls):
        os.makedirs(cls.UPLOAD_DIR, exist_ok=True)
        cls._files_before = set(os.listdir(cls.UPLOAD_DIR))
        with contextlib.redirect_stdout(io.StringIO()):
            with patch.dict(
                os.environ,
                {
                    "DEMO_ADMIN_PASSWORD": secrets.token_urlsafe(16) + "A1",
                    "DEMO_STAFF_PASSWORD": STAFF_PW,
                    "DEMO_RESIDENT_PASSWORD": "Resident@1234",
                },
            ):
                seed.run()
        conn = dbh.get_db()
        cls.req = {}   # request type -> a request row
        for t in ALL_TYPES:
            row = conn.execute("SELECT * FROM requests WHERE request_type=? LIMIT 1", (t,)).fetchone()
            assert row, f"seed has no {t} request"
            cls.req[t] = row
        cls.cmp = conn.execute("SELECT * FROM complaints LIMIT 1").fetchone()
        cls.cmp_file = conn.execute("SELECT stored_name FROM complaint_files LIMIT 1").fetchone()
        cls.staff_id = {o: conn.execute("SELECT id FROM users WHERE office=?", (o,)).fetchone()["id"] for o in OFFICES}
        conn.close()

    @classmethod
    def tearDownClass(cls):
        # remove any files the tests created in uploads/requests (leave pre-existing ones alone)
        for name in set(os.listdir(cls.UPLOAD_DIR)) - cls._files_before:
            os.remove(os.path.join(cls.UPLOAD_DIR, name))

    @staticmethod
    def _status(client, url):
        r = client.get(url)
        code = r.status_code
        r.close()
        return code

    def staff_client(self, office):
        c = A.app.test_client()
        r = login(c, OFFICES[office][0], STAFF_PW)
        self.assertEqual(r.status_code, 302)
        return c

    # ------------------------------------------------------------ requests
    def test_request_access_matrix(self):
        for office, (_, types, _c) in OFFICES.items():
            c = self.staff_client(office)
            for t in ALL_TYPES:
                rid = self.req[t]["id"]
                want = 200 if t in types else 403
                with self.subTest(office=office, type=t, action="view"):
                    self.assertEqual(c.get(f"/requests/{rid}").status_code, want)
                with self.subTest(office=office, type=t, action="update"):
                    r = c.post(f"/requests/{rid}/update", data={"status": "Under Review"})
                    self.assertEqual(r.status_code, 302 if t in types else 403)

    def test_request_queue_only_shows_own_types(self):
        for office, (_, types, _c) in OFFICES.items():
            c = self.staff_client(office)
            r = c.get("/staff/requests")
            if not types:
                self.assertEqual(r.status_code, 403, office)
                continue
            self.assertEqual(r.status_code, 200)
            html = r.get_data(as_text=True)
            for t in ALL_TYPES:
                refs = [x["reference_no"] for x in self._all_requests(t)]
                for ref in refs:
                    if t in types:
                        self.assertIn(ref, html, f"{office} should see {ref}")
                    else:
                        self.assertNotIn(ref, html, f"{office} must NOT see {ref} ({t})")

    def _all_requests(self, rtype):
        conn = dbh.get_db()
        rows = conn.execute("SELECT reference_no FROM requests WHERE request_type=?", (rtype,)).fetchall()
        conn.close()
        return rows

    # ----------------------------------------------------------- complaints
    def test_complaint_access(self):
        for office, (_, _t, can_c) in OFFICES.items():
            c = self.staff_client(office)
            cid = self.cmp["id"]
            self.assertEqual(c.get("/staff/complaints").status_code, 200 if can_c else 403, office)
            self.assertEqual(c.get(f"/complaints/{cid}").status_code, 200 if can_c else 403, office)
            r = c.post(f"/complaints/{cid}/update", data={"status": "Under Review"})
            self.assertEqual(r.status_code, 302 if can_c else 403, office)

    # -------------------------------------------------------------- uploads
    def test_orphan_and_complaint_uploads(self):
        for office, (_, _types, can_c) in OFFICES.items():
            c = self.staff_client(office)
            # a file that belongs to no request is never served to staff
            self.assertEqual(c.get("/uploads/requests/does-not-belong-to-anyone.jpg").status_code, 403, office)
            if self.cmp_file:
                r = c.get(f"/uploads/complaints/{self.cmp_file['stored_name']}")
                self.assertIn(r.status_code, (200, 404) if can_c else (403,), office)

    def test_upload_is_scoped_to_owning_request_type(self):
        """A file attached to a Barangay Clearance is not readable by the Secretary, and vice versa."""
        up = os.path.join(ROOT, "uploads", "requests")
        os.makedirs(up, exist_ok=True)
        made = []
        conn = dbh.get_db()
        try:
            for t, name in (("Barangay Clearance", "t_clearance.jpg"), ("Certificate of Residency", "t_residency.jpg")):
                with open(os.path.join(up, name), "wb") as fh:
                    fh.write(b"\xff\xd8\xff\xd9")
                made.append(os.path.join(up, name))
                conn.execute(
                    "INSERT INTO request_files (request_id, file_type, filename, stored_name) VALUES (?,?,?,?)",
                    (self.req[t]["id"], "supporting_document", name, name))
            conn.commit()
        finally:
            conn.close()
        try:
            treasurer, secretary = self.staff_client("treasurer"), self.staff_client("secretary")
            self.assertEqual(self._status(treasurer, "/uploads/requests/t_clearance.jpg"), 200)
            self.assertEqual(treasurer.get("/uploads/requests/t_residency.jpg").status_code, 403)
            self.assertEqual(self._status(secretary, "/uploads/requests/t_residency.jpg"), 200)
            self.assertEqual(secretary.get("/uploads/requests/t_clearance.jpg").status_code, 403)
        finally:
            conn = dbh.get_db()
            conn.execute("DELETE FROM request_files WHERE stored_name IN ('t_clearance.jpg','t_residency.jpg')")
            conn.commit()
            conn.close()
            for p in made:
                os.remove(p)

    # ------------------------------------------------ dashboards, API, search
    def test_dashboard_api_and_search_are_scoped(self):
        for office, (_, types, can_c) in OFFICES.items():
            c = self.staff_client(office)
            self.assertEqual(c.get("/staff/dashboard").status_code, 200)
            api = c.get("/api/dashboard/staff").get_json()
            self.assertEqual("pending_approvals" in api, bool(types), office)
            self.assertEqual("open_complaints" in api, can_c, office)
            html = c.get("/search?q=REQ").get_data(as_text=True)
            for t in ALL_TYPES - types:
                for row in self._all_requests(t):
                    self.assertNotIn(row["reference_no"], html, f"search leaked {t} to {office}")

    def test_search_bar_is_in_topbar_for_every_role(self):
        conn = dbh.get_db()
        users = conn.execute(
            "SELECT id, role FROM users WHERE role IN ('admin', 'staff', 'resident')"
        ).fetchall()
        conn.close()

        for user in users:
            client = A.app.test_client()
            with client.session_transaction() as session:
                session["user_id"] = user["id"]
                session["role"] = user["role"]

            response = client.get("/search")
            html = response.get_data(as_text=True)
            self.assertEqual(response.status_code, 200, user["role"])
            self.assertIn('class="topbar-search"', html, user["role"])
            self.assertNotIn('<h1 class="page-title content-page-title">Search</h1>', html, user["role"])
            self.assertNotIn('class="search-bar"', html, user["role"])

    def test_navigation_only_lists_own_area(self):
        for office, (_, types, can_c) in OFFICES.items():
            html = self.staff_client(office).get("/staff/dashboard").get_data(as_text=True)
            self.assertEqual("/staff/requests" in html, bool(types), office)
            self.assertEqual("/staff/complaints" in html, can_c, office)
            if office == "treasurer":
                self.assertNotIn("Office: <strong>Barangay Treasurer</strong> — handles Barangay Clearance", html)
            elif office == "secretary":
                self.assertIn("Office: <strong>Barangay Secretary</strong> — handles Certificate of Indigency", html)
            else:
                self.assertIn("Office: <strong>BPSO (Public Safety)</strong> — handles complaints", html)

    def test_navigation_badges_count_new_items_with_office_scope(self):
        conn = dbh.get_db()
        resident_id = conn.execute("SELECT id FROM users WHERE role='resident' LIMIT 1").fetchone()["id"]
        before_requests = {
            office: conn.execute(
                "SELECT COUNT(*) FROM requests WHERE status='Submitted' AND request_type IN ({})".format(
                    ",".join("?" for _ in types)
                ),
                tuple(types),
            ).fetchone()[0] if types else 0
            for office, (_, types, _) in OFFICES.items()
        }
        before_requests["admin"] = conn.execute(
            "SELECT COUNT(*) FROM requests WHERE status='Submitted'"
        ).fetchone()[0]
        before_complaints = conn.execute(
            "SELECT COUNT(*) FROM complaints WHERE status='Pending'"
        ).fetchone()[0]

        request_ids, complaint_ids = [], []
        try:
            for index, request_type in enumerate(sorted(ALL_TYPES)):
                cursor = conn.execute(
                    "INSERT INTO requests (reference_no, resident_id, request_type, status) VALUES (?,?,?,'Submitted')",
                    (f"TEST-NAV-COUNT-REQ-{index}", resident_id, request_type),
                )
                request_ids.append(cursor.lastrowid)
                cursor = conn.execute(
                    "INSERT INTO requests (reference_no, resident_id, request_type, status) VALUES (?,?,?,'Processing')",
                    (f"TEST-NAV-COUNT-PROC-{index}", resident_id, request_type),
                )
                request_ids.append(cursor.lastrowid)
            cursor = conn.execute(
                "INSERT INTO complaints (reference_no, resident_id, category, description, status) "
                "VALUES ('TEST-NAV-COUNT-CMP', ?, 'Others', 'Badge count test', 'Pending')",
                (resident_id,),
            )
            complaint_ids.append(cursor.lastrowid)
            cursor = conn.execute(
                "INSERT INTO complaints (reference_no, resident_id, category, description, status) "
                "VALUES ('TEST-NAV-COUNT-REVIEW', ?, 'Others', 'Badge count test', 'Under Review')",
                (resident_id,),
            )
            complaint_ids.append(cursor.lastrowid)
            conn.commit()
        finally:
            conn.close()

        def badge_count(html, endpoint):
            link = re.search(rf'<a href="{endpoint}"[^>]*>(.*?)</a>', html, re.DOTALL)
            if not link:
                return None
            badge = re.search(r'class="pill-badge"[^>]*>(\d+)</span>', link.group(1))
            return int(badge.group(1)) if badge else 0

        try:
            for office, (_, types, can_c) in OFFICES.items():
                html = self.staff_client(office).get("/staff/dashboard").get_data(as_text=True)
                expected_requests = before_requests[office] + len(types)
                expected_complaints = before_complaints + 1 if can_c else None
                self.assertEqual(
                    badge_count(html, "/staff/requests") if types else None,
                    expected_requests if types else None,
                    office,
                )
                self.assertEqual(badge_count(html, "/staff/complaints"), expected_complaints, office)

            html = admin_client().get("/admin/dashboard").get_data(as_text=True)
            self.assertEqual(badge_count(html, "/admin/requests"), before_requests["admin"] + len(ALL_TYPES))
            self.assertEqual(badge_count(html, "/admin/complaints"), before_complaints + 1)
        finally:
            conn = dbh.get_db()
            conn.executemany("DELETE FROM requests WHERE id=?", [(rid,) for rid in request_ids])
            conn.executemany("DELETE FROM complaints WHERE id=?", [(cid,) for cid in complaint_ids])
            conn.commit()
            conn.close()

    # ------------------------------------------------------------- assignment
    def test_assignee_must_belong_to_the_right_office(self):
        sec = self.staff_client("secretary")
        rid = self.req["Certificate of Residency"]["id"]
        bad = sec.post(f"/requests/{rid}/update", data={"status": "Processing", "assigned_staff_id": self.staff_id["bpso"]})
        self.assertEqual(bad.status_code, 302)
        conn = dbh.get_db()
        assigned = conn.execute("SELECT assigned_staff_id FROM requests WHERE id=?", (rid,)).fetchone()[0]
        conn.close()
        self.assertNotEqual(assigned, self.staff_id["bpso"])
        ok = sec.post(f"/requests/{rid}/update", data={"status": "Processing", "assigned_staff_id": self.staff_id["secretary"]})
        self.assertEqual(ok.status_code, 302)
        conn = dbh.get_db()
        assigned = conn.execute("SELECT assigned_staff_id FROM requests WHERE id=?", (rid,)).fetchone()[0]
        conn.close()
        self.assertEqual(assigned, self.staff_id["secretary"])

    # ---------------------------------------------------------- fail closed
    def test_staff_without_office_can_access_nothing(self):
        conn = dbh.get_db()
        conn.execute("UPDATE users SET office=NULL WHERE email=?", (OFFICES["secretary"][0],))
        conn.commit()
        conn.close()
        try:
            c = self.staff_client("secretary")
            self.assertEqual(c.get("/staff/dashboard").status_code, 200)   # shows "no office assigned"
            self.assertEqual(c.get("/staff/requests").status_code, 403)
            self.assertEqual(c.get("/staff/complaints").status_code, 403)
            for t in ALL_TYPES:
                self.assertEqual(c.get(f"/requests/{self.req[t]['id']}").status_code, 403)
            self.assertEqual(c.get(f"/complaints/{self.cmp['id']}").status_code, 403)
        finally:
            conn = dbh.get_db()
            conn.execute("UPDATE users SET office='secretary' WHERE email=?", (OFFICES["secretary"][0],))
            conn.commit()
            conn.close()

    # ----------------------------------------------------- admin & residents
    def test_admin_keeps_full_access(self):
        c = admin_client()
        for t in ALL_TYPES:
            self.assertEqual(c.get(f"/requests/{self.req[t]['id']}").status_code, 200)
        self.assertEqual(c.get(f"/complaints/{self.cmp['id']}").status_code, 200)
        self.assertEqual(c.get("/admin/dashboard").status_code, 200)

    def test_resident_cannot_use_staff_routes(self):
        c = A.app.test_client()
        login(c, "juan.resident@example.com", "Resident@1234")
        for url in ("/staff/dashboard", "/staff/requests", "/staff/complaints", "/admin/staff"):
            self.assertEqual(c.get(url).status_code, 403, url)

    # ------------------------------------------------------ admin staff form
    def test_admin_staff_creation_requires_valid_office(self):
        c = admin_client()
        html = c.get("/admin/staff").get_data(as_text=True)
        self.assertIn('name="office"', html)
        self.assertIn("Office Coverage", html)

        def post(**data):
            c.post("/admin/staff", data=data)
            conn = dbh.get_db()
            row = conn.execute("SELECT office, position FROM users WHERE email=?", (data["email"],)).fetchone()
            conn.close()
            return row

        self.assertIsNone(post(full_name="No Office", email="nooffice@x.ph"))
        self.assertIsNone(post(full_name="Bad Office", email="bad@x.ph", office="janitor"))
        row = post(
            full_name="New Treasurer Aide", email="aide@x.ph",
            office="treasurer", contact_number="0917 123 0000",
        )
        self.assertEqual(row["office"], "treasurer")
        self.assertEqual(row["position"], "Barangay Treasurer")

    def test_admin_can_reassign_office(self):
        c = admin_client()
        conn = dbh.get_db()
        c.post("/admin/staff", data={
            "full_name": "Move Me", "email": "moveme@x.ph",
            "office": "treasurer", "contact_number": "0917 123 0000",
        })
        sid = conn.execute("SELECT id FROM users WHERE email='moveme@x.ph'").fetchone()["id"]
        conn.close()
        c.post(f"/admin/staff/{sid}/office", data={"office": "bpso"})
        conn = dbh.get_db()
        self.assertEqual(conn.execute("SELECT office FROM users WHERE id=?", (sid,)).fetchone()[0], "bpso")
        conn.close()
        c.post(f"/admin/staff/{sid}/office", data={"office": "nonsense"})
        conn = dbh.get_db()
        self.assertEqual(conn.execute("SELECT office FROM users WHERE id=?", (sid,)).fetchone()[0], "bpso")
        conn.close()

    def test_staff_can_update_resident_mobile_for_mfa(self):
        conn = dbh.get_db()
        resident = conn.execute(
            "SELECT id, contact_number FROM users WHERE role='resident' LIMIT 1"
        ).fetchone()
        conn.close()
        client = self.staff_client("secretary")
        response = client.post(
            f"/admin/users/{resident['id']}/mobile",
            data={"contact_number": "0917 123 4567"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, "/staff/residents")
        conn = dbh.get_db()
        updated = conn.execute(
            "SELECT contact_number FROM users WHERE id=?", (resident["id"],)
        ).fetchone()["contact_number"]
        conn.execute(
            "UPDATE users SET contact_number=? WHERE id=?",
            (resident["contact_number"], resident["id"]),
        )
        conn.commit()
        conn.close()
        self.assertEqual(updated, "+639171234567")

    # ------------------------------------------------------ Certificate of Residency
    def _submit_residency(self, voter):
        c = A.app.test_client()
        login(c, "maria.resident@example.com", "Resident@1234")
        data = {
            "request_type": "Certificate of Residency", "purpose": "Employment", "civil_status": "Single",
            "age": "30", "contact_number": "0917 000 0000", "request_address": "Purok 1",
            "valid_id": (io.BytesIO(b"%PDF-1.4 test"), "id.pdf"),
        }
        if voter:
            data["registered_voter"] = "on"
        before = self._notif_counts()
        r = c.post("/requests/new", data=data, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 302, r.get_data(as_text=True)[:400])
        conn = dbh.get_db()
        row = conn.execute("SELECT * FROM requests WHERE request_type='Certificate of Residency' ORDER BY id DESC LIMIT 1").fetchone()
        conn.close()
        return row, before

    def _notif_counts(self):
        conn = dbh.get_db()
        out = {o: conn.execute("SELECT COUNT(*) FROM notifications WHERE user_id=?", (self.staff_id[o],)).fetchone()[0] for o in OFFICES}
        conn.close()
        return out

    def test_residency_fee_and_routing(self):
        row, before = self._submit_residency(voter=False)
        self.assertEqual(row["computed_fee"], 50)
        self.assertEqual(row["registered_voter"], 0)
        after = self._notif_counts()
        self.assertEqual(after["secretary"], before["secretary"] + 1)
        self.assertEqual(after["treasurer"], before["treasurer"])
        self.assertEqual(after["bpso"], before["bpso"])

        conn = dbh.get_db()
        conn.execute("UPDATE requests SET status='Completed' WHERE id=?", (row["id"],))
        conn.commit()
        conn.close()

        row, _ = self._submit_residency(voter=True)
        self.assertEqual(row["computed_fee"], 0)
        self.assertEqual(row["registered_voter"], 1)

    def test_residency_requires_a_purpose(self):
        c = A.app.test_client()
        login(c, "maria.resident@example.com", "Resident@1234")
        r = c.post("/requests/new", data={
            "request_type": "Certificate of Residency", "purpose": "", "civil_status": "Single", "age": "30",
            "contact_number": "0917", "request_address": "Purok 1",
            "valid_id": (io.BytesIO(b"%PDF-1.4"), "id.pdf")}, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200)  # re-rendered with an error
        self.assertIn("purpose of your Certificate of Residency", r.get_data(as_text=True))

    def test_residency_is_no_longer_a_clearance_purpose(self):
        self.assertFalse([p for p, _ in A.BARANGAY_CLEARANCE_PURPOSES if "Residency" in p])
        self.assertIn("Certificate of Residency", A.REQUEST_TYPES)

    def test_residency_detail_tells_where_to_pay(self):
        row = self.req["Certificate of Residency"]
        html = self.staff_client("secretary").get(f"/requests/{row['id']}").get_data(as_text=True)
        self.assertIn("Barangay Treasurer Office", html)

    def test_clearance_is_handled_only_by_treasurer_config(self):
        self.assertEqual(A.offices_for_request_type("Barangay Clearance"), ["treasurer"])
        self.assertEqual(A.offices_for_request_type("Certificate of Residency"), ["secretary"])
        self.assertEqual(A.offices_for_request_type("Certificate of Indigency"), ["secretary"])
        self.assertEqual(A.offices_for_complaints(), ["bpso"])
        covered = {t for cfg in A.OFFICES.values() for t in cfg["requests"]}
        self.assertEqual(covered, set(A.REQUEST_TYPES), "every request type must be owned by an office")

    def test_urgent_complaint_alerts_go_to_bpso_and_admin_only(self):
        c = A.app.test_client()
        login(c, "juan.resident@example.com", "Resident@1234")
        before = self._notif_counts()
        r = c.post("/complaints/new", data={
            "category": "Public Safety", "description": "There is a fire near our house, please help",
            "location": "Purok 2"})
        self.assertEqual(r.status_code, 302)
        after = self._notif_counts()
        self.assertEqual(after["bpso"], before["bpso"] + 1)
        self.assertEqual(after["treasurer"], before["treasurer"])
        self.assertEqual(after["secretary"], before["secretary"])

    def test_active_duplicate_request_and_concern_are_blocked_until_resolved(self):
        conn = dbh.get_db()
        email = f"{dbh.gen_reference('TST').lower()}@example.com"
        cursor = conn.execute(
            "INSERT INTO users (full_name, email, password_hash, role) VALUES (?,?,?,'resident')",
            ("Duplicate Test Resident", email, "test-password-hash"),
        )
        resident_id = cursor.lastrowid
        conn.commit()
        conn.close()

        client = A.app.test_client()
        with client.session_transaction() as session:
            session["user_id"] = resident_id
            session["role"] = "resident"

        def submit_request(request_type="Certificate of Residency", purpose="Employment"):
            return client.post(
                "/requests/new",
                data={
                    "request_type": request_type,
                    "purpose": purpose,
                    "civil_status": "Single",
                    "age": "30",
                    "contact_number": "09170000000",
                    "request_address": "Purok 1",
                    "valid_id": (io.BytesIO(b"%PDF-1.4 test"), "valid-id.pdf"),
                },
                content_type="multipart/form-data",
                follow_redirects=True,
            )

        def submit_concern():
            return client.post(
                "/complaints/new",
                data={
                    "category": "Noise Complaint",
                    "description": "Repeated loud music at night.",
                    "location": "Purok 77, Test Street",
                },
                follow_redirects=True,
            )

        try:
            first_request = submit_request()
            self.assertIn("Request submitted. Reference number:", first_request.get_data(as_text=True))
            request_row = self._resident_row("requests", resident_id)
            self.assertIsNotNone(request_row)

            for status in ("Submitted", "Under Review", "Processing", "Approved"):
                conn = dbh.get_db()
                conn.execute("UPDATE requests SET status=? WHERE id=?", (status, request_row["id"]))
                conn.commit()
                conn.close()

                duplicate_request = submit_request().get_data(as_text=True)
                self.assertIn('<dialog class="concern-success-dialog" id="duplicate-request-dialog"', duplicate_request)
                self.assertIn("<h2 id=\"duplicate-request-title\">Existing Document Request</h2>", duplicate_request)
                self.assertIn("You already have an active request for this document with the same purpose.", duplicate_request)
                self.assertIn("Please wait until your current request is completed before submitting another one.", duplicate_request)
                self.assertIn("Request In Progress", duplicate_request)
                self.assertIn('href="/requests">Go to My Request</a>', duplicate_request)
                self.assertIn('data-redirect="/requests"', duplicate_request)
                self.assertEqual(self._resident_count("requests", resident_id), 1, status)

            conn = dbh.get_db()
            conn.execute("UPDATE requests SET status='Completed' WHERE id=?", (request_row["id"],))
            conn.commit()
            conn.close()
            completed_request = submit_request().get_data(as_text=True)
            self.assertIn("Request submitted. Reference number:", completed_request)
            self.assertEqual(self._resident_count("requests", resident_id), 2)

            conn = dbh.get_db()
            latest_request = conn.execute(
                "SELECT id FROM requests WHERE resident_id=? ORDER BY id DESC LIMIT 1", (resident_id,)
            ).fetchone()
            conn.execute("UPDATE requests SET status='Completed' WHERE id=?", (latest_request["id"],))
            conn.commit()
            conn.close()
            completed_resubmission = submit_request().get_data(as_text=True)
            self.assertIn("Request submitted. Reference number:", completed_resubmission)
            self.assertEqual(self._resident_count("requests", resident_id), 3)

            conn = dbh.get_db()
            latest_request = conn.execute(
                "SELECT id FROM requests WHERE resident_id=? ORDER BY id DESC LIMIT 1", (resident_id,)
            ).fetchone()
            conn.execute("UPDATE requests SET status='Rejected' WHERE id=?", (latest_request["id"],))
            conn.commit()
            conn.close()
            rejected_resubmission = submit_request().get_data(as_text=True)
            self.assertIn("Request submitted. Reference number:", rejected_resubmission)
            self.assertEqual(self._resident_count("requests", resident_id), 4)

            other_purpose = submit_request(purpose="Education").get_data(as_text=True)
            self.assertIn("Request submitted. Reference number:", other_purpose)
            other_document = submit_request(
                request_type="Barangay Clearance", purpose="Bank Requirement"
            ).get_data(as_text=True)
            self.assertIn("Request submitted. Reference number:", other_document)
            self.assertEqual(self._resident_count("requests", resident_id), 6)

            first_concern = submit_concern().get_data(as_text=True)
            self.assertIn('<dialog class="concern-success-dialog" id="concern-success-dialog"', first_concern)
            self.assertIn('data-redirect="/complaints"', first_concern)
            concern_row = self._resident_row("complaints", resident_id)
            self.assertIsNotNone(concern_row)

            duplicate_concern = submit_concern().get_data(as_text=True)
            self.assertIn('<dialog class="concern-success-dialog" id="duplicate-concern-dialog"', duplicate_concern)
            self.assertIn("<h2 id=\"duplicate-concern-title\">Active Concern Found</h2>", duplicate_concern)
            self.assertIn(
                "You already have an active concern for this category and location.",
                duplicate_concern,
            )
            self.assertIn('href="/complaints">Go to My Concern</a>', duplicate_concern)
            self.assertIn('data-redirect="/complaints"', duplicate_concern)
            self.assertEqual(self._resident_count("complaints", resident_id), 1)

            conn = dbh.get_db()
            conn.execute("UPDATE complaints SET status='Resolved' WHERE id=?", (concern_row["id"],))
            conn.commit()
            conn.close()
            resolved_concern = submit_concern().get_data(as_text=True)
            self.assertIn('<dialog class="concern-success-dialog" id="concern-success-dialog"', resolved_concern)
            self.assertEqual(self._resident_count("complaints", resident_id), 2)
        finally:
            conn = dbh.get_db()
            requests_ = conn.execute("SELECT id FROM requests WHERE resident_id=?", (resident_id,)).fetchall()
            complaints_ = conn.execute("SELECT id FROM complaints WHERE resident_id=?", (resident_id,)).fetchall()
            request_ids = [row["id"] for row in requests_]
            complaint_ids = [row["id"] for row in complaints_]
            if request_ids:
                marks = ",".join("?" * len(request_ids))
                conn.execute(
                    f"DELETE FROM status_history WHERE entity_type='request' AND entity_id IN ({marks})",
                    request_ids,
                )
                conn.execute(f"DELETE FROM request_files WHERE request_id IN ({marks})", request_ids)
                conn.execute(f"DELETE FROM requests WHERE id IN ({marks})", request_ids)
            if complaint_ids:
                marks = ",".join("?" * len(complaint_ids))
                conn.execute(
                    f"DELETE FROM status_history WHERE entity_type='complaint' AND entity_id IN ({marks})",
                    complaint_ids,
                )
                conn.execute(f"DELETE FROM complaints WHERE id IN ({marks})", complaint_ids)
            conn.execute("DELETE FROM notifications WHERE user_id=?", (resident_id,))
            conn.execute("DELETE FROM audit_logs WHERE actor_id=?", (resident_id,))
            conn.execute("DELETE FROM users WHERE id=?", (resident_id,))
            conn.commit()
            conn.close()

    def _resident_row(self, table, resident_id):
        conn = dbh.get_db()
        row = conn.execute(f"SELECT * FROM {table} WHERE resident_id=? ORDER BY id LIMIT 1", (resident_id,)).fetchone()
        conn.close()
        return row

    def _resident_count(self, table, resident_id):
        conn = dbh.get_db()
        count = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE resident_id=?", (resident_id,)).fetchone()[0]
        conn.close()
        return count

    def test_resident_complaint_allowance_cooldown_block_and_reset(self):
        conn = dbh.get_db()
        email = f"{dbh.gen_reference('TST').lower()}@example.com"
        cursor = conn.execute(
            "INSERT INTO users (full_name, email, password_hash, role) VALUES (?,?,?,'resident')",
            ("Daily Limit Test Resident", email, "test-password-hash"),
        )
        resident_id = cursor.lastrowid
        conn.commit()
        conn.close()

        client = A.app.test_client()
        with client.session_transaction() as session:
            session["user_id"] = resident_id
            session["role"] = "resident"

        try:
            form = client.get("/complaints/new").get_data(as_text=True)
            self.assertIn("You have 5 of 5 concern submissions available.", form)
            self.assertNotIn("Cooldown ends in", form)
            self.assertRegex(
                form,
                r'File Concern<span class="pill-badge" aria-label="5 concerns available" '
                r'title="5 concerns available">5</span>',
            )

            for filed_count in range(1, A.COMPLAINT_DAILY_LIMIT + 1):
                response = client.post(
                    "/complaints/new",
                    data={
                        "category": "Noise Complaint",
                        "description": f"Daily limit test concern {filed_count}",
                        "location": f"Daily limit test location {filed_count}",
                    },
                    follow_redirects=True,
                )
                self.assertEqual(response.status_code, 200)
                remaining = A.COMPLAINT_DAILY_LIMIT - filed_count
                response_html = response.get_data(as_text=True)
                self.assertIn('<dialog class="concern-success-dialog"', response_html)
                self.assertRegex(response_html, r"Complaint filed\. Reference number: <strong>CMP-\d{4}-[A-Z0-9]{5}</strong>")
                self.assertIn(
                    f"You have <strong>{remaining}</strong> "
                    f"{'concern' if remaining == 1 else 'concerns'} available.",
                    response_html,
                )
                if filed_count < A.COMPLAINT_ALLOWANCE_MAX:
                    conn = dbh.get_db()
                    self.assertIsNone(conn.execute(
                        "SELECT cooldown_started_at FROM resident_concern_allowances WHERE user_id=?",
                        (resident_id,),
                    ).fetchone()["cooldown_started_at"])
                    conn.close()
                else:
                    self.assertIn("Your 24-hour cooldown has started.", response_html)
                self.assertIn(
                    'href="/complaints">Go to My Concern to Track the Status</a>',
                    response_html,
                )
                self.assertNotIn("na lang ang natitira ngayong araw.", response_html)
                self.assertIn(
                    f'aria-label="{remaining} concerns available"',
                    response_html,
                )

            blocked = client.get("/complaints/new").get_data(as_text=True)
            self.assertIn("You have used all 5 available concern submissions.", blocked)
            self.assertIn("A 24-hour cooldown is active.", blocked)
            self.assertRegex(blocked, r'Cooldown ends in <strong class="countdown-value">\d+h \d+m \d+s</strong>')
            self.assertRegex(blocked, r'class="concern-reset-countdown" role="timer" data-countdown-seconds="\d+"')
            self.assertIn('aria-label="0 concerns available"', blocked)
            self.assertNotIn('id="complaint-form"', blocked)

            blocked_post = client.post(
                "/complaints/new",
                data={
                    "category": "Noise Complaint",
                    "description": "This submission must be blocked.",
                    "location": "Blocked test location",
                    "confirm_submission": "1",
                    "action": "continue",
                },
            )
            self.assertEqual(blocked_post.status_code, 200)
            self.assertIn(
                "You have used all 5 available concern submissions.",
                blocked_post.get_data(as_text=True),
            )

            conn = dbh.get_db()
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM complaints WHERE resident_id=?",
                    (resident_id,),
                ).fetchone()[0],
                A.COMPLAINT_DAILY_LIMIT,
            )
            conn.execute(
                "UPDATE resident_concern_allowances SET cooldown_expires_at=? WHERE user_id=?",
                ("2000-01-01T00:00:00", resident_id),
            )
            conn.commit()
            conn.close()

            reset_form = client.get("/complaints/new").get_data(as_text=True)
            self.assertIn("You have 5 of 5 concern submissions available.", reset_form)
            self.assertIn('aria-label="5 concerns available"', reset_form)
            self.assertIn('id="complaint-form"', reset_form)
        finally:
            conn = dbh.get_db()
            complaint_ids = [
                row["id"] for row in conn.execute(
                    "SELECT id FROM complaints WHERE resident_id=?", (resident_id,)
                ).fetchall()
            ]
            if complaint_ids:
                marks = ",".join("?" * len(complaint_ids))
                conn.execute(
                    f"DELETE FROM status_history WHERE entity_type='complaint' AND entity_id IN ({marks})",
                    complaint_ids,
                )
                conn.execute(f"DELETE FROM complaints WHERE id IN ({marks})", complaint_ids)
            conn.execute("DELETE FROM notifications WHERE user_id=?", (resident_id,))
            conn.execute("DELETE FROM audit_logs WHERE actor_id=?", (resident_id,))
            conn.execute("DELETE FROM users WHERE id=?", (resident_id,))
            conn.commit()
            conn.close()


class MigrationTests(unittest.TestCase):
    def test_old_database_is_upgraded_and_staff_are_mapped(self):
        path = os.path.join(tempfile.mkdtemp(), "old.db")
        c = sqlite3.connect(path)
        c.executescript("""
            CREATE TABLE users (id INTEGER PRIMARY KEY, full_name TEXT, email TEXT, role TEXT,
                                position TEXT, category_scope TEXT);
            CREATE TABLE requests (id INTEGER PRIMARY KEY, request_type TEXT);
            INSERT INTO users VALUES (1,'A','a@x','staff','Records Officer','Barangay Clearance');
            INSERT INTO users VALUES (2,'B','b@x','staff','Complaints Desk Officer','Noise Complaint');
            INSERT INTO users VALUES (3,'C','c@x','staff','Clerk',NULL);
            INSERT INTO users VALUES (4,'D','d@x','staff','Records','Certificate of Indigency');
            INSERT INTO users VALUES (5,'E','e@x','resident',NULL,NULL);
        """)
        c.commit(); c.close()
        old_path, dbh.DB_PATH = dbh.DB_PATH, path
        try:
            dbh.migrate_db()
            dbh.migrate_db()  # idempotent
        finally:
            dbh.DB_PATH = old_path
        c = sqlite3.connect(path)
        offices = dict(c.execute("SELECT id, office FROM users").fetchall())
        user_cols = {r[1] for r in c.execute("PRAGMA table_info(users)")}
        otp_cols = {r[1] for r in c.execute("PRAGMA table_info(admin_otp_challenges)")}
        cols = [r[1] for r in c.execute("PRAGMA table_info(requests)")]
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        c.close()
        self.assertEqual(offices[1], "treasurer")
        self.assertEqual(offices[2], "bpso")
        self.assertIsNone(offices[3], "unmappable staff must stay office-less (no access)")
        self.assertEqual(offices[4], "secretary")
        self.assertIsNone(offices[5])
        self.assertIn("contact_number", user_cols)
        self.assertTrue({"email_otp_hash", "sms_otp_hash", "pending_phone"}.issubset(otp_cols))
        self.assertIn("registered_voter", cols)
        self.assertIn("password_reset_codes", tables)


if __name__ == "__main__":
    unittest.main()
