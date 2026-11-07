"""
Safely provision the single Founding Administrator.

The initial password is read only from FOUNDING_ADMIN_PASSWORD. It is hashed
before it is written, and the account must change it on first login.
"""

import os
import re
import sqlite3
import sys

from werkzeug.security import check_password_hash, generate_password_hash

import db as dbh


FOUNDING_ADMIN_EMAIL = "ralphpatrickga@gmail.com"


def setup_founding_admin():
    name = os.environ.get("FOUNDING_ADMIN_NAME", "").strip()
    password = os.environ.get("FOUNDING_ADMIN_PASSWORD", "")

    if not name:
        raise ValueError("FOUNDING_ADMIN_NAME is missing.")
    if (
        len(password) < 12
        or not re.search(r"[A-Za-z]", password)
        or not re.search(r"\d", password)
    ):
        raise ValueError(
            "FOUNDING_ADMIN_PASSWORD must have at least 12 characters, "
            "one letter, and one number."
        )

    os.makedirs(os.path.dirname(dbh.DB_PATH), exist_ok=True)
    database_exists = os.path.isfile(dbh.DB_PATH)
    if not database_exists:
        dbh.init_db()
    dbh.migrate_db()

    conn = dbh.get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        founders = conn.execute(
            "SELECT id, full_name, role, is_super_admin, status "
            "FROM users WHERE role='admin' AND is_super_admin=1"
        ).fetchall()
        if len(founders) > 1:
            raise ValueError(
                "Multiple Founding Admin accounts exist; reconcile them manually "
                "before running setup."
            )

        target = conn.execute(
            "SELECT id FROM users WHERE lower(email)=?",
            (FOUNDING_ADMIN_EMAIL,),
        ).fetchone()

        if founders:
            founder = founders[0]
            if founder["role"] != "admin" or not founder["is_super_admin"]:
                raise ValueError("The existing Founding Admin could not be identified safely.")
            if founder["status"] != "active":
                raise ValueError(
                    "The existing Founding Admin account is inactive; no account was changed."
                )
            if target and target["id"] != founder["id"]:
                raise ValueError(
                    "The designated email belongs to another account; no account was changed."
                )

            conn.execute(
                "UPDATE users SET full_name=?, email=?, recovery_email=?, password_hash=?, "
                "must_reset_pw=1, failed_logins=0, locked_until=NULL WHERE id=?",
                (
                    name,
                    FOUNDING_ADMIN_EMAIL,
                    FOUNDING_ADMIN_EMAIL,
                    generate_password_hash(password),
                    founder["id"],
                ),
            )
            action = "Updated Founding Admin account and required initial password change"
        else:
            existing_admin = conn.execute(
                "SELECT id FROM users WHERE role='admin' LIMIT 1"
            ).fetchone()
            if existing_admin:
                raise ValueError(
                    "Administrator accounts exist but none is marked Founding Admin; "
                    "no account was promoted or changed."
                )
            if target:
                raise ValueError(
                    "The designated email belongs to a non-admin account; "
                    "no account was changed."
                )

            conn.execute(
                "INSERT INTO users (full_name, email, recovery_email, password_hash, role, "
                "is_super_admin, status, must_reset_pw, failed_logins, locked_until) "
                "VALUES (?, ?, ?, ?, 'admin', 1, 'active', 1, 0, NULL)",
                (
                    name,
                    FOUNDING_ADMIN_EMAIL,
                    FOUNDING_ADMIN_EMAIL,
                    generate_password_hash(password),
                ),
            )
            action = "Created Founding Admin account and required initial password change"

        dbh.log_action(
            conn,
            None,
            action,
            target=FOUNDING_ADMIN_EMAIL,
        )
        verified = conn.execute(
            "SELECT id, role, is_super_admin, status, must_reset_pw, "
            "recovery_email, password_hash "
            "FROM users WHERE lower(email)=?",
            (FOUNDING_ADMIN_EMAIL,),
        ).fetchall()
        founder_count = conn.execute(
            "SELECT COUNT(*) FROM users WHERE role='admin' AND is_super_admin=1"
        ).fetchone()[0]
        if (
            len(verified) != 1
            or verified[0]["role"] != "admin"
            or not verified[0]["is_super_admin"]
            or verified[0]["status"] != "active"
            or not verified[0]["must_reset_pw"]
            or verified[0]["recovery_email"] != FOUNDING_ADMIN_EMAIL
            or not check_password_hash(verified[0]["password_hash"], password)
            or founder_count != 1
        ):
            raise RuntimeError("Post-setup account verification failed.")

        conn.commit()
        print("[SETUP] Founding Admin account configured and verified.")
        print("[SETUP] Email and recovery email are set to the designated address.")
        print("[SETUP] The initial password must be changed on first login.")
    finally:
        if conn.in_transaction:
            conn.rollback()
        conn.close()


if __name__ == "__main__":
    try:
        setup_founding_admin()
    except (OSError, sqlite3.Error, ValueError, RuntimeError) as error:
        print(f"[SETUP ERROR] {error}", file=sys.stderr)
        sys.exit(1)
