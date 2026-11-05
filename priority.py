"""
BarangayConnect Complaint Priority Detection.

Classifies a new complaint into High / Normal based on its category and
free-text description, so elevated-safety issues surface at the top of the
staff/admin queue instead of waiting in line behind routine complaints.

This is keyword + category based (works offline, zero setup, fully
deterministic and explainable — every classification records *why* it was
flagged). Staff/Admin can always override the result manually; once they do,
the override is marked so the system won't silently re-classify it later.
"""
import re

# High-priority signals that should remain in the app
HIGH_KEYWORDS = [
    "fight", "away", "nag-aaway", "threat", "banta", "harass", "harassment",
    "inaano", "drunk", "lasing", "intoxicated", "trespass", "pumasok",
    "theft", "nagnakaw", "stealing", "break-in", "binreak", "vandalism",
    "sira", "injury", "nasugatan", "hurt", "hospital", "ospital", "chase",
    "hinahabol", "gang", "barkada", "riot", "gulo",
    "fire", "sunog", "gun", "baril", "weapon", "armed", "knife", "kutsilyo",
    "stab", "saksak", "blood", "dugo", "unconscious", "hindi gumagalaw",
    "dying", "patay", "namatay", "assault", "sinaktan", "rape", "ginahasa",
    "abuse", "child abuse", "domestic violence", "violence", "marahas",
    "threat to kill", "papatayin", "drowning", "nalulunod", "explosion",
    "sumasabog", "gas leak", "electrocute", "na-kuryente", "collapse",
    "nawalan ng malay", "seizure", "overdose", "suicide", "nagbabantang",
    "shooting", "binaril", "hostage", "kidnap",
]

# Categories that default to a higher baseline even without alarming keywords
HIGH_BASELINE_CATEGORIES = {"Public Safety", "Domestic Concern"}

PRIORITY_LABELS = ["High", "Normal"]


def _find_matches(text, keywords):
    lower = text.lower()
    return [kw for kw in keywords if re.search(r"\b" + re.escape(kw) + r"\b", lower)]


def classify(category, description, location=""):
    """Returns (priority, reason) where priority is one of PRIORITY_LABELS
    and reason is a short human-readable explanation for the audit trail."""
    text = f"{description} {location}"

    high_hits = _find_matches(text, HIGH_KEYWORDS)
    if high_hits:
        return "High", f"Flagged High: mentions \"{high_hits[0]}\"."

    if category in HIGH_BASELINE_CATEGORIES:
        return "High", f"Flagged High: category '{category}' is treated as elevated-priority by default."

    return "Normal", "No elevated-priority signals detected; routed as a routine complaint."
