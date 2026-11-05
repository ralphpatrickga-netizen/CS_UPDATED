"""
Seeds BarangayConnect with demo accounts and sample data so you can try
every role immediately. Safe to re-run: it wipes and rebuilds the database.

Usage:  python3 seed.py
"""
import os
import random
from datetime import datetime, timedelta

from werkzeug.security import generate_password_hash

import db as dbh
import priority

random.seed(42)


def _make_placeholder_upload():
    """Creates a tiny real JPEG so seeded 'Valid ID' attachment links actually work."""
    upload_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads", "requests")
    os.makedirs(upload_dir, exist_ok=True)
    path = os.path.join(upload_dir, "seed_placeholder.jpg")
    if not os.path.isfile(path):
        try:
            from PIL import Image
            img = Image.new("RGB", (400, 250), color=(15, 35, 64))
            img.save(path, "JPEG")
        except Exception:
            # Fallback: write minimal valid JPEG bytes if Pillow isn't available.
            with open(path, "wb") as f:
                f.write(bytes.fromhex(
                    "ffd8ffe000104a46494600010100000100010000ffdb004300030202020202"
                    "03020202030303030406040404040408060605070908080807080807090a0c"
                    "0a090b09080a0e0b0c0d0d0e0d0d0e0e0e0e0e0effc00011080001000103012"
                    "200021101031101ffc4001f0000010501010101010100000000000000000102"
                    "0304050607080910110b12ffda000c03010002110311003f00bf00ffd9"
                ))
    return path


def run():
    demo_passwords = {
        "admin": os.environ.get("DEMO_ADMIN_PASSWORD", ""),
        "staff": os.environ.get("DEMO_STAFF_PASSWORD", ""),
        "resident": os.environ.get("DEMO_RESIDENT_PASSWORD", ""),
    }
    for role, password in demo_passwords.items():
        if (
            len(password) < 8
            or not any(character.isalpha() for character in password)
            or not any(character.isdigit() for character in password)
        ):
            raise ValueError(
                f"DEMO_{role.upper()}_PASSWORD must have at least 8 characters, "
                "one letter, and one number."
            )

    if os.path.isfile(dbh.DB_PATH):
        os.remove(dbh.DB_PATH)
    dbh.init_db()
    _make_placeholder_upload()
    conn = dbh.get_db()

    # ---- accounts -----------------------------------------------------
    admin_id = conn.execute(
        "INSERT INTO users (full_name, email, recovery_email, password_hash, role) "
        "VALUES (?,?,?,?,?)",
        (
            "Elena Ramirez",
            "admin@barangayconnect.ph",
            "admin@barangayconnect.ph",
            generate_password_hash(demo_passwords["admin"]),
            "admin",
        ),
    ).lastrowid

    staff_defs = [
        ("Marco Villanueva", "marco.staff@barangayconnect.ph", "treasurer"),
        ("Ramon Bautista", "ramon.staff@barangayconnect.ph", "secretary"),
        ("Liza Fernandez", "liza.staff@barangayconnect.ph", "bpso"),
    ]
    office_labels = {"treasurer": "Barangay Treasurer", "secretary": "Barangay Secretary", "bpso": "BPSO (Public Safety)"}
    staff_by_office = {}
    for name, email, office in staff_defs:
        sid = conn.execute(
            "INSERT INTO users (full_name, email, password_hash, role, office, position, must_reset_pw, created_by) "
            "VALUES (?,?,?,?,?,?,0,?)",
            (
                name,
                email,
                generate_password_hash(demo_passwords["staff"]),
                "staff",
                office,
                office_labels[office],
                admin_id,
            ),
        ).lastrowid
        staff_by_office.setdefault(office, []).append(sid)
        dbh.log_action(conn, {"id": admin_id, "full_name": "Elena Ramirez"}, "Created staff account", target=email,
                       details=f"office={office}")

    resident_defs = [
        ("Juan Dela Cruz", "juan.resident@example.com", "Purok 3, Barangay San Isidro", "0917 111 2222"),
        ("Maria Santos", "maria.resident@example.com", "Purok 5, Barangay San Isidro", "0917 333 4444"),
        ("Pedro Reyes", "pedro.resident@example.com", "Purok 1, Barangay San Isidro", "0917 555 6666"),
    ]
    resident_ids = []
    for name, email, address, contact in resident_defs:
        rid = conn.execute(
            "INSERT INTO users (full_name, email, password_hash, role, address, contact_number) VALUES (?,?,?,?,?,?)",
            (
                name,
                email,
                generate_password_hash(demo_passwords["resident"]),
                "resident",
                address,
                contact,
            ),
        ).lastrowid
        resident_ids.append(rid)
        dbh.log_action(conn, {"id": rid, "full_name": name}, "Resident self-registered", target=email)

    conn.commit()

    # ---- sample requests -----------------------------------------------
    req_types = ["Certificate of Indigency", "Certificate of Residency", "Barangay Clearance"]
    residency_purposes = ["Employment", "School Enrollment", "Government ID Application", "Bank Requirement"]
    request_office = {"Certificate of Indigency": "secretary", "Certificate of Residency": "secretary", "Barangay Clearance": "treasurer"}
    indigency_purposes = [
        "Financial Assistance", "Medical Assistance", "Burial Assistance",
        "Scholarship ID", "Philhealth", "First Time Job Seeker",
    ]
    clearance_purposes = [
        ("Local Employment", 50), ("International Employment", 200),
        ("Postal I.D.", 100), ("Bank Requirement", 100),
        ("Application for Business", 200),
        ("Student / Indigent / Senior Citizen / Scholarship / PWD", 0),
    ]
    civil_statuses = ["Single", "Married", "Widowed", "Separated"]
    req_statuses = ["Submitted", "Under Review", "Processing", "Approved", "Completed", "Rejected"]
    for i in range(18):
        resident_id = random.choice(resident_ids)
        rtype = random.choice(req_types)
        registered_voter = 0
        if rtype == "Certificate of Indigency":
            purpose = random.choice(indigency_purposes)
            computed_fee = 0
            num_copies = random.choice([1, 1, 2, 3])
        elif rtype == "Certificate of Residency":
            purpose = random.choice(residency_purposes)
            registered_voter = random.choice([0, 1])
            computed_fee = 0 if registered_voter else 50
            num_copies = 1
        else:
            purpose, computed_fee = random.choice(clearance_purposes)
            num_copies = 1
        status = req_statuses[i % len(req_statuses)]
        days_ago = random.randint(0, 40)
        created = (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")
        ref = dbh.gen_reference("REQ")
        assigned = random.choice(staff_by_office[request_office[rtype]]) if status != "Submitted" else None
        remarks = "Verified against submitted documents." if status in ("Approved", "Completed") else (
            "Missing valid ID, resident notified." if status == "Rejected" else ""
        )
        contact_number = "0917" + str(random.randint(1000000, 9999999))
        request_address = f"Purok {random.randint(1,5)}, Barangay San Isidro"
        civil_status = random.choice(civil_statuses)
        age = random.randint(19, 70)
        req_id = conn.execute(
            "INSERT INTO requests (reference_no, resident_id, request_type, purpose, contact_number, "
            "request_address, civil_status, age, computed_fee, num_copies, status, assigned_staff_id, "
            "remarks, created_at, updated_at, registered_voter) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ref, resident_id, rtype, purpose, contact_number, request_address, civil_status, age,
             computed_fee, num_copies, status, assigned, remarks, created, created, registered_voter),
        ).lastrowid
        conn.execute(
            "INSERT INTO request_files (request_id, file_type, filename, stored_name) VALUES (?,?,?,?)",
            (req_id, "valid_id", "sample_valid_id.jpg", "seed_placeholder.jpg"),
        )
        dbh.notify(conn, resident_id, f"Your request {ref} ({rtype}) is now {status}.")
        if assigned:
            staff_row = conn.execute("SELECT id, full_name FROM users WHERE id=?", (assigned,)).fetchone()
            dbh.log_action(conn, staff_row, f"Updated request status to {status}", target=ref)

    # ---- sample complaints ----------------------------------------------
    comp_statuses = ["Pending", "Under Review", "Scheduled", "Resolved", "Closed", "Rejected"]
    sample_complaints = [
        ("Noise Complaint", "Loud videoke every night past midnight, neighbors can't sleep.", "Purok 2"),
        ("Illegal Parking", "A delivery van keeps blocking the street corner, hard for other vehicles to pass.", "Purok 4"),
        ("Sanitation", "Uncollected garbage has been piling up outside the sari-sari store for a week.", "Purok 1"),
        ("Neighbor Dispute", "Ongoing argument between two households over a shared fence line.", "Purok 3"),
        ("Public Safety", "Broken streetlight has left the alley completely dark at night, residents worried about safety.", "Purok 5"),
        ("Public Safety", "A fire broke out near a residence, neighbors are trying to help put it out.", "Purok 2"),
        ("Domestic Concern", "Loud fighting and shouting heard from a neighboring household, may involve violence.", "Purok 3"),
        ("Others", "Stray dogs roaming the street have been chasing children on their way to school.", "Purok 1"),
        ("Illegal Parking", "Tricycle terminal operating illegally blocks the barangay road during rush hour.", "Purok 4"),
        ("Sanitation", "Clogged drainage causing flooding whenever it rains.", "Purok 5"),
    ]
    for i, (category, description, location) in enumerate(sample_complaints):
        resident_id = random.choice(resident_ids)
        status = comp_statuses[i % len(comp_statuses)]
        days_ago = random.randint(0, 40)
        created = (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")
        ref = dbh.gen_reference("CMP")
        assigned = random.choice(staff_by_office["bpso"]) if status != "Pending" else None
        remarks = "Coordinated with both parties; issue addressed." if status in ("Resolved", "Closed") else ""
        auto_priority, priority_reason = priority.classify(category, description, location)
        conn.execute(
            "INSERT INTO complaints (reference_no, resident_id, category, description, location, priority, "
            "priority_reason, status, assigned_staff_id, remarks, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (ref, resident_id, category, description, location, auto_priority, priority_reason, status,
             assigned, remarks, created, created),
        )
        dbh.notify(conn, resident_id, f"Your complaint {ref} ({category}) is now {status}.")

    dbh.set_setting(conn, "barangay_name", "Barangay San Isidro")
    dbh.set_setting(conn, "barangay_address", "Barangay Government Complex, Faustino St., Isidora Hills")
    dbh.set_setting(conn, "barangay_main_number", "0931-129-4693")
    dbh.set_setting(conn, "barangay_base_number", "0915-224-4744")
    dbh.set_setting(conn, "barangay_annex_number", "0928-315-9183")
    dbh.set_setting(conn, "barangay_annex2_number", "0929-566-5537")
    dbh.set_setting(conn, "traffic_number", "0977-737-9947")
    dbh.set_setting(conn, "bhert_number", "0975-489-7605")
    dbh.set_setting(conn, "mrcf_number", "0936-593-8539")
    dbh.set_setting(conn, "dogpound_number", "0948-449-1796")
    dbh.set_setting(conn, "pnp_number", "0963-686-5659")
    dbh.set_setting(conn, "bfp_number", "0976-550-0362")
    dbh.set_setting(conn, "bdrrm_number", "0931-129-4694")

    sample_announcements = [
        ("Barangay Hall closed on October 15", "In observance of a local holiday, the Barangay Hall will be closed on October 15. Online requests and complaints can still be filed; processing resumes the next business day."),
        ("New requirement for Certificate of Indigency", "Starting this month, please bring a valid ID showing your barangay address when claiming a free Certificate of Indigency."),
    ]
    for title, message in sample_announcements:
        conn.execute(
            "INSERT INTO announcements (title, message, posted_by_name) VALUES (?,?,?)",
            (title, message, "Elena Ramirez"),
        )
        for rid in resident_ids:
            dbh.notify(conn, rid, f"Barangay Announcement: {title}")

    conn.commit()
    conn.close()
    print("Database seeded successfully.\n")
    print("=" * 60)
    print("TEST ACCOUNTS (passwords supplied via environment variables)")
    print("=" * 60)
    print(f"{'Role':<10} {'Email':<32} {'Password'}")
    print(f"{'Admin':<10} {'admin@barangayconnect.ph':<32} DEMO_ADMIN_PASSWORD (+ email OTP; SMTP required)")
    for name, email, office in staff_defs:
        print(f"{'Staff':<10} {email:<32} DEMO_STAFF_PASSWORD  ({office_labels[office]})")
    for name, email, *_ in resident_defs:
        print(f"{'Resident':<10} {email:<32} DEMO_RESIDENT_PASSWORD")
    print("=" * 60)


if __name__ == "__main__":
    run()
