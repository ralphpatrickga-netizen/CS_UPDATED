import os
import random
import re
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

from werkzeug.security import check_password_hash, generate_password_hash

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import db as dbh  # noqa: E402

_boot_dir = tempfile.mkdtemp()
dbh.DB_PATH = os.path.join(_boot_dir, "boot.db")
import app as A  # noqa: E402


class SecurityAndAllowanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._old_path = dbh.DB_PATH
        cls._random_state = random.getstate()
        cls._dir = tempfile.mkdtemp()
        dbh.DB_PATH = os.path.join(cls._dir, "test.db")
        dbh._last_auto_check = time.time()
        dbh.init_db()
        conn = dbh.get_db()
        conn.execute(
            "INSERT INTO users (full_name,email,password_hash,role,is_super_admin,contact_number) "
            "VALUES (?,?,?,?,1,?)",
            ("Test Admin", "test-admin@example.test", generate_password_hash("Admin@1234"), "admin", "+639171000001"),
        )
        conn.commit()
        conn.close()

    @classmethod
    def tearDownClass(cls):
        random.setstate(cls._random_state)
        dbh.DB_PATH = cls._old_path

    def create_resident(self, email):
        conn = dbh.get_db()
        cursor = conn.execute(
            "INSERT INTO users (full_name,email,password_hash,role) VALUES (?,?,?,'resident')",
            ("Test Resident", email, generate_password_hash("Resident@1234")),
        )
        conn.commit()
        resident_id = cursor.lastrowid
        conn.close()
        return resident_id

    def test_semaphore_sends_philippine_number_in_local_format(self):
        response = Mock()
        response.json.return_value = [{"status": "Pending"}]
        with patch.dict(os.environ, {"SEMAPHORE_API_KEY": "test-key"}), patch.object(
            A.sms_service.requests, "post", return_value=response
        ) as post:
            A.sms_service.send_sms("+639171234567", "verification code")

        self.assertEqual(
            post.call_args.kwargs["data"],
            {
                "apikey": "test-key",
                "number": "09171234567",
                "message": "verification code",
            },
        )

    def test_registration_allows_email_only_and_normalizes_optional_mobile(self):
        client = A.app.test_client()
        data = {
            "full_name": "New Resident",
            "email": "new-resident@example.test",
            "password": "Resident@1234",
            "confirm_password": "Resident@1234",
            "address": "Purok 1",
        }
        response = client.post("/register", data=data)
        self.assertEqual(response.status_code, 302)
        conn = dbh.get_db()
        self.assertIsNone(conn.execute(
            "SELECT contact_number FROM users WHERE email=?", (data["email"],)
        ).fetchone()["contact_number"])
        conn.close()

        data["email"] = "new-resident-with-mobile@example.test"
        data["contact_number"] = "0917 123 4567"
        response = client.post("/register", data=data)
        self.assertEqual(response.status_code, 302)
        conn = dbh.get_db()
        contact = conn.execute(
            "SELECT contact_number FROM users WHERE email=?", (data["email"],)
        ).fetchone()["contact_number"]
        conn.close()
        self.assertEqual(contact, "+639171234567")

    def resident_client(self, resident_id):
        client = A.app.test_client()
        with client.session_transaction() as session:
            session["user_id"] = resident_id
            session["role"] = "resident"
        return client

    def admin_client(self):
        client = A.app.test_client()
        with client.session_transaction() as session:
            session["user_id"] = 1
            session["role"] = "admin"
        return client

    def test_reset_email_code_is_hashed_single_use_and_resets_without_login(self):
        email = "reset-flow@example.test"
        self.create_resident(email)
        client = A.app.test_client()
        with patch.object(A.email_service, "send_email") as send_email:
            response = client.post("/forgot-password", data={"email": email})
        self.assertEqual(response.status_code, 302)
        self.assertIn("/reset-password", response.location)
        send_email.assert_called_once()
        body = send_email.call_args.args[2]
        code = re.search(r"(?m)^(\d{6})$", body).group(1)
        conn = dbh.get_db()
        stored = conn.execute(
            "SELECT code_hash, expires_at FROM password_reset_codes"
        ).fetchone()
        self.assertNotEqual(stored["code_hash"], code)
        self.assertGreater(datetime.fromisoformat(stored["expires_at"]), datetime.now())
        conn.close()

        self.assertEqual(client.get(response.location).status_code, 200)
        response = client.post(
            "/reset-password",
            data={
                "email": email,
                "code": code,
                "password": "Updated@123",
                "confirm_password": "Updated@123",
            },
        )
        self.assertEqual(response.status_code, 302)
        conn = dbh.get_db()
        password_hash = conn.execute(
            "SELECT password_hash FROM users WHERE email=?", (email,)
        ).fetchone()["password_hash"]
        self.assertTrue(check_password_hash(password_hash, "Updated@123"))
        self.assertIsNotNone(conn.execute(
            "SELECT used_at FROM password_reset_codes WHERE user_id=("
            "SELECT id FROM users WHERE email=?)",
            (email,),
        ).fetchone()["used_at"])
        conn.close()
        response = client.post(
            "/reset-password",
            data={
                "email": email,
                "code": code,
                "password": "Another@123",
                "confirm_password": "Another@123",
            },
        )
        self.assertEqual(response.status_code, 400)

    def test_unknown_email_does_not_send_and_expired_reset_code_is_rejected(self):
        client = A.app.test_client()
        with patch.object(A.email_service, "send_email") as send_email:
            response = client.post("/forgot-password", data={"email": "missing@example.test"})
        send_email.assert_not_called()
        self.assertIn("/reset-password", response.location)
        email = "expired-reset@example.test"
        resident_id = self.create_resident(email)
        with patch.object(A.email_service, "send_email") as send_email:
            client.post("/forgot-password", data={"email": email})
        code = re.search(r"(?m)^(\d{6})$", send_email.call_args.args[2]).group(1)
        conn = dbh.get_db()
        conn.execute(
            "UPDATE password_reset_codes SET expires_at=? WHERE user_id=?",
            ((datetime.now() - timedelta(minutes=1)).isoformat(), resident_id),
        )
        conn.commit()
        conn.close()
        self.assertEqual(
            client.post(
                "/reset-password",
                data={
                    "email": email,
                    "code": code,
                    "password": "Updated@123",
                    "confirm_password": "Updated@123",
                },
            ).status_code,
            400,
        )

    def test_reset_code_locks_after_five_incorrect_attempts(self):
        email = "reset-attempts@example.test"
        self.create_resident(email)
        client = A.app.test_client()
        with patch.object(A.email_service, "send_email") as send_email:
            client.post("/forgot-password", data={"email": email})
        code = re.search(r"(?m)^(\d{6})$", send_email.call_args.args[2]).group(1)
        wrong_code = "000000" if code != "000000" else "000001"
        for _ in range(5):
            response = client.post(
                "/reset-password",
                data={
                    "email": email,
                    "code": wrong_code,
                    "password": "Updated@123",
                    "confirm_password": "Updated@123",
                },
            )
            self.assertEqual(response.status_code, 400)
        locked = client.post(
            "/reset-password",
            data={
                "email": email,
                "code": code,
                "password": "Updated@123",
                "confirm_password": "Updated@123",
            },
        )
        self.assertEqual(locked.status_code, 400)

    def test_reset_email_delivery_failure_is_not_reported_as_success(self):
        email = "failed-reset@example.test"
        resident_id = self.create_resident(email)
        client = A.app.test_client()
        with patch.object(
            A.email_service,
            "send_email",
            side_effect=A.email_service.EmailConfigurationError("not configured"),
        ):
            response = client.post("/forgot-password", data={"email": email}, follow_redirects=True)
        self.assertIn("could not deliver the reset email", response.get_data(as_text=True).lower())
        conn = dbh.get_db()
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM password_reset_codes WHERE user_id=? AND code_hash IS NOT NULL "
            "AND used_at IS NULL",
            (resident_id,),
        ).fetchone()[0], 0)
        conn.close()

    def test_admin_otp_delivery_expiry_resend_and_one_time_validation(self):
        email = "otp-admin@example.test"
        conn = dbh.get_db()
        admin_id = conn.execute(
            "INSERT INTO users (full_name,email,password_hash,role,contact_number) VALUES (?,?,?,'admin',?)",
            ("OTP Admin", email, generate_password_hash("Admin@1234"), "+639171000002"),
        ).lastrowid
        conn.commit()
        conn.close()

        client = A.app.test_client()
        with patch.object(A.email_service, "send_email") as send_email, patch.object(
            A.sms_service, "send_sms"
        ) as send_sms, patch.dict(
            os.environ, {"ADMIN_OTP_RESEND_SECONDS": "60", "LOGIN_SMS_ENABLED": "true"}
        ):
            response = client.post("/login", data={"email": email, "password": "Admin@1234"})
            self.assertEqual(response.status_code, 302)
            old_email_code = re.search(r"code is (\d{6})", send_email.call_args.args[2]).group(1)
            old_sms_code = re.search(r"code is (\d{6})", send_sms.call_args.args[1]).group(1)
            self.assertEqual(client.get("/verify-otp").status_code, 200)
            self.assertNotIn(old_email_code, client.get("/verify-otp").get_data(as_text=True))
            rate_limited = client.post("/verify-otp/resend", follow_redirects=True)
            self.assertIn("Please wait", rate_limited.get_data(as_text=True))
            self.assertEqual(send_email.call_count, 1)

            wrong_code = "000000" if old_email_code != "000000" else "000001"
            wrong = client.post("/verify-otp", data={"email_otp": wrong_code, "sms_otp": wrong_code})
            self.assertEqual(wrong.status_code, 302)
            conn = dbh.get_db()
            conn.execute(
                "UPDATE admin_otp_challenges SET expires_at=?, sent_at=? WHERE user_id=?",
                (
                    (datetime.now() - timedelta(seconds=1)).isoformat(),
                    (datetime.now() - timedelta(seconds=61)).isoformat(),
                    admin_id,
                ),
            )
            conn.commit()
            conn.close()
            expired = client.post(
                "/verify-otp",
                data={"email_otp": old_email_code, "sms_otp": old_sms_code},
                follow_redirects=True,
            )
            self.assertIn("verification code expired", expired.get_data(as_text=True).lower())

            self.assertEqual(client.post("/verify-otp/resend").status_code, 302)
            self.assertEqual(send_email.call_count, 2)
            self.assertEqual(send_sms.call_count, 2)
            new_email_code = re.search(r"code is (\d{6})", send_email.call_args.args[2]).group(1)
            new_sms_code = re.search(r"code is (\d{6})", send_sms.call_args.args[1]).group(1)
            self.assertNotEqual(new_email_code, old_email_code)
            self.assertNotEqual(new_sms_code, old_sms_code)
            old_rejected = client.post(
                "/verify-otp",
                data={"email_otp": old_email_code, "sms_otp": old_sms_code},
                follow_redirects=True,
            )
            self.assertIn("Incorrect email or SMS verification code.", old_rejected.get_data(as_text=True))
            accepted = client.post(
                "/verify-otp",
                data={"email_otp": new_email_code, "sms_otp": new_sms_code},
            )
            self.assertEqual(accepted.status_code, 302)
            conn = dbh.get_db()
            challenge = conn.execute(
                "SELECT email_otp_hash, sms_otp_hash, used_at FROM admin_otp_challenges WHERE user_id=?",
                (admin_id,),
            ).fetchone()
            conn.close()
            self.assertIsNone(challenge["email_otp_hash"])
            self.assertIsNone(challenge["sms_otp_hash"])
            self.assertIsNotNone(challenge["used_at"])

    def test_resident_login_requires_both_email_and_sms_codes(self):
        email = "mfa-resident@example.test"
        conn = dbh.get_db()
        resident_id = conn.execute(
            "INSERT INTO users (full_name,email,password_hash,role,contact_number) "
            "VALUES (?,?,?,'resident',?)",
            ("MFA Resident", email, generate_password_hash("Resident@1234"), "+639171000003"),
        ).lastrowid
        conn.commit()
        conn.close()
        client = A.app.test_client()

        with patch.object(A.email_service, "send_email") as send_email, patch.object(
            A.sms_service, "send_sms"
        ) as send_sms, patch.dict(os.environ, {"LOGIN_SMS_ENABLED": "true"}):
            response = client.post(
                "/login", data={"email": email, "password": "Resident@1234"}
            )
            self.assertEqual(response.status_code, 302)
            self.assertIn("/verify-otp", response.location)
            email_code = re.search(
                r"code is (\d{6})", send_email.call_args.args[2]
            ).group(1)
            sms_code = re.search(
                r"code is (\d{6})", send_sms.call_args.args[1]
            ).group(1)
            self.assertNotEqual(email_code, sms_code)

            email_only = client.post(
                "/verify-otp", data={"email_otp": email_code, "sms_otp": "000000"}
            )
            self.assertEqual(email_only.status_code, 302)
            with client.session_transaction() as session:
                self.assertNotIn("user_id", session)

            verified = client.post(
                "/verify-otp",
                data={"email_otp": email_code, "sms_otp": sms_code},
            )
            self.assertEqual(verified.status_code, 302)
            self.assertEqual(verified.location, "/dashboard")
            with client.session_transaction() as session:
                self.assertEqual(session["user_id"], resident_id)
                self.assertEqual(session["role"], "resident")

    def test_founder_without_mobile_can_complete_email_verified_setup(self):
        conn = dbh.get_db()
        conn.execute(
            "UPDATE users SET contact_number=NULL WHERE id=1"
        )
        conn.execute("DELETE FROM admin_otp_challenges WHERE user_id=1")
        conn.commit()
        conn.close()
        client = A.app.test_client()

        try:
            with patch.object(A.email_service, "send_email") as send_email, patch.object(
                A.sms_service, "send_sms"
            ) as send_sms, patch.dict(os.environ, {"LOGIN_SMS_ENABLED": "true"}):
                response = client.post(
                    "/login",
                    data={"email": "test-admin@example.test", "password": "Admin@1234"},
                )
                self.assertEqual(response.status_code, 302)
                email_code = re.search(
                    r"code is (\d{6})", send_email.call_args.args[2]
                ).group(1)
                email_verified = client.post(
                    "/verify-otp", data={"email_otp": email_code}
                )
                self.assertEqual(email_verified.location, "/setup-admin-mobile")

                sent = client.post(
                    "/setup-admin-mobile",
                    data={"action": "send", "contact_number": "0917 123 4567"},
                )
                self.assertEqual(sent.status_code, 200)
                sms_code = re.search(
                    r"code is (\d{6})", send_sms.call_args.args[1]
                ).group(1)
                verified = client.post(
                    "/setup-admin-mobile",
                    data={"action": "verify", "sms_otp": sms_code},
                )
                self.assertEqual(verified.location, "/admin/dashboard")

            with client.session_transaction() as session:
                self.assertEqual(session["user_id"], 1)
                self.assertEqual(session["role"], "admin")
        finally:
            conn = dbh.get_db()
            conn.execute(
                "UPDATE users SET contact_number=? WHERE id=1", ("+639171000001",)
            )
            conn.execute("DELETE FROM admin_otp_challenges WHERE user_id=1")
            conn.commit()
            conn.close()

    def test_founding_admin_otp_is_followed_by_required_password_change(self):
        email = "test-admin@example.test"
        conn = dbh.get_db()
        conn.execute(
            "UPDATE users SET must_reset_pw=1, recovery_email=? WHERE id=1",
            (email,),
        )
        conn.commit()
        conn.close()

        client = A.app.test_client()
        with patch.object(A.email_service, "send_email") as send_email, patch.object(
            A.sms_service, "send_sms"
        ) as send_sms, patch.dict(os.environ, {"LOGIN_SMS_ENABLED": "true"}):
            response = client.post(
                "/login",
                data={"email": email, "password": "Admin@1234"},
            )
            self.assertEqual(response.status_code, 302)
            self.assertEqual(send_email.call_args.args[0], email)
            email_code = re.search(r"code is (\d{6})", send_email.call_args.args[2]).group(1)
            sms_code = re.search(r"code is (\d{6})", send_sms.call_args.args[1]).group(1)

            verified = client.post(
                "/verify-otp",
                data={"email_otp": email_code, "sms_otp": sms_code},
            )
            self.assertEqual(verified.status_code, 302)
            self.assertIn("/first-login-reset", verified.location)
            self.assertEqual(client.get("/first-login-reset").status_code, 200)

        password = "NewFoundingPassword@123"
        response = client.post(
            "/first-login-reset",
            data={"password": password, "confirm_password": password},
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin", response.location)
        conn = dbh.get_db()
        account = conn.execute(
            "SELECT password_hash, must_reset_pw FROM users WHERE id=1"
        ).fetchone()
        self.assertTrue(check_password_hash(account["password_hash"], password))
        self.assertEqual(account["must_reset_pw"], 0)
        conn.close()
        administrators = client.get("/admin/administrators")
        self.assertEqual(administrators.status_code, 200)
        self.assertIn(b"Create Administrator Account", administrators.data)

    def test_email_only_login_works_without_mobile_or_sms_provider(self):
        email = "email-only-resident@example.test"
        resident_id = self.create_resident(email)
        client = A.app.test_client()
        with patch.object(A.email_service, "send_email") as send_email, patch.object(
            A.sms_service, "send_sms"
        ) as send_sms, patch.dict(os.environ, {"LOGIN_SMS_ENABLED": "false"}):
            response = client.post(
                "/login", data={"email": email, "password": "Resident@1234"}
            )
            self.assertEqual(response.status_code, 302)
            code = re.search(
                r"code is (\d{6})", send_email.call_args.args[2]
            ).group(1)
            send_sms.assert_not_called()
            page = client.get("/verify-otp").get_data(as_text=True)
            self.assertIn("email address", page)
            self.assertNotIn("SMS verification code", page)
            verified = client.post("/verify-otp", data={"email_otp": code})
            self.assertEqual(verified.status_code, 302)
            with client.session_transaction() as session:
                self.assertEqual(session["user_id"], resident_id)

    def test_admin_password_recovery_uses_registered_recovery_email(self):
        login_email = "recover-admin@example.test"
        recovery_email = "admin-recovery@example.test"
        conn = dbh.get_db()
        conn.execute(
            "INSERT INTO users (full_name,email,recovery_email,password_hash,role) "
            "VALUES (?,?,?,?, 'admin')",
            (
                "Recovery Admin",
                login_email,
                recovery_email,
                generate_password_hash("Admin@1234"),
            ),
        )
        conn.commit()
        conn.close()

        client = A.app.test_client()
        with patch.object(A.email_service, "send_email") as send_email:
            response = client.post("/forgot-password", data={"email": login_email})
            self.assertEqual(response.status_code, 302)
            self.assertEqual(send_email.call_args.args[0], recovery_email)
            code = re.search(r"(?m)^(\d{6})$", send_email.call_args.args[2]).group(1)

        password = "RecoveredAdminPassword@123"
        response = client.post(
            "/reset-password",
            data={
                "email": login_email,
                "code": code,
                "password": password,
                "confirm_password": password,
            },
        )
        self.assertEqual(response.status_code, 302)
        conn = dbh.get_db()
        account = conn.execute(
            "SELECT password_hash FROM users WHERE email=?", (login_email,)
        ).fetchone()
        self.assertTrue(check_password_hash(account["password_hash"], password))
        conn.close()

    def test_allowance_cooldown_restore_and_restore_once(self):
        resident_id = self.create_resident("allowance@example.test")
        client = self.resident_client(resident_id)
        complaint_ids = []
        for index in range(5):
            response = client.post(
                "/complaints/new",
                data={
                    "category": "Noise Complaint",
                    "description": f"Test concern {index}",
                    "location": f"Allowance test location {index}",
                },
                follow_redirects=True,
            )
            self.assertEqual(response.status_code, 200)
            conn = dbh.get_db()
            complaint = conn.execute(
                "SELECT id FROM complaints WHERE resident_id=? ORDER BY id DESC LIMIT 1",
                (resident_id,),
            ).fetchone()
            complaint_ids.append(complaint["id"])
            allowance = conn.execute(
                "SELECT * FROM resident_concern_allowances WHERE user_id=?", (resident_id,)
            ).fetchone()
            if index < 4:
                self.assertIsNone(allowance["cooldown_started_at"])
                self.assertIsNone(allowance["cooldown_expires_at"])
            else:
                started = datetime.fromisoformat(allowance["cooldown_started_at"])
                expires = datetime.fromisoformat(allowance["cooldown_expires_at"])
                self.assertEqual(expires - started, timedelta(hours=24))
            conn.close()

        blocked = client.post(
            "/complaints/new",
            data={
                "category": "Noise Complaint",
                "description": "Must remain blocked.",
                "location": "Blocked location",
            },
        )
        self.assertEqual(blocked.status_code, 200)
        conn = dbh.get_db()
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM complaints WHERE resident_id=?", (resident_id,)).fetchone()[0],
            5,
        )
        conn.close()

        admin = self.admin_client()
        admin.post(f"/complaints/{complaint_ids[0]}/update", data={"status": "Resolved"})
        conn = dbh.get_db()
        allowance = conn.execute(
            "SELECT * FROM resident_concern_allowances WHERE user_id=?", (resident_id,)
        ).fetchone()
        self.assertEqual(allowance["remaining"], 1)
        self.assertIsNone(allowance["cooldown_expires_at"])
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM concern_allowance_events "
            "WHERE complaint_id=? AND event_type='resolved_restoration'",
            (complaint_ids[0],),
        ).fetchone()[0], 1)
        conn.close()

        admin.post(f"/complaints/{complaint_ids[0]}/update", data={"status": "Resolved"})
        admin.post(f"/complaints/{complaint_ids[1]}/update", data={"status": "Rejected"})
        conn = dbh.get_db()
        self.assertEqual(
            conn.execute(
                "SELECT remaining FROM resident_concern_allowances WHERE user_id=?",
                (resident_id,),
            ).fetchone()["remaining"],
            1,
        )
        conn.close()

        client.get("/logout")
        with client.session_transaction() as session:
            session["user_id"] = resident_id
            session["role"] = "resident"
        self.assertIn("You have 1 of 5 concern submissions available.", client.get(
            "/complaints/new"
        ).get_data(as_text=True))

        conn = dbh.get_db()
        conn.execute(
            "UPDATE resident_concern_allowances SET remaining=0, cooldown_started_at=?, "
            "cooldown_expires_at=? WHERE user_id=?",
            (
                (datetime.now() - timedelta(hours=24, seconds=1)).isoformat(),
                (datetime.now() - timedelta(seconds=1)).isoformat(),
                resident_id,
            ),
        )
        conn.commit()
        conn.close()
        refreshed = client.get("/complaints/new").get_data(as_text=True)
        self.assertIn("You have 5 of 5 concern submissions available.", refreshed)

    def test_concurrent_submissions_cannot_exceed_five_slots(self):
        resident_id = self.create_resident("concurrent@example.test")

        def submit(index):
            client = self.resident_client(resident_id)
            response = client.post(
                "/complaints/new",
                data={
                    "category": "Noise Complaint",
                    "description": f"Concurrent submission {index}",
                    "location": f"Concurrent location {index}",
                },
            )
            return response.status_code

        with ThreadPoolExecutor(max_workers=8) as pool:
            statuses = list(pool.map(submit, range(8)))
        self.assertEqual(statuses.count(302), 5)
        self.assertEqual(statuses.count(200), 3)
        conn = dbh.get_db()
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM complaints WHERE resident_id=?", (resident_id,)
        ).fetchone()[0], 5)
        self.assertEqual(conn.execute(
            "SELECT remaining FROM resident_concern_allowances WHERE user_id=?", (resident_id,)
        ).fetchone()["remaining"], 0)
        conn.close()
