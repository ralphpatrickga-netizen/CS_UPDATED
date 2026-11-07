import os
import sys
import tempfile
import unittest
from datetime import date, timedelta
from urllib.parse import urlencode

from werkzeug.security import generate_password_hash

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import db as dbh  # noqa: E402

_boot_dir = tempfile.mkdtemp()
dbh.DB_PATH = os.path.join(_boot_dir, "boot.db")
import app as A  # noqa: E402


class AnalyticsReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._old_path = dbh.DB_PATH
        cls._old_ready = A._db_ready
        cls._temp_dir = tempfile.mkdtemp()
        dbh.DB_PATH = os.path.join(cls._temp_dir, "analytics.db")
        dbh.init_db()
        A._db_ready = True
        conn = dbh.get_db()
        cls.admin_id = conn.execute(
            "INSERT INTO users (full_name,email,password_hash,role) VALUES (?,?,?,'admin')",
            ("Analytics Admin", "analytics-admin@example.test", generate_password_hash("Admin@123")),
        ).lastrowid
        cls.bpso_id = conn.execute(
            "INSERT INTO users (full_name,email,password_hash,role,office) VALUES (?,?,?,'staff','bpso')",
            ("Analytics BPSO", "analytics-bpso@example.test", generate_password_hash("Staff@123")),
        ).lastrowid
        cls.secretary_id = conn.execute(
            "INSERT INTO users (full_name,email,password_hash,role,office) VALUES (?,?,?,'staff','secretary')",
            ("Analytics Secretary", "analytics-secretary@example.test", generate_password_hash("Staff@123")),
        ).lastrowid
        cls.unassigned_id = conn.execute(
            "INSERT INTO users (full_name,email,password_hash,role) VALUES (?,?,?,'staff')",
            ("Unassigned Staff", "analytics-unassigned@example.test", generate_password_hash("Staff@123")),
        ).lastrowid
        resident_id = conn.execute(
            "INSERT INTO users (full_name,email,password_hash,role) VALUES (?,?,?,'resident')",
            ("Analytics Resident", "analytics-resident@example.test", generate_password_hash("Resident@123")),
        ).lastrowid

        today = date.today()
        current_date = today.replace(day=10) if today.day >= 10 else today
        prior_month_end = (today.replace(day=1) - timedelta(days=1))
        prior_month_date = prior_month_end.replace(day=10) if prior_month_end.day >= 10 else prior_month_end
        conn.executemany(
            "INSERT INTO complaints (reference_no,resident_id,category,description,status,is_verified,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            [
                ("ANA-C-1", resident_id, "Noise Complaint", "Noise report", "Pending", 0,
                 f"{current_date.isoformat()} 12:00:00"),
                ("ANA-C-2", resident_id, "Sanitation", "Waste report", "Resolved", 1,
                 f"{prior_month_date.isoformat()} 12:00:00"),
            ],
        )
        conn.executemany(
            "INSERT INTO requests (reference_no,resident_id,request_type,status,created_at) "
            "VALUES (?,?,?,?,?)",
            [
                ("ANA-R-1", resident_id, "Barangay Clearance", "Approved",
                 f"{current_date.isoformat()} 12:00:00"),
                ("ANA-R-2", resident_id, "Certificate of Residency", "Submitted",
                 f"{prior_month_date.isoformat()} 12:00:00"),
            ],
        )
        conn.commit()
        conn.close()

    @classmethod
    def tearDownClass(cls):
        dbh.DB_PATH = cls._old_path
        A._db_ready = cls._old_ready

    def client_as(self, user_id, role):
        client = A.app.test_client()
        with client.session_transaction() as session:
            session["user_id"] = user_id
            session["role"] = role
        return client

    def test_admin_analytics_filters_and_previous_period_counts(self):
        today = date.today()
        current_month = today.replace(day=1)
        next_month = (current_month.replace(day=28) + timedelta(days=4)).replace(day=1)
        query = urlencode({
            "range": "custom",
            "start_date": current_month.isoformat(),
            "end_date": (next_month - timedelta(days=1)).isoformat(),
        })
        client = self.client_as(self.admin_id, "admin")
        response = client.get(f"/reports?{query}")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Most Common Complaints", html)
        self.assertIn("Most Requested Documents", html)
        self.assertIn("Monthly Transaction Trends", html)
        self.assertIn("Noise Complaint", html)
        self.assertIn("Barangay Clearance", html)
        self.assertIn("1 vs 1", html)

        filtered = client.get(
            f"/reports?{urlencode({'range':'all','category':'Noise Complaint'})}"
        ).get_data(as_text=True)
        self.assertIn("Total Complaints", filtered)
        self.assertIn("Noise Complaint", filtered)
        self.assertNotIn("<strong>Sanitation</strong>", filtered)

    def test_staff_analytics_are_scoped_to_assigned_office(self):
        bpso = self.client_as(self.bpso_id, "staff")
        bpso_page = bpso.get("/reports")
        self.assertEqual(bpso_page.status_code, 200)
        bpso_html = bpso_page.get_data(as_text=True)
        self.assertIn("Noise Complaint", bpso_html)
        self.assertNotIn("Barangay Clearance", bpso_html)
        self.assertNotIn("Most Requested Documents", bpso_html)

        secretary = self.client_as(self.secretary_id, "staff")
        secretary_page = secretary.get("/reports")
        self.assertEqual(secretary_page.status_code, 200)
        secretary_html = secretary_page.get_data(as_text=True)
        self.assertIn("Certificate of Residency", secretary_html)
        self.assertNotIn("Noise Complaint", secretary_html)
        self.assertNotIn("Most Common Complaints", secretary_html)

        unassigned = self.client_as(self.unassigned_id, "staff")
        self.assertEqual(unassigned.get("/reports").status_code, 403)
        self.assertEqual(unassigned.get("/reports/export?format=csv").status_code, 403)

    def test_filtered_exports_include_real_report_data(self):
        client = self.client_as(self.admin_id, "admin")
        csv_response = client.get("/reports/export?range=all&format=csv")
        self.assertEqual(csv_response.status_code, 200)
        self.assertIn(b"Noise Complaint", csv_response.data)
        self.assertIn(b"Barangay Clearance", csv_response.data)

        pdf_response = client.get("/reports/export?range=all&format=pdf")
        self.assertEqual(pdf_response.status_code, 200)
        self.assertEqual(pdf_response.mimetype, "application/pdf")
        self.assertTrue(pdf_response.data.startswith(b"%PDF"))

        xlsx_response = client.get("/reports/export?range=all&format=xlsx")
        self.assertEqual(xlsx_response.status_code, 200)
        self.assertTrue(xlsx_response.data.startswith(b"PK"))

        filtered_csv = client.get(
            "/reports/export?range=all&category=Noise+Complaint&format=csv"
        )
        self.assertIn(b"Noise Complaint", filtered_csv.data)
        self.assertNotIn(b"Sanitation", filtered_csv.data)

    def test_invalid_custom_range_is_reported_without_server_error(self):
        client = self.client_as(self.admin_id, "admin")
        response = client.get("/reports?range=custom&start_date=2026-12-20&end_date=2026-12-01")
        self.assertEqual(response.status_code, 200)
        self.assertIn("end date must be on or after", response.get_data(as_text=True))

    def test_empty_date_range_shows_empty_state(self):
        client = self.client_as(self.admin_id, "admin")
        future = date.today() + timedelta(days=3650)
        response = client.get(
            f"/reports?range=custom&start_date={future.isoformat()}&end_date={future.isoformat()}"
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("No transactions match these filters", response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
