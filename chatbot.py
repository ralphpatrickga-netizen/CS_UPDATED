"""
BarangayConnect Resident Assistant.

Works with zero setup: it answers questions about the resident's own
requests/complaints straight from the database, and covers common FAQs with
canned answers drawn from the PRD. If an ANTHROPIC_API_KEY environment
variable is present (and this machine has internet access), open-ended
questions that don't match a reference number or FAQ are instead answered by
Claude, grounded with a short system prompt describing the barangay's
services.
"""
import os
import re
from unicodedata import normalize

import requests

REF_PATTERN = re.compile(r"\b(REQ|CMP)-\d{4}-[A-Z0-9]{5}\b", re.IGNORECASE)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")

REQUEST_STATUS_LABELS = [
    "Submitted", "Under Review", "Processing", "Approved", "Rejected", "Completed"
]
COMPLAINT_STATUS_LABELS = [
    "Pending", "Under Review", "Scheduled", "Resolved", "Closed", "Rejected"
]

DOC_ALIASES = {
    "Barangay Clearance": [
        "barangay clearance", "clearance", "barangay clearances", "clearance requirement",
        "requirements for clearance", "ano ang requirements para sa clearance", "clearance requirements"
    ],
    "Certificate of Residency": [
        "certificate of residency", "residency", "proof of residency", "certificate residency",
        "residency requirements", "ano requirements sa residency", "residency fee"
    ],
    "Certificate of Indigency": [
        "certificate of indigency", "indigency", "cert of indigency", "indigency requirements",
        "requirements for indigency", "ano requirements sa indigency"
    ],
}

COMPLAINT_ALIASES = [
    "complaint", "file a complaint", "file complaint", "report a problem", "report complaint",
    "how to complain", "complaint category", "paano mag-file ng complaint", "mag-file ng complaint",
    "report a concern", "concern"
]

FAQS = [
    (
        ["barangay clearance", "clearance requirement", "clearance requirements", "requirements for clearance"],
        "A Barangay Clearance is typically used for employment, business, or general legal purposes. To request one, go to New Request, choose 'Barangay Clearance', and select the purpose. Bring a valid ID when you claim it at the Barangay Treasurer Office.",
    ),
    (
        ["certificate of residency", "proof of residency", "residency", "residency requirements"],
        "A Certificate of Residency confirms you live in the barangay and is often needed for school enrollment or government transactions. Submit it under New Request → 'Certificate of Residency'. The fee is ₱50, and it is free for registered voters. Pay first at the Barangay Treasurer Office, then claim at the Barangay Secretary Office with your official receipt.",
    ),
    (
        ["business clearance"],
        "For business use, request a Barangay Clearance under New Request and choose the purpose 'Application for Business'. Claim it at the Barangay Treasurer Office, where the fee is based on the selected purpose.",
    ),
    (
        ["indigency", "certificate of indigency", "indigency requirements"],
        "A Certificate of Indigency certifies that a resident belongs to a low-income household, often for medical or scholarship assistance. Submit it under New Request → 'Certificate of Indigency'.",
    ),
    (
        ["file a complaint", "how to complain", "complaint category", "file complaint", "mag-file ng complaint", "paano mag-file ng complaint"],
        "To file a complaint: go to 'File Complaint', choose a complaint category, describe what happened, include the location, and attach photos if needed. You will receive a reference number and can track it later.",
    ),
    (
        ["how long", "processing time", "how many days", "tagal", "processing"],
        "Processing time depends on the request type and queue, but you can track live status updates such as Submitted, Under Review, Processing, Approved, Rejected, and Completed.",
    ),
    (
        ["forgot password", "reset password", "can't log in", "cannot log in", "nakalimutan password", "forgot my password"],
        "Use the 'Forgot password?' link on the login page for resident accounts. Staff accounts are recovered by the barangay Administrator instead.",
    ),
    (
        ["hotline", "emergency", "emergency number", "911", "hotline number"],
        "For emergencies, contact your local emergency hotline or call 911. This portal supports non-emergency barangay requests and complaints only.",
    ),
    (
        ["hello", "hi", "kumusta", "magandang", "good morning", "good afternoon"],
        "Kumusta! I can help you check a request or complaint status, explain document requirements, or guide you on filing a complaint. Ask me anything in English, Filipino, or Taglish.",
    ),
]

FALLBACK_REPLY = (
    "I can help with request and complaint tracking, document requirements, filing complaints, and account help. "
    "Try asking about Barangay Clearance, Certificate of Residency, Certificate of Indigency, filing a complaint, or your own request status."
)

SYSTEM_PROMPT = (
    "You are the BarangayConnect Resident Assistant, a helpful chat assistant embedded in a Philippine barangay's citizen request and complaint management portal. "
    "You help residents understand available services, how to file requests or complaints, and what to expect from the process. "
    "If asked about a specific reference number, instruct them to paste it so the system can look it up directly. "
    "Keep answers short, warm, practical, and grounded in barangay process, not legal advice."
)


def _normalize(text):
    text = text or ""
    text = normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-z0-9\s]", " ", text.lower())
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _contains_any(text, keywords):
    return any(keyword in text for keyword in keywords)


def _lookup_reference(conn, user, ref):
    ref = ref.upper()
    if ref.startswith("REQ"):
        row = conn.execute(
            "SELECT * FROM requests WHERE reference_no=? AND resident_id=?", (ref, user["id"])
        ).fetchone()
        if not row:
            return f"I couldn't find a request with reference {ref} under your account."
        remarks = f" Staff remarks: {row['remarks']}" if row["remarks"] else ""
        return (
            f"Request {ref} ({row['request_type']}) is currently **{row['status']}**, "
            f"last updated {row['updated_at']}.{remarks}"
        )
    row = conn.execute(
        "SELECT * FROM complaints WHERE reference_no=? AND resident_id=?", (ref, user["id"])
    ).fetchone()
    if not row:
        return f"I couldn't find a complaint with reference {ref} under your account."
    remarks = f" Staff remarks: {row['remarks']}" if row["remarks"] else ""
    return (
        f"Complaint {ref} ({row['category']}) is currently **{row['status']}**, "
        f"last updated {row['updated_at']}.{remarks}"
    )


def _extract_document_context(history):
    if not history:
        return None
    for item in reversed(history):
        text = _normalize(item.get("content") or item.get("message") or "")
        for doc_name, aliases in DOC_ALIASES.items():
            if _contains_any(text, aliases):
                return doc_name
        if "clearance" in text:
            return "Barangay Clearance"
        if "residency" in text:
            return "Certificate of Residency"
        if "indigency" in text:
            return "Certificate of Indigency"
    return None


def _document_requirements_response(doc_name):
    if doc_name == "Barangay Clearance":
        return (
            "Barangay Clearance is generally used for employment, business, or legal purposes. "
            "To request it, go to New Request and choose 'Barangay Clearance', then select the purpose. "
            "Bring a valid ID when you claim it at the Barangay Treasurer Office."
        )
    if doc_name == "Certificate of Residency":
        return (
            "A Certificate of Residency confirms that you live in the barangay and is often needed for school enrollment or government transactions. "
            "The fee is ₱50, and registered voters may be exempt. Pay first at the Barangay Treasurer Office, then claim the certificate at the Barangay Secretary Office with the official receipt."
        )
    if doc_name == "Certificate of Indigency":
        return (
            "A Certificate of Indigency certifies that a resident belongs to a low-income household, often for medical or scholarship assistance. "
            "Submit your request under New Request → 'Certificate of Indigency'."
        )
    return (
        "I can help with Barangay Clearance, Certificate of Residency, and Certificate of Indigency. "
        "Choose the document you need and I can explain the requirements or process."
    )


def _document_process_response(doc_name):
    if doc_name == "Barangay Clearance":
        return (
            "To request a Barangay Clearance, open New Request, choose 'Barangay Clearance', pick a purpose, and submit the form. "
            "The fee depends on the selected purpose, and you claim the document at the Barangay Treasurer Office."
        )
    if doc_name == "Certificate of Residency":
        return (
            "To request a Certificate of Residency, go to New Request and choose 'Certificate of Residency'. "
            "Fill in the details, pay the ₱50 fee at the Barangay Treasurer Office if required, and claim the document at the Barangay Secretary Office with your receipt."
        )
    if doc_name == "Certificate of Indigency":
        return (
            "To request a Certificate of Indigency, go to New Request and choose 'Certificate of Indigency', then follow the form and upload the required attachments for the selected purpose."
        )
    return (
        "The document process starts in New Request. Pick the document type, complete the form, upload any required files, and wait for the barangay office to update the status."
    )


def _complaint_help_response():
    return (
        "To file a complaint, go to File Complaint, choose a category such as Noise Complaint, Neighbor Dispute, Illegal Parking, Sanitation, Public Safety, Domestic Concern, or Others, describe what happened, include the location, and attach photos if needed. "
        "After submission, you will receive a reference number to track the complaint."
    )


def _status_help_response():
    return (
        "You can track your own request or complaint in My Requests and My Complaints. If you have a reference number, paste it here and I can look it up under your account. "
        "Examples: REQ-2026-ABCD1 or CMP-2026-ABCD1."
    )


def _status_meaning_response(kind):
    if kind == "request":
        return (
            "Request statuses are: Submitted, Under Review, Processing, Approved, Rejected, and Completed. "
            "These show the current step in the barangay processing queue."
        )
    return (
        "Complaint statuses are: Pending, Under Review, Scheduled, Resolved, Closed, and Rejected. "
        "They explain how the complaint is being handled and whether it is still active or already closed."
    )


def _resolve_follow_up(message, history):
    text = _normalize(message)
    recent_doc = _extract_document_context(history)
    doc_name = _detect_document_name(text) or recent_doc

    if _contains_any(text, ["requirements", "requirement", "reqs", "ano ang requirements", "ano requirements"]):
        if doc_name:
            return _document_requirements_response(doc_name)
        return (
            "I can explain the requirements for Barangay Clearance, Certificate of Residency, and Certificate of Indigency. "
            "Which one do you need?"
        )
    if _contains_any(text, ["process", "how to apply", "paano mag apply", "procedure", "application process", "paano kumuha"]):
        if doc_name:
            return _document_process_response(doc_name)
        return (
            "To request a document, go to New Request, choose the type of document, fill out the form, upload any required attachments, and wait for the office update. "
            "Which document do you want to request?"
        )
    if _contains_any(text, ["status", "track", "update", "follow up", "anong status", "update ng complaint", "update ng request"]):
        return _status_help_response()
    return None


def _match_faq(message):
    lower = _normalize(message)
    for keywords, answer in FAQS:
        if any(k in lower for k in keywords):
            return answer
    return None


def _classify_message(message, history=None):
    text = _normalize(message)
    if not text:
        return None
    history = history or []
    recent_doc = _extract_document_context(history)

    if (_contains_any(text, ["complaint", "file complaint", "report problem", "report concern", "mag file ng complaint", "paano mag file ng complaint"]) or
        ("complaint" in text and "status" in text) or
        ("complaint" in text and "how" in text)):
        return {"type": "complaint"}

    if _contains_any(text, ["barangay clearance", "clearance", "certificate of residency", "residency", "indigency", "certificate of indigency"]) or recent_doc:
        return {"type": "document", "document": recent_doc or _detect_document_name(text)}

    if _contains_any(text, ["status", "track", "update", "follow up", "anong status", "update ko"]) or _contains_any(text, ["request", "complaint"]):
        return {"type": "status"}

    if _contains_any(text, ["password", "login", "log in", "forgot password", "nakalimutan password", "account"]) :
        return {"type": "account"}

    if _contains_any(text, ["hi", "hello", "kumusta", "magandang", "good morning", "good afternoon"]):
        return {"type": "greeting"}

    return None


def _detect_document_name(text):
    for doc_name, aliases in DOC_ALIASES.items():
        if _contains_any(text, aliases):
            return doc_name
    if "clearance" in text:
        return "Barangay Clearance"
    if "residency" in text:
        return "Certificate of Residency"
    if "indigency" in text:
        return "Certificate of Indigency"
    return None


def _call_claude(message, history):
    if not ANTHROPIC_API_KEY:
        return None
    try:
        messages = history + [{"role": "user", "content": message}]
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": ANTHROPIC_MODEL,
                "max_tokens": 400,
                "system": SYSTEM_PROMPT,
                "messages": messages,
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        parts = [b["text"] for b in data.get("content", []) if b.get("type") == "text"]
        return "\n".join(parts).strip() or None
    except Exception:
        return None


def get_reply(conn, user, message, history=None):
    """Returns the assistant's reply text. history is a list of
    {"role": "user"|"assistant", "content": str} for Claude context, oldest first."""
    history = history or []
    message = (message or "").strip()
    if not message:
        return FALLBACK_REPLY

    refs = REF_PATTERN.findall(message)
    if refs:
        full_ref = REF_PATTERN.search(message).group(0)
        return _lookup_reference(conn, user, full_ref)

    follow_up = _resolve_follow_up(message, history)
    if follow_up:
        return follow_up

    faq = _match_faq(message)
    if faq:
        return faq

    intent = _classify_message(message, history)
    if intent:
        if intent["type"] == "document":
            doc_name = intent.get("document") or _detect_document_name(_normalize(message))
            if doc_name:
                if "requirements" in _normalize(message) or "requirement" in _normalize(message):
                    return _document_requirements_response(doc_name)
                if "process" in _normalize(message) or "paano" in _normalize(message) or "how to apply" in _normalize(message):
                    return _document_process_response(doc_name)
                return _document_requirements_response(doc_name)
        if intent["type"] == "complaint":
            return _complaint_help_response()
        if intent["type"] == "status":
            return _status_help_response()
        if intent["type"] == "account":
            return "Use the 'Forgot password?' link on the login page for resident accounts. Staff accounts are handled by the barangay Administrator."
        if intent["type"] == "greeting":
            return "Kumusta! I can help you with request status, complaint filing, or document requirements. Ask me in English, Filipino, or Taglish."

    if "status meaning" in _normalize(message) or "ano ang ibig sabihin" in _normalize(message):
        return _status_meaning_response("complaint" if "complaint" in _normalize(message) else "request")

    claude_reply = _call_claude(message, history)
    if claude_reply:
        return claude_reply

    return (
        "I can help with request tracking, filing a complaint, and document requirements for Barangay Clearance, Certificate of Residency, and Certificate of Indigency. "
        "Try one of these: 'How do I get a barangay clearance?', 'Paano mag-file ng complaint?', or 'Check my request status.'"
    )
