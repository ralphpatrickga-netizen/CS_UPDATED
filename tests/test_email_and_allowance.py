import os
import random
import re
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
import unittest
from datetime import datetime, timedelta
from urllib.parse import urlsplit
from unittest.mock import MagicMock, patch

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

    def test_registration_allows_email_only_and_normalizes_optional_mobile(self):
        client = A.app.test_client()
        data = {
            "full_name": "New Resident",
            "email": "new-resident@example.test",
            "password": "Resident@1234",
            "confirm_password": "Resident@1234",
            "address": "Purok 1",
        }
        with patch.dict(os.environ, {"APP_BASE_URL": "https://barangayconnect.example.test"}), \
                patch.object(A.email_service, "send_email") as send_email:
            response = client.post("/register", data=data)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, "/verify-registration")
        code = re.search(r"is (\d{6})", send_email.call_args.args[2]).group(1)
        conn = dbh.get_db()
        pending = conn.execute(
            "SELECT contact_number,email_verified FROM users WHERE email=?",
            (data["email"],),
        ).fetchone()
        conn.close()
        self.assertIsNone(pending["contact_number"])
        self.assertEqual(pending["email_verified"], 0)
        rejected = client.post("/login", data={"email": data["email"], "password": "Resident@1234"})
        self.assertEqual(rejected.status_code, 403)
        verified = client.post("/verify-registration", data={"email_otp": code})
        self.assertEqual(verified.location, "/login")
        conn = dbh.get_db()
        self.assertEqual(conn.execute(
            "SELECT email_verified FROM users WHERE email=?", (data["email"],)
        ).fetchone()["email_verified"], 1)
        conn.close()

        data["email"] = "new-resident-with-mobile@example.test"
        data["contact_number"] = "0917 123 4567"
        with patch.dict(os.environ, {"APP_BASE_URL": "https://barangayconnect.example.test"}), \
                patch.object(A.email_service, "send_email") as send_email:
            response = client.post("/register", data=data)
        self.assertEqual(response.status_code, 302)
        conn = dbh.get_db()
        contact = conn.execute(
            "SELECT contact_number FROM users WHERE email=?", (data["email"],)
        ).fetchone()["contact_number"]
        conn.close()
        self.assertEqual(contact, "+639171234567")
        self.assertEqual(client.get("/verify-registration").status_code, 200)

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

    def test_smtp_service_uses_configured_tls_auth_and_message(self):
        config = {
            "SMTP_HOST": "smtp.example.test",
            "SMTP_PORT": "587",
            "SMTP_USERNAME": "mailer@example.test",
            "SMTP_PASSWORD": "test-secret",
            "SMTP_FROM": "BarangayConnect <no-reply@example.test>",
            "SMTP_USE_TLS": "true",
            "SMTP_USE_SSL": "false",
        }
        smtp = MagicMock()
        smtp.__enter__.return_value = smtp
        with patch.dict(os.environ, config), patch.object(
            A.email_service.smtplib, "SMTP", return_value=smtp
        ) as smtp_constructor:
            A.email_service.send_email("resident@example.test", "Test subject", "Test body")
        smtp_constructor.assert_called_once_with("smtp.example.test", 587, timeout=15)
        smtp.starttls.assert_called_once()
        smtp.login.assert_called_once_with("mailer@example.test", "test-secret")
        message = smtp.send_message.call_args.args[0]
        self.assertEqual(message["To"], "resident@example.test")
        self.assertEqual(message["Subject"], "Test subject")
        self.assertIn("Test body", message.get_content())

    def test_smtp_service_rejects_partial_auth_configuration(self):
        config = {
            "SMTP_HOST": "smtp.example.test",
            "SMTP_PORT": "587",
            "SMTP_USERNAME": "mailer@example.test",
            "SMTP_PASSWORD": "",
            "SMTP_FROM": "no-reply@example.test",
            "SMTP_USE_TLS": "true",
            "SMTP_USE_SSL": "false",
        }
        with patch.dict(os.environ, config):
            with self.assertRaises(A.email_service.EmailConfigurationError):
                A.email_service.send_email("resident@example.test", "Test subject", "Test body")

    def test_production_reset_link_requires_configured_public_https_origin(self):
        config = {"APP_BASE_URL": "", "RENDER_EXTERNAL_URL": ""}
        with patch.dict(os.environ, config), A.app.test_request_context("/"):
            with self.assertRaisesRegex(
                A.email_service.EmailConfigurationError, "APP_BASE_URL must be configured"
            ):
                A._password_reset_link("test-token")
        with patch.dict(os.environ, {
            "APP_BASE_URL": "http://localhost:5000",
            "RENDER_EXTERNAL_URL": "",
        }), A.app.test_request_context("/"):
            with self.assertRaisesRegex(
                A.email_service.EmailConfigurationError, "localhost reset URL"
            ):
                A._password_reset_link("test-token")

    def test_reset_email_link_is_hashed_single_use_and_resets_without_login(self):
        email = "reset-flow@example.test"
        resident_id = self.create_resident(email)
        client = A.app.test_client()
        with patch.dict(os.environ, {"APP_BASE_URL": "https://barangayconnect.example.test"}), \
                patch.object(A.email_service, "send_email") as send_email:
            response = client.post("/forgot-password", data={"email": email})
            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.location, "/forgot-password?requested=1")
            confirmation = client.get(response.location)
        confirmation_html = confirmation.get_data(as_text=True)
        self.assertIn("If an active account matches that email", confirmation_html)
        self.assertNotIn('id="forgot-password-form"', confirmation_html)
        self.assertNotIn("Send Reset Link", confirmation_html)
        send_email.assert_called_once()
        body = send_email.call_args.args[2]
        reset_url = re.search(r"https://barangayconnect\.example\.test/reset-password/\S+", body).group(0)
        self.assertEqual(send_email.call_args.args[1], "BarangayConnect - Password Reset Request")
        html_body = send_email.call_args.kwargs["html_body"]
        self.assertIn(f'href="{reset_url}"', html_body)
        self.assertIn(">Reset Password</a>", html_body)
        token = urlsplit(reset_url).path.rsplit("/", 1)[-1]
        self.assertEqual(send_email.call_args.args[0], email)
        conn = dbh.get_db()
        stored = conn.execute(
            "SELECT token_hash, expires_at, used_at FROM password_reset_tokens WHERE user_id=?",
            (resident_id,),
        ).fetchone()
        self.assertNotEqual(stored["token_hash"], token)
        self.assertEqual(stored["token_hash"], A._token_digest(token))
        self.assertIsNone(stored["used_at"])
        self.assertGreater(datetime.fromisoformat(stored["expires_at"]), datetime.now())
        conn.close()

        self.assertEqual(client.get(reset_url).status_code, 200)
        mismatch = client.post(
            urlsplit(reset_url).path,
            data={"password": "Updated@123", "confirm_password": "Different@123"},
        )
        self.assertEqual(mismatch.status_code, 200)
        self.assertIn("Passwords do not match", mismatch.get_data(as_text=True))
        response = client.post(
            urlsplit(reset_url).path,
            data={"password": "Updated@123", "confirm_password": "Updated@123"},
        )
        self.assertEqual(response.status_code, 302)
        conn = dbh.get_db()
        account = conn.execute("SELECT password_hash,must_reset_pw FROM users WHERE id=?", (resident_id,)).fetchone()
        password_hash = account["password_hash"]
        self.assertTrue(check_password_hash(password_hash, "Updated@123"))
        self.assertFalse(check_password_hash(password_hash, "Resident@1234"))
        self.assertEqual(account["must_reset_pw"], 0)
        self.assertIsNotNone(conn.execute(
            "SELECT used_at FROM password_reset_tokens WHERE user_id=?",
            (resident_id,),
        ).fetchone()["used_at"])
        conn.close()
        response = client.get(reset_url)
        self.assertEqual(response.status_code, 400)
        with patch.object(A.email_service, "send_email") as send_email:
            old_password = client.post(
                "/login", data={"email": email, "password": "Resident@1234"}
            )
            self.assertEqual(old_password.status_code, 200)
            self.assertEqual(send_email.call_count, 0)
            new_password = client.post(
                "/login", data={"email": email, "password": "Updated@123"}
            )
            self.assertEqual(new_password.location, "/dashboard")
            self.assertEqual(send_email.call_count, 0)

    def test_staff_reset_link_uses_registered_email_and_invalidates_previous_token(self):
        email = "reset-staff@example.test"
        conn = dbh.get_db()
        staff_id = conn.execute(
            "INSERT INTO users (full_name,email,recovery_email,password_hash,role,office) "
            "VALUES (?,?,?,?, 'staff','bpso')",
            ("Reset Staff", email, "alternate@example.test", generate_password_hash("Staff@1234")),
        ).lastrowid
        conn.commit()
        conn.close()
        client = A.app.test_client()
        with patch.dict(os.environ, {
            "APP_BASE_URL": "https://barangayconnect.example.test",
            "PASSWORD_RESET_RESEND_SECONDS": "1",
        }), patch.object(A.email_service, "send_email") as send_email:
            client.post("/forgot-password", data={"email": email})
            first_token = urlsplit(
                re.search(r"https://\S+", send_email.call_args.args[2]).group(0)
            ).path.rsplit("/", 1)[-1]
            conn = dbh.get_db()
            conn.execute(
                "UPDATE password_reset_limits SET last_sent_at=? WHERE user_id=?",
                ((datetime.now() - timedelta(seconds=2)).isoformat(), staff_id),
            )
            conn.commit()
            conn.close()
            client.post("/forgot-password", data={"email": email})
        self.assertEqual(send_email.call_count, 2)
        self.assertEqual(send_email.call_args.args[0], email)
        second_token = urlsplit(
            re.search(r"https://\S+", send_email.call_args.args[2]).group(0)
        ).path.rsplit("/", 1)[-1]
        self.assertNotEqual(first_token, second_token)
        conn = dbh.get_db()
        tokens = conn.execute(
            "SELECT token_hash,used_at FROM password_reset_tokens WHERE user_id=? ORDER BY id",
            (staff_id,),
        ).fetchall()
        self.assertEqual(tokens[0]["token_hash"], A._token_digest(first_token))
        self.assertIsNotNone(tokens[0]["used_at"])
        self.assertEqual(tokens[1]["token_hash"], A._token_digest(second_token))
        self.assertIsNone(tokens[1]["used_at"])
        conn.commit()
        conn.close()

    def test_password_reset_requests_are_rate_limited(self):
        email = "limited-reset@example.test"
        self.create_resident(email)
        client = A.app.test_client()
        with patch.dict(os.environ, {
            "PASSWORD_RESET_RESEND_SECONDS": "1",
            "PASSWORD_RESET_MAX_SENDS_PER_HOUR": "1",
            "APP_BASE_URL": "https://barangayconnect.example.test",
        }), patch.object(A.email_service, "send_email") as send_email:
            client.post("/forgot-password", data={"email": email})
            limited = client.post(
                "/forgot-password", data={"email": email}, follow_redirects=True
            )
        self.assertEqual(send_email.call_count, 1)
        self.assertIn("If an active account matches that email", limited.get_data(as_text=True))
        self.assertNotIn('id="forgot-password-form"', limited.get_data(as_text=True))

    def test_unknown_email_and_smtp_failure_have_generic_response(self):
        client = A.app.test_client()
        with patch.object(A.email_service, "send_email") as send_email:
            unknown = client.post(
                "/forgot-password",
                data={"email": "missing@example.test"},
                follow_redirects=True,
            )
        send_email.assert_not_called()
        email = "failed-reset@example.test"
        user_id = self.create_resident(email)
        with patch.dict(os.environ, {"APP_BASE_URL": "https://barangayconnect.example.test"}), \
                patch.object(
            A.email_service,
            "send_email",
            side_effect=A.email_service.EmailConfigurationError("not configured"),
        ):
            failed = client.post(
                "/forgot-password", data={"email": email}, follow_redirects=True
            )
        self.assertEqual(unknown.get_data(as_text=True), failed.get_data(as_text=True))
        conn = dbh.get_db()
        token = conn.execute(
            "SELECT used_at FROM password_reset_tokens WHERE user_id=?", (user_id,)
        ).fetchone()
        self.assertIsNotNone(token["used_at"])
        conn.close()
        with patch.dict(os.environ, {"APP_BASE_URL": "https://barangayconnect.example.test"}), \
                patch.object(A.email_service, "send_email") as retry_email:
            retry = client.post(
                "/forgot-password",
                data={"email": email},
                follow_redirects=True,
            )
        self.assertEqual(retry_email.call_count, 1)
        self.assertNotIn('id="forgot-password-form"', retry.get_data(as_text=True))

    def test_invalid_and_expired_reset_links_are_rejected(self):
        email = "expired-reset@example.test"
        user_id = self.create_resident(email)
        client = A.app.test_client()
        with patch.dict(os.environ, {"APP_BASE_URL": "https://barangayconnect.example.test"}), \
                patch.object(A.email_service, "send_email") as send_email:
            client.post("/forgot-password", data={"email": email})
        reset_url = re.search(r"https?://\S+", send_email.call_args.args[2]).group(0)
        self.assertEqual(client.get("/reset-password/not-a-valid-token").status_code, 400)
        conn = dbh.get_db()
        conn.execute(
            "UPDATE password_reset_tokens SET expires_at=? WHERE user_id=?",
            ((datetime.now() - timedelta(minutes=1)).isoformat(), user_id),
        )
        conn.commit()
        conn.close()
        self.assertEqual(client.get(reset_url).status_code, 400)

    def test_registration_verification_rejects_wrong_expired_and_reused_codes(self):
        email = "verify-registration@example.test"
        client = A.app.test_client()
        with patch.object(A.email_service, "send_email") as send_email:
            response = client.post("/register", data={
                "full_name": "Unverified Resident",
                "email": email,
                "password": "Resident@1234",
                "confirm_password": "Resident@1234",
                "address": "Purok 2",
            })
        self.assertEqual(response.location, "/verify-registration")
        code = re.search(r"is (\d{6})", send_email.call_args.args[2]).group(1)
        wrong_code = "000000" if code != "000000" else "000001"
        wrong = client.post("/verify-registration", data={"email_otp": wrong_code}, follow_redirects=True)
        self.assertIn("Incorrect email verification code", wrong.get_data(as_text=True))
        conn = dbh.get_db()
        conn.execute(
            "UPDATE registration_email_verifications SET expires_at=?,sent_at=? WHERE user_id=("
            "SELECT id FROM users WHERE email=?)",
            (
                (datetime.now() - timedelta(seconds=1)).isoformat(),
                (datetime.now() - timedelta(seconds=2)).isoformat(),
                email,
            ),
        )
        conn.commit()
        conn.close()
        expired = client.post(
            "/verify-registration", data={"email_otp": code}, follow_redirects=True
        )
        self.assertIn("invalid or expired", expired.get_data(as_text=True).lower())
        with patch.dict(os.environ, {"REGISTRATION_OTP_RESEND_SECONDS": "1"}), \
                patch.object(A.email_service, "send_email") as resend_email:
            client.post("/verify-registration/resend")
        new_code = re.search(r"is (\d{6})", resend_email.call_args.args[2]).group(1)
        conn = dbh.get_db()
        new_code_hash = conn.execute(
            "SELECT otp_hash FROM registration_email_verifications WHERE user_id=("
            "SELECT id FROM users WHERE email=?)",
            (email,),
        ).fetchone()["otp_hash"]
        conn.close()
        self.assertEqual(new_code_hash, A._otp_digest(new_code))
        self.assertNotEqual(new_code_hash, A._otp_digest(code))
        old_code = client.post(
            "/verify-registration", data={"email_otp": code}, follow_redirects=True
        )
        self.assertIn("Incorrect email verification code", old_code.get_data(as_text=True))
        verified = client.post("/verify-registration", data={"email_otp": new_code})
        self.assertEqual(verified.location, "/login")

    def test_registration_email_is_still_one_time_and_rate_limited(self):
        email = "registration-one-time@example.test"
        client = A.app.test_client()
        with patch.dict(os.environ, {"REGISTRATION_OTP_RESEND_SECONDS": "60"}), \
                patch.object(A.email_service, "send_email") as send_email:
            client.post("/register", data={
                "full_name": "Verified Resident",
                "email": email,
                "password": "Resident@1234",
                "confirm_password": "Resident@1234",
                "address": "Purok 3",
            })
            code = re.search(r"is (\d{6})", send_email.call_args.args[2]).group(1)
            rate_limited = client.post(
                "/verify-registration/resend", follow_redirects=True
            )
            self.assertIn("Please wait", rate_limited.get_data(as_text=True))
            self.assertEqual(send_email.call_count, 1)
            verified = client.post("/verify-registration", data={"email_otp": code})
            self.assertEqual(verified.location, "/login")
            self.assertEqual(
                client.post("/verify-registration", data={"email_otp": code}).location,
                "/register",
            )
        response = client.post("/login", data={"email": email, "password": "Resident@1234"})
        self.assertEqual(response.location, "/dashboard")
        with client.session_transaction() as session:
            self.assertIn("user_id", session)

    def test_registration_smtp_failure_leaves_account_unverified_and_resend_works(self):
        email = "registration-delivery-failure@example.test"
        client = A.app.test_client()
        with patch.dict(os.environ, {"REGISTRATION_OTP_RESEND_SECONDS": "1"}), \
                patch.object(
                    A.email_service,
                    "send_email",
                    side_effect=A.email_service.EmailConfigurationError("SMTP not configured"),
                ):
            response = client.post("/register", data={
                "full_name": "Pending Resident",
                "email": email,
                "password": "Resident@1234",
                "confirm_password": "Resident@1234",
                "address": "Purok 4",
            }, follow_redirects=True)
        self.assertIn("email verification could not be sent", response.get_data(as_text=True).lower())
        conn = dbh.get_db()
        user = conn.execute(
            "SELECT id,email_verified FROM users WHERE email=?", (email,)
        ).fetchone()
        self.assertEqual(user["email_verified"], 0)
        conn.execute(
            "UPDATE registration_email_verifications SET sent_at=? WHERE user_id=?",
            ((datetime.now() - timedelta(seconds=2)).isoformat(), user["id"]),
        )
        conn.commit()
        conn.close()
        self.assertEqual(
            client.post("/login", data={"email": email, "password": "Resident@1234"}).status_code,
            403,
        )
        with patch.dict(os.environ, {"REGISTRATION_OTP_RESEND_SECONDS": "1"}), \
                patch.object(A.email_service, "send_email") as send_email:
            response = client.post(
                "/verify-registration/resend", follow_redirects=True
            )
        self.assertEqual(send_email.call_count, 1)
        code = re.search(r"is (\d{6})", send_email.call_args.args[2]).group(1)
        self.assertEqual(
            client.post("/verify-registration", data={"email_otp": code}).location,
            "/login",
        )
        conn = dbh.get_db()
        self.assertEqual(conn.execute(
            "SELECT email_verified FROM users WHERE id=?", (user["id"],)
        ).fetchone()["email_verified"], 1)
        conn.close()

    def test_resident_staff_and_admin_login_without_otp(self):
        accounts = (
            ("mfa-resident@example.test", "resident", None, "/dashboard", 0),
            ("mfa-bpso@example.test", "staff", "bpso", "/staff/dashboard", 0),
            ("mfa-treasurer@example.test", "staff", "treasurer", "/staff/dashboard", 0),
            ("mfa-secretary@example.test", "staff", "secretary", "/staff/dashboard", 0),
            ("mfa-admin@example.test", "admin", None, "/admin/dashboard", 0),
            ("test-admin@example.test", "admin", None, "/admin/dashboard", 1),
        )
        conn = dbh.get_db()
        user_ids = {}
        for email, role, office, _, is_super_admin in accounts:
            password_hash = generate_password_hash("Resident@1234")
            if is_super_admin:
                conn.execute(
                    "UPDATE users SET password_hash=?,must_reset_pw=0,email_verified=0 "
                    "WHERE email=?",
                    (password_hash, email),
                )
                user_ids[email] = conn.execute(
                    "SELECT id FROM users WHERE email=?", (email,)
                ).fetchone()["id"]
                continue
            user_ids[email] = conn.execute(
                "INSERT INTO users "
                "(full_name,email,password_hash,role,office,is_super_admin,email_verified) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    role.title(),
                    email,
                    password_hash,
                    role,
                    office,
                    is_super_admin,
                    1 if role == "resident" else 0,
                ),
            ).lastrowid
        conn.commit()
        conn.close()
        with patch.object(A.email_service, "send_email") as send_email:
            for email, _, _, destination, _ in accounts:
                client = A.app.test_client()
                response = client.post(
                    "/login", data={"email": email, "password": "Resident@1234"}
                )
                self.assertEqual(response.location, destination)
                self.assertEqual(send_email.call_count, 0)
                with client.session_transaction() as session:
                    self.assertEqual(session["user_id"], user_ids[email])
        rejected = A.app.test_client().post(
            "/login", data={"email": accounts[0][0], "password": "wrong"}
        )
        self.assertEqual(rejected.status_code, 200)
        routes = {rule.rule for rule in A.app.url_map.iter_rules()}
        self.assertNotIn("/verify-otp", routes)
        self.assertNotIn("/verify-otp/resend", routes)

    def test_founding_admin_direct_login_still_requires_initial_password_change(self):
        email = "test-admin@example.test"
        conn = dbh.get_db()
        conn.execute(
            "UPDATE users SET must_reset_pw=1, recovery_email=?, password_hash=? WHERE id=1",
            (email, generate_password_hash("Admin@1234")),
        )
        conn.commit()
        conn.close()

        client = A.app.test_client()
        with patch.object(A.email_service, "send_email") as send_email:
            response = client.post(
                "/login",
                data={"email": email, "password": "Admin@1234"},
            )
        self.assertEqual(response.location, "/first-login-reset")
        send_email.assert_not_called()
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

    def test_admin_password_recovery_uses_registered_email(self):
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
        with patch.dict(os.environ, {"APP_BASE_URL": "https://barangayconnect.example.test"}), \
                patch.object(A.email_service, "send_email") as send_email:
            response = client.post("/forgot-password", data={"email": login_email})
            self.assertEqual(response.status_code, 302)
            self.assertEqual(send_email.call_args.args[0], login_email)
            reset_url = re.search(
                r"https://barangayconnect\.example\.test/reset-password/\S+",
                send_email.call_args.args[2],
            ).group(0)

        password = "RecoveredAdminPassword@123"
        response = client.post(
            urlsplit(reset_url).path,
            data={"password": password, "confirm_password": password},
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
