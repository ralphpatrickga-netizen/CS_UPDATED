"""
Tests for: database backup, bulk complaint actions, resident filters.

Run from the project folder:   python3 -m unittest tests.test_new_features -v
Uses its own temporary database (and backups folder), never instance/.
"""
import contextlib
import io
import os
import secrets
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import db as dbh  # noqa: E402
_boot_dir = tempfile.mkdtemp()
dbh.DB_PATH = os.path.join(_boot_dir, "boot.db")
import app as A  # noqa: E402
import seed  # noqa: E402

STAFF_PW = "Staff@1234"


def client_for(email, password):
    c = A.app.test_client()
    conn = dbh.get_db()
    user = conn.execute("SELECT id, role FROM users WHERE email=?", (email,)).fetchone()
    conn.close()
    if user:
        with c.session_transaction() as session:
            session["user_id"] = user["id"]
            session["role"] = user["role"]
    r = c.get("/")
    assert r.status_code == 302, f"login failed for {email}"
    return c


def admin_client():
    c = A.app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = 1
        s["role"] = "admin"
    return c


class FeatureTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._old_path = dbh.DB_PATH
        cls._dir = tempfile.mkdtemp()
        dbh.DB_PATH = os.path.join(cls._dir, "test.db")
        dbh._last_auto_check = 0.0
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

    @classmethod
    def tearDownClass(cls):
        dbh.DB_PATH = cls._old_path

    def q(self, sql, params=()):
        conn = dbh.get_db()
        rows = conn.execute(sql, params).fetchall()
        conn.close()
        return rows


class BackupTests(FeatureTestBase):
    def test_admin_can_download_a_valid_backup(self):
        r = admin_client().post("/admin/settings/backup")
        self.assertEqual(r.status_code, 200)
        self.assertIn("attachment", r.headers["Content-Disposition"])
        self.assertIn("barangayconnect-", r.headers["Content-Disposition"])
        path = os.path.join(self._dir, "dl.db")
        with open(path, "wb") as f:
            f.write(r.data)
        c = sqlite3.connect(path)
        self.assertEqual(c.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(c.execute("SELECT COUNT(*) FROM users").fetchone()[0],
                         self.q("SELECT COUNT(*) c FROM users")[0]["c"])
        c.close()
        self.assertTrue(self.q("SELECT 1 FROM audit_logs WHERE action='Downloaded database backup'"))

    def test_only_admin_may_back_up(self):
        staff = client_for("liza.staff@barangayconnect.ph", STAFF_PW)
        self.assertEqual(staff.post("/admin/settings/backup").status_code, 403)
        self.assertEqual(staff.get("/admin/settings").status_code, 403)
        anon = A.app.test_client()
        self.assertEqual(anon.post("/admin/settings/backup").status_code, 302)  # -> login

    def test_download_existing_and_reject_bad_names(self):
        a = admin_client()
        a.post("/admin/settings/backup")
        name = dbh.list_backups()[0]
        self.assertEqual(a.get(f"/admin/settings/backup/{name}").status_code, 200)
        for bad in ("nope.db", "..%2F..%2Fdb.py", "barangayconnect.db", "x"):
            self.assertEqual(a.get(f"/admin/settings/backup/{bad}").status_code, 404, bad)

    def test_settings_page_lists_backups(self):
        a = admin_client()
        a.post("/admin/settings/backup")
        html = a.get("/admin/settings").get_data(as_text=True)
        self.assertIn("Database Backup", html)
        self.assertIn("Back up now &amp; download", html)
        self.assertNotIn("A backup is made automatically", html)
        self.assertNotIn("Keep downloaded copies somewhere safe", html)
        self.assertNotIn("Note: this saves the database only", html)
        self.assertNotIn("need to be copied separately", html)
        for heading in ("Backup", "Created", "Size", "Download"):
            self.assertIn(heading, html)
        self.assertIn(dbh.list_backups()[0], html)

    def test_retention_keeps_only_newest(self):
        for _ in range(5):
            dbh.backup_database(keep=3)
        self.assertEqual(len(dbh.list_backups()), 3)

    def test_automatic_backup_is_daily_and_throttled(self):
        for n in dbh.list_backups():
            os.remove(os.path.join(dbh.backup_dir(), n))
        dbh._last_auto_check = 0.0
        self.assertIsNotNone(dbh.auto_backup_if_due(now_ts=1_000_000.0))   # none exist -> backs up
        self.assertEqual(len(dbh.list_backups()), 1)
        self.assertIsNone(dbh.auto_backup_if_due(now_ts=1_000_100.0))      # checked <1h ago
        # an hour later but newest backup is fresh (real mtime ~ now) -> still no new backup
        import time
        self.assertIsNone(dbh.auto_backup_if_due(now_ts=time.time() + 7200))
        self.assertEqual(len(dbh.list_backups()), 1)
        # >24h later -> due again
        self.assertIsNotNone(dbh.auto_backup_if_due(now_ts=time.time() + 25 * 3600 + 7200))


class ProfilePictureTests(FeatureTestBase):
    def test_profile_page_has_custom_picker_preview_and_admin_logout(self):
        html = admin_client().get("/profile").get_data(as_text=True)
        self.assertIn("Upload a new profile picture", html)
        self.assertIn("Choose File", html)
        self.assertIn("JPG or PNG only (max 20 MB)", html)
        self.assertIn('data-profile-preview="profile-pic-preview"', html)
        sidebar = html.split('<aside class="sidebar">', 1)[1].split("</aside>", 1)[0]
        self.assertIn('<a href="/logout" class="logout-link">Logout</a>', sidebar)
        self.assertRegex(
            sidebar,
            r'<a href="/admin/complaints"[^>]*>Concerns(?:<span class="pill-badge"[^>]*>\d+</span>)?</a>',
        )
        self.assertNotIn(">Complaints</a>", sidebar)
        self.assertNotIn("logout-mini", html)
        self.assertEqual(A.app.config["MAX_CONTENT_LENGTH"], A.MAX_FILE_SIZE + 1024)

    def test_valid_20_mb_profile_picture_is_saved_and_displayed(self):
        image = b"\xff\xd8\xff" + b"\x00" * (A.PROFILE_IMAGE_MAX_SIZE - 3)
        with tempfile.TemporaryDirectory() as upload_dir, patch.object(A, "UPLOAD_DIR", upload_dir):
            client = admin_client()
            response = client.post(
                "/profile",
                data={"profile_picture": (io.BytesIO(image), "profile.jpg")},
                content_type="multipart/form-data",
            )
            self.assertEqual(response.status_code, 302)
            stored_name = self.q("SELECT profile_picture FROM users WHERE id=1")[0]["profile_picture"]
            saved_path = os.path.join(upload_dir, "profiles", stored_name)
            self.assertTrue(os.path.isfile(saved_path))
            self.assertEqual(os.path.getsize(saved_path), A.PROFILE_IMAGE_MAX_SIZE)

            profile_html = client.get("/profile").get_data(as_text=True)
            self.assertIn(stored_name, profile_html)
            self.assertIn("Profile picture preview", profile_html)

    def test_oversized_and_unsupported_profile_pictures_are_rejected(self):
        with tempfile.TemporaryDirectory() as upload_dir, patch.object(A, "UPLOAD_DIR", upload_dir):
            client = admin_client()
            before = self.q("SELECT profile_picture FROM users WHERE id=1")[0]["profile_picture"]

            oversized = b"\xff\xd8\xff" + b"\x00" * A.PROFILE_IMAGE_MAX_SIZE
            response = client.post(
                "/profile",
                data={"profile_picture": (io.BytesIO(oversized), "too-large.jpg")},
                content_type="multipart/form-data",
                follow_redirects=True,
            )
            self.assertIn("File must be no larger than 20 MB.", response.get_data(as_text=True))
            self.assertEqual(self.q("SELECT profile_picture FROM users WHERE id=1")[0]["profile_picture"], before)

            beyond_request_limit = b"\xff\xd8\xff" + b"\x00" * (
                A.PROFILE_IMAGE_MAX_SIZE + 1024 * 1024
            )
            response = client.post(
                "/profile",
                data={"profile_picture": (io.BytesIO(beyond_request_limit), "over-request-limit.jpg")},
                content_type="multipart/form-data",
                follow_redirects=True,
            )
            self.assertIn("Profile picture must be 20 MB or smaller.", response.get_data(as_text=True))
            self.assertEqual(self.q("SELECT profile_picture FROM users WHERE id=1")[0]["profile_picture"], before)

            response = client.post(
                "/profile",
                data={"profile_picture": (io.BytesIO(b"GIF89a"), "unsupported.gif")},
                content_type="multipart/form-data",
                follow_redirects=True,
            )
            self.assertIn("Only JPG and PNG images are allowed.", response.get_data(as_text=True))
            self.assertEqual(self.q("SELECT profile_picture FROM users WHERE id=1")[0]["profile_picture"], before)

            response = client.post(
                "/profile",
                data={"profile_picture": (io.BytesIO(b"\x89PNG\r\n\x1a\n"), "mismatch.jpg")},
                content_type="multipart/form-data",
                follow_redirects=True,
            )
            self.assertIn("File content does not match its declared type.", response.get_data(as_text=True))
            self.assertEqual(self.q("SELECT profile_picture FROM users WHERE id=1")[0]["profile_picture"], before)
            self.assertEqual(os.listdir(upload_dir), [])

    def test_admin_logout_clears_session_and_redirects_to_landing_page(self):
        client = admin_client()
        response = client.get("/logout")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/")
        with client.session_transaction() as session:
            self.assertNotIn("user_id", session)
            self.assertNotIn("role", session)
        self.assertIn("You have been logged out.", client.get("/").get_data(as_text=True))
        self.assertEqual(client.get("/admin/dashboard").status_code, 302)

    def test_landing_page_has_concerns_link_without_urgent_demo(self):
        html = A.app.test_client().get("/").get_data(as_text=True)
        self.assertIn('href="#concerns"', html)
        self.assertIn('id="concerns"', html)
        self.assertIn("<strong>911</strong>", html)
        self.assertNotIn("Urgent complaints do not wait in line.", html)
        self.assertNotIn('id="urgent"', html)

    def test_authenticated_admin_visiting_login_is_redirected_to_dashboard(self):
        response = admin_client().get("/login")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/admin/dashboard")


class BulkComplaintTests(FeatureTestBase):
    def ids_with_status(self, *statuses):
        marks = ",".join("?" * len(statuses))
        return [r["id"] for r in self.q(f"SELECT id FROM complaints WHERE status IN ({marks})", statuses)]

    def fresh_open_ids(self, n=3):
        ids = self.ids_with_status("Pending", "Under Review", "Scheduled")[:n]
        self.assertGreaterEqual(len(ids), 1, "seed needs some open complaints")
        return ids

    def test_bpso_staff_can_bulk_resolve(self):
        ids = self.fresh_open_ids()
        hist_before = self.q("SELECT COUNT(*) c FROM status_history WHERE entity_type='complaint'")[0]["c"]
        c = client_for("liza.staff@barangayconnect.ph", STAFF_PW)
        r = c.post("/complaints/bulk", data={"action": "Resolved", "complaint_ids": ids, "remarks": "Settled on site"})
        self.assertEqual(r.status_code, 302)
        for row in self.q(f"SELECT status, remarks FROM complaints WHERE id IN ({','.join('?'*len(ids))})", ids):
            self.assertEqual(row["status"], "Resolved")
            self.assertEqual(row["remarks"], "Settled on site")
        hist_after = self.q("SELECT COUNT(*) c FROM status_history WHERE entity_type='complaint'")[0]["c"]
        self.assertEqual(hist_after - hist_before, len(ids))
        self.assertEqual(len(self.q(
            "SELECT 1 FROM audit_logs WHERE action='Updated complaint status to Resolved' AND details LIKE '%(bulk action)%'")),
            len(ids))
        owners = {r["resident_id"] for r in self.q(f"SELECT resident_id FROM complaints WHERE id IN ({','.join('?'*len(ids))})", ids)}
        for o in owners:
            self.assertTrue(self.q("SELECT 1 FROM notifications WHERE user_id=? AND message LIKE '%status changed to Resolved%'", (o,)))

    def test_finished_complaints_are_skipped_not_rewritten(self):
        done = self.ids_with_status("Resolved", "Closed", "Rejected")
        if not done:
            c = admin_client()
            c.post("/complaints/bulk", data={"action": "Resolved", "complaint_ids": self.fresh_open_ids(1)})
            done = self.ids_with_status("Resolved")
        before = self.q(f"SELECT id, status, updated_at FROM complaints WHERE id IN ({','.join('?'*len(done))})", done)
        hist_before = self.q("SELECT COUNT(*) c FROM status_history")[0]["c"]
        r = admin_client().post("/complaints/bulk", data={"action": "Resolved", "complaint_ids": done}, follow_redirects=True)
        self.assertIn("skipped", r.get_data(as_text=True))
        after = self.q(f"SELECT id, status, updated_at FROM complaints WHERE id IN ({','.join('?'*len(done))})", done)
        self.assertEqual([tuple(x) for x in before], [tuple(x) for x in after])
        self.assertEqual(hist_before, self.q("SELECT COUNT(*) c FROM status_history")[0]["c"])

    def test_resolved_can_be_closed_in_bulk(self):
        ids = self.fresh_open_ids(1)
        a = admin_client()
        a.post("/complaints/bulk", data={"action": "Resolved", "complaint_ids": ids})
        a.post("/complaints/bulk", data={"action": "Closed", "complaint_ids": ids})
        self.assertEqual(self.q("SELECT status FROM complaints WHERE id=?", (ids[0],))[0]["status"], "Closed")

    def test_only_complaint_handlers_may_use_it(self):
        ids = self.fresh_open_ids(1)
        for email in ("marco.staff@barangayconnect.ph", "ramon.staff@barangayconnect.ph"):  # treasurer, secretary
            c = client_for(email, STAFF_PW)
            self.assertEqual(c.post("/complaints/bulk", data={"action": "Closed", "complaint_ids": ids}).status_code, 403, email)
        before = self.q("SELECT status FROM complaints WHERE id=?", (ids[0],))[0]["status"]
        self.assertNotEqual(before, "Closed")

    def test_resident_cannot_use_it(self):
        ids = self.fresh_open_ids(1)
        email = self.q("SELECT email FROM users WHERE role='resident' LIMIT 1")[0]["email"]
        c = A.app.test_client()
        with c.session_transaction() as s:
            s["user_id"] = self.q("SELECT id FROM users WHERE email=?", (email,))[0]["id"]
            s["role"] = "resident"
        self.assertEqual(c.post("/complaints/bulk", data={"action": "Closed", "complaint_ids": ids}).status_code, 403)

    def test_bad_input_changes_nothing(self):
        ids = self.fresh_open_ids(2)
        snap = [tuple(r) for r in self.q("SELECT id, status FROM complaints ORDER BY id")]
        a = admin_client()
        for data in ({"action": "Rejected", "complaint_ids": ids},      # not an allowed bulk action
                     {"action": "Resolved"},                            # nothing selected
                     {"action": "Resolved", "complaint_ids": ["999999"]}):  # unknown id
            self.assertEqual(a.post("/complaints/bulk", data=data).status_code, 302)
        self.assertEqual(a.post("/complaints/bulk", data={"action": "Resolved", "complaint_ids": ["abc"]}).status_code, 400)
        self.assertEqual(snap, [tuple(r) for r in self.q("SELECT id, status FROM complaints ORDER BY id")])

    def test_staff_complaints_page_removes_bulk_selection_ui(self):
        admin_html = admin_client().get("/admin/complaints").get_data(as_text=True)
        self.assertNotIn('id="bulk-form"', admin_html)
        self.assertNotIn('id="bulk-all"', admin_html)
        self.assertNotIn('class="bulk-pick"', admin_html)
        self.assertNotIn('class="check-col"', admin_html)
        self.assertNotIn("Resolve selected", admin_html)
        self.assertNotIn("Close selected", admin_html)
        for column in ("Priority", "Reference", "Resident", "Address", "Contact",
                       "Category", "Status", "Filed", "View"):
            self.assertIn(column, admin_html)

        staff = client_for("liza.staff@barangayconnect.ph", STAFF_PW)
        staff_html = staff.get("/staff/complaints").get_data(as_text=True)
        self.assertNotIn('id="bulk-form"', staff_html)
        self.assertNotIn('id="bulk-all"', staff_html)
        self.assertNotIn('class="bulk-pick"', staff_html)
        self.assertNotIn('class="check-col"', staff_html)
        self.assertNotIn('name="complaint_ids"', staff_html)
        self.assertNotIn('name="remarks"', staff_html)
        self.assertNotIn("Resolve selected", staff_html)
        self.assertNotIn("Close selected", staff_html)
        self.assertLess(staff_html.index("<th>Priority</th>"), staff_html.index("<th>Reference</th>"))


class CommunityReportTests(FeatureTestBase):
    def _resident(self, name):
        conn = dbh.get_db()
        user_id = conn.execute(
            "INSERT INTO users (full_name,email,password_hash,role,address) VALUES (?,?,?,'resident',?)",
            (name, f"{name.lower().replace(' ','.')}@community.test", "unused", "Purok 991"),
        ).lastrowid
        conn.commit()
        conn.close()
        client = A.app.test_client()
        with client.session_transaction() as session:
            session["user_id"] = user_id
            session["role"] = "resident"
        return client, user_id

    def test_resident_reports_cluster_follow_and_shared_status_privately(self):
        from uuid import uuid4

        location = f"Street {uuid4().hex[:8]} Purok 991"
        first, first_id = self._resident("Private Reporter One")
        second, second_id = self._resident("Private Reporter Two")
        follower, follower_id = self._resident("Private Follower")
        payload = {
            "category": "Others",
            "description": "Private description must not be published.",
            "location": location,
        }
        self.assertEqual(first.post("/complaints/new", data=payload).status_code, 302)
        response = second.post("/complaints/new", data=payload, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("2 residents have reported the same issue, including you.", response.get_data(as_text=True))

        issue = self.q(
            "SELECT ci.* FROM community_issues ci JOIN complaints c ON c.community_issue_id=ci.id "
            "WHERE c.resident_id=?",
            (first_id,),
        )[0]
        complaint_ids = [r["id"] for r in self.q(
            "SELECT id FROM complaints WHERE community_issue_id=? ORDER BY id", (issue["id"],)
        )]
        self.assertEqual(len(complaint_ids), 2)
        self.assertEqual(len(self.q(
            "SELECT 1 FROM community_issue_follows WHERE user_id IN (?,?) AND community_issue_id=?",
            (first_id, second_id, issue["id"]),
        )), 2)
        bpso = client_for("liza.staff@barangayconnect.ph", STAFF_PW)
        management_html = bpso.get("/management/community-issues").get_data(as_text=True)
        self.assertIn("Pending Review", management_html)
        self.assertIn("Clustered Issues", management_html)
        self.assertIn("Review Issue & Reports", management_html)
        self.assertEqual(admin_client().get("/management/community-issues").status_code, 200)
        management_detail = bpso.get(f"/management/community-issues/{issue['id']}")
        self.assertEqual(management_detail.status_code, 200)
        management_detail_html = management_detail.get_data(as_text=True)
        self.assertIn("Verify All Pending", management_detail_html)
        self.assertIn("Private Reporter One", management_detail_html)
        self.assertIn("Private description must not be published.", management_detail_html)
        self.assertEqual(
            client_for("ramon.staff@barangayconnect.ph", STAFF_PW).get("/management/community-issues").status_code,
            403,
        )
        self.assertEqual(first.get("/management/community-issues").status_code, 403)
        verify_response = bpso.post(
            f"/management/community-issues/{issue['id']}/verify",
            data={"report_id": complaint_ids[0]},
        )
        self.assertEqual(verify_response.status_code, 302)
        self.assertEqual(self.q(
            "SELECT is_verified, verified_by FROM complaints WHERE id=?", (complaint_ids[0],)
        )[0]["is_verified"], 1)
        self.assertEqual(bpso.post(
            f"/management/community-issues/{issue['id']}/verify"
        ).status_code, 302)
        self.assertEqual(self.q(
            "SELECT COUNT(*) c FROM complaints WHERE community_issue_id=? AND is_verified=1",
            (issue["id"],),
        )[0]["c"], 2)
        self.assertEqual(follower.post(f"/community-reports/{issue['id']}/follow").status_code, 302)
        self.assertEqual(bpso.post(
            f"/management/community-issues/{issue['id']}/status",
            data={
                "status": "Under Review",
                "resident_message": "The barangay team is checking this location.",
            },
        ).status_code, 302)
        other_location = f"Different street {uuid4().hex[:8]} Purok 991"
        self.assertEqual(
            follower.post("/complaints/new", data={**payload, "location": other_location}).status_code,
            302,
        )
        other_issue_id = self.q(
            "SELECT community_issue_id FROM complaints WHERE resident_id=? ORDER BY id DESC LIMIT 1",
            (follower_id,),
        )[0]["community_issue_id"]
        self.assertNotEqual(other_issue_id, issue["id"], "same purok but a different street must not be clustered")

        list_html = first.get("/community-reports").get_data(as_text=True)
        self.assertIn(location, list_html)
        detail_url = f"/community-reports/{issue['id']}"
        detail_html = first.get(detail_url).get_data(as_text=True)
        for private_value in ("Private Reporter One", "Private Reporter Two", "Private description"):
            self.assertNotIn(private_value, detail_html)

        self.assertEqual(
            {r["status"] for r in self.q(
                "SELECT status FROM complaints WHERE community_issue_id=?", (issue["id"],)
            )},
            {"Under Review"},
        )
        self.assertEqual(
            self.q("SELECT status FROM community_issues WHERE id=?", (issue["id"],))[0]["status"],
            "Under Review",
        )
        self.assertEqual(len(self.q(
            "SELECT 1 FROM community_issue_updates WHERE community_issue_id=? AND status='Under Review'",
            (issue["id"],),
        )), 1)
        for recipient in (first_id, second_id, follower_id):
            notification = self.q(
                "SELECT link, message FROM notifications WHERE user_id=? AND message LIKE '%status updated to Under Review%'",
                (recipient,),
            )
            self.assertTrue(notification, f"missing shared status notification for user {recipient}")
            self.assertIn(detail_url, notification[0]["link"])
            self.assertIn("The barangay team is checking this location.", notification[0]["message"])
        self.assertEqual(
            admin_client().post(
                "/complaints/bulk",
                data={"action": "Resolved", "complaint_ids": [complaint_ids[0]]},
            ).status_code,
            302,
        )
        self.assertEqual(
            {r["status"] for r in self.q(
                "SELECT status FROM complaints WHERE community_issue_id=?", (issue["id"],)
            )},
            {"Resolved"},
        )
        self.assertEqual(
            self.q("SELECT status FROM community_issues WHERE id=?", (issue["id"],))[0]["status"],
            "Resolved",
        )
        self.assertEqual(
            follower.post(f"{detail_url}/unfollow").status_code,
            302,
        )
        self.assertEqual(first.post(f"{detail_url}/unfollow").status_code, 302)
        self.assertFalse(self.q(
            "SELECT 1 FROM community_issue_follows WHERE user_id=? AND community_issue_id=?",
            (follower_id, issue["id"]),
        ))
        admin_client().post(
            "/complaints/bulk",
            data={"action": "Closed", "complaint_ids": [complaint_ids[0]]},
        )
        self.assertTrue(self.q(
            "SELECT 1 FROM notifications WHERE user_id=? AND message LIKE '%status updated to Closed%'",
            (first_id,),
        ), "reporters must continue receiving updates after unfollowing")
        self.assertFalse(self.q(
            "SELECT 1 FROM notifications WHERE user_id=? AND message LIKE '%status updated to Closed%'",
            (follower_id,),
        ), "non-reporting followers should stop receiving updates after unfollowing")
        self.assertEqual(first.post("/complaints/new", data=payload).status_code, 302)
        reopened_issue_id = self.q(
            "SELECT community_issue_id FROM complaints WHERE resident_id=? ORDER BY id DESC LIMIT 1",
            (first_id,),
        )[0]["community_issue_id"]
        self.assertNotEqual(reopened_issue_id, issue["id"])

    def test_legacy_follow_migration_backfills_reports_and_is_idempotent(self):
        previous_path = dbh.DB_PATH
        with tempfile.TemporaryDirectory() as directory:
            dbh.DB_PATH = os.path.join(directory, "legacy.db")
            conn = dbh.get_db()
            conn.executescript("""
                CREATE TABLE users (id INTEGER PRIMARY KEY, full_name TEXT, role TEXT, category_scope TEXT, position TEXT);
                CREATE TABLE requests (id INTEGER PRIMARY KEY);
                CREATE TABLE complaints (
                    id INTEGER PRIMARY KEY, reference_no TEXT, resident_id INTEGER, category TEXT,
                    description TEXT, location TEXT, status TEXT, created_at TEXT, updated_at TEXT
                );
                CREATE TABLE community_issue_follows (
                    id INTEGER PRIMARY KEY, user_id INTEGER, category TEXT,
                    normalized_location TEXT, created_at TEXT
                );
                INSERT INTO users VALUES (11,'Legacy Resident','resident',NULL,NULL);
                INSERT INTO complaints VALUES (21,'CMP-OLD-1',11,'Others','Report','Purok 4','Pending',
                    '2026-01-01 00:00:00','2026-01-01 00:00:00');
                INSERT INTO community_issue_follows VALUES (31,11,'Others','purok:4','2026-01-01 00:00:00');
            """)
            conn.commit()
            conn.close()
            try:
                dbh.migrate_db()
                dbh.migrate_db()
                conn = dbh.get_db()
                self.assertEqual(conn.execute(
                    "SELECT community_issue_id FROM complaints WHERE id=21"
                ).fetchone()["community_issue_id"], 1)
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM community_issue_follows WHERE user_id=11 AND community_issue_id=1"
                ).fetchone()[0], 1)
                conn.close()
            finally:
                dbh.DB_PATH = previous_path


class ResidentFilterTests(FeatureTestBase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        conn = dbh.get_db()
        conn.execute("UPDATE users SET address='Purok 3, Barangay Holy Spirit' WHERE role='resident' AND id=(SELECT MIN(id) FROM users WHERE role='resident')")
        conn.execute("UPDATE users SET address='purok30 Mabini St', status='inactive' WHERE role='resident' AND id=(SELECT MAX(id) FROM users WHERE role='resident')")
        conn.execute("INSERT INTO users (full_name,email,password_hash,role,address) VALUES ('No Purok Person','np@x.ph','x','resident','Lot 5 Rizal Ave')")
        conn.commit()
        conn.close()

    def names(self, qs):
        html = admin_client().get("/admin/residents" + qs).get_data(as_text=True)
        return html

    def test_status_filter(self):
        inactive = [r["full_name"] for r in self.q("SELECT full_name FROM users WHERE role='resident' AND status='inactive'")]
        active = [r["full_name"] for r in self.q("SELECT full_name FROM users WHERE role='resident' AND status='active'")]
        html = self.names("?status=inactive")
        for n in inactive: self.assertIn(n, html)
        for n in active: self.assertNotIn(n, html)
        html = self.names("?status=active")
        for n in inactive: self.assertNotIn(n, html)
        for n in active: self.assertIn(n, html)

    def test_purok_is_exact_and_not_a_substring(self):
        p3 = self.q("SELECT full_name FROM users WHERE address LIKE 'Purok 3,%'")[0]["full_name"]
        p30 = self.q("SELECT full_name FROM users WHERE address LIKE 'purok30%'")[0]["full_name"]
        html = self.names("?purok=3")
        self.assertIn(p3, html)
        self.assertNotIn(p30, html)
        self.assertIn(p30, self.names("?purok=30"))

    def test_no_purok_option_and_address_search(self):
        self.assertIn("No Purok Person", self.names("?purok=none"))
        self.assertNotIn("No Purok Person", self.names("?purok=3"))
        self.assertIn("No Purok Person", self.names("?address=rizal"))
        self.assertNotIn("No Purok Person", self.names("?address=mabini"))

    def test_filters_combine_and_survive_garbage(self):
        html = self.names("?status=inactive&purok=3")
        self.assertIn("No residents found", html)
        for junk in ("?status=hacker", "?purok=999", "?purok=%27%3B--", "?address=%25"):
            self.assertEqual(admin_client().get("/admin/residents" + junk).status_code, 200, junk)

    def test_staff_view_gets_purok_but_not_status(self):
        staff = client_for("liza.staff@barangayconnect.ph", STAFF_PW)
        r = staff.get("/staff/residents?status=inactive&purok=3")
        html = r.get_data(as_text=True)
        self.assertEqual(r.status_code, 200)
        self.assertNotIn('name="status"', html)           # no status dropdown for staff
        self.assertNotIn("purok30 Mabini", html)           # purok filter still applied
        self.assertIn("Purok 3, Barangay Holy Spirit", html)

    def test_purok_helpers(self):
        self.assertEqual(dbh.purok_of("Purok 3, X"), "3")
        self.assertEqual(dbh.purok_of("purok03"), "3")
        self.assertEqual(dbh.purok_of("Purok 30"), "30")
        self.assertIsNone(dbh.purok_of("Zone 1"))
        self.assertIsNone(dbh.purok_of(None))


class StaffWorkloadTests(FeatureTestBase):
    def test_admin_staff_list_shows_only_assigned_active_work(self):
        conn = dbh.get_db()
        staff_id = conn.execute(
            "SELECT id FROM users WHERE role='staff' ORDER BY id LIMIT 1"
        ).fetchone()["id"]
        assignments = []
        for table, status_clause in (
            ("requests", "status NOT IN ('Completed','Rejected')"),
            ("requests", "status='Completed'"),
            ("complaints", "status NOT IN ('Resolved','Closed','Rejected')"),
            ("complaints", "status='Resolved'"),
        ):
            row = conn.execute(
                f"SELECT id, assigned_staff_id FROM {table} WHERE {status_clause} ORDER BY id LIMIT 1"
            ).fetchone()
            assignments.append((table, row["id"], row["assigned_staff_id"]))
            conn.execute(
                f"UPDATE {table} SET assigned_staff_id=? WHERE id=?", (staff_id, row["id"])
            )
        conn.commit()

        try:
            request_count = conn.execute(
                "SELECT COUNT(*) c FROM requests WHERE assigned_staff_id=? "
                "AND status NOT IN ('Completed','Rejected')", (staff_id,)
            ).fetchone()["c"]
            complaint_count = conn.execute(
                "SELECT COUNT(*) c FROM complaints WHERE assigned_staff_id=? "
                "AND status NOT IN ('Resolved','Closed','Rejected')", (staff_id,)
            ).fetchone()["c"]
            html = admin_client().get("/admin/staff").get_data(as_text=True)
            self.assertIn("Workload", html)
            self.assertIn(
                f"{request_count} requests · {complaint_count} complaints", html
            )
            self.assertIn(f"{request_count + complaint_count} active items", html)
        finally:
            for table, item_id, assigned_staff_id in assignments:
                conn.execute(
                    f"UPDATE {table} SET assigned_staff_id=? WHERE id=?",
                    (assigned_staff_id, item_id),
                )
            conn.commit()
            conn.close()

    def test_claim_slip_displays_barangay_seal_watermark(self):
        request_id = self.q(
            "SELECT id FROM requests WHERE status IN ('Approved','Completed') ORDER BY id LIMIT 1"
        )[0]["id"]
        html = admin_client().get(f"/requests/{request_id}").get_data(as_text=True)
        self.assertIn('class="claim-slip-watermark"', html)
        self.assertIn("/static/images/barangay-seal.png", html)
        seal_response = admin_client().get("/static/images/barangay-seal.png")
        self.assertEqual(seal_response.status_code, 200)
        seal_response.close()


if __name__ == "__main__":
    unittest.main()
