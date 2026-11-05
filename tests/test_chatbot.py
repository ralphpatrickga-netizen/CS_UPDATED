import unittest

import chatbot


class ChatbotIntentTests(unittest.TestCase):
    def test_clearance_variants_are_understood(self):
        replies = [
            chatbot.get_reply(None, {"id": 1}, "How can I get a barangay clearance?"),
            chatbot.get_reply(None, {"id": 1}, "Paano kumuha ng barangay clearance?"),
            chatbot.get_reply(None, {"id": 1}, "Ano requirements para sa clearance?"),
        ]
        for reply in replies:
            self.assertIn("Barangay Clearance", reply)
            self.assertIn("New Request", reply)

    def test_follow_up_requirement_uses_context(self):
        history = [
            {"role": "user", "content": "How can I get a barangay clearance?"},
            {"role": "assistant", "content": "I can help with that. Would you like the requirements or the application process?"},
        ]
        reply = chatbot.get_reply(None, {"id": 1}, "Requirements.", history)
        self.assertIn("Barangay Clearance", reply)
        self.assertIn("valid id", reply.lower())

    def test_compaint_help_supports_taglish(self):
        reply = chatbot.get_reply(None, {"id": 1}, "Paano mag-file ng complaint?")
        self.assertIn("File Complaint", reply)
        self.assertIn("complaint", reply.lower())

    def test_request_status_guidance_is_helpful(self):
        reply = chatbot.get_reply(None, {"id": 1}, "What is the status of my request?")
        self.assertIn("My Requests", reply)
        self.assertIn("reference number", reply.lower())


if __name__ == "__main__":
    unittest.main()
