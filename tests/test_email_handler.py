"""
Unit tests for the SIR email integration Lambda handler.
"""

import sys
import os
import json
from unittest.mock import patch

# Add lambda/ directory to path so we can import email_handler directly
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambda"))

from email_handler import (
    parse_email,
    extract_case_id,
    extract_action,
    strip_reply_text,
    handler,
)


# --- parse_email tests ---


def _build_raw_email(from_addr, subject, body):
    """Helper to build a raw MIME email string."""
    return (
        f"From: {from_addr}\r\n"
        f"To: sir-cases@example.com\r\n"
        f"Subject: {subject}\r\n"
        f"Content-Type: text/plain; charset=utf-8\r\n"
        f"\r\n"
        f"{body}"
    )


class TestParseEmail:
    def test_basic_email(self):
        raw = _build_raw_email(
            "user@example.com",
            "[SIR-1234567890] Update",
            "This is my update.",
        )
        result = parse_email(raw)
        assert result["from"] == "user@example.com"
        assert result["subject"] == "[SIR-1234567890] Update"
        assert result["body"] == "This is my update."

    def test_strips_quoted_reply(self):
        raw = _build_raw_email(
            "user@example.com",
            "[SIR-1234567890] Re: Incident",
            "New info here.\n\nOn Mon, Jan 1, 2025, someone wrote:\n> old text",
        )
        result = parse_email(raw)
        assert "New info here" in result["body"]
        assert "old text" not in result["body"]

    def test_strips_original_message_marker(self):
        raw = _build_raw_email(
            "user@example.com",
            "[SIR-1234567890] Re: Incident",
            "Fresh content.\n\n--- Original Message ---\nOld stuff here",
        )
        result = parse_email(raw)
        assert "Fresh content" in result["body"]
        assert "Old stuff" not in result["body"]

    def test_empty_body(self):
        raw = _build_raw_email(
            "user@example.com",
            "[SIR-1234567890] Empty",
            "",
        )
        result = parse_email(raw)
        assert result["body"] == ""

    def test_display_name_in_from(self):
        raw = (
            "From: John Doe <john@example.com>\r\n"
            "To: sir-cases@example.com\r\n"
            "Subject: [SIR-1234567890] Update\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            "\r\n"
            "Some update."
        )
        result = parse_email(raw)
        assert "john@example.com" in result["from"]

    def test_multipart_email_extracts_plain_text(self):
        raw = (
            "From: user@example.com\r\n"
            "To: sir-cases@example.com\r\n"
            "Subject: [SIR-1234567890] Update\r\n"
            "MIME-Version: 1.0\r\n"
            "Content-Type: multipart/alternative; boundary=boundary123\r\n"
            "\r\n"
            "--boundary123\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            "\r\n"
            "Plain text body.\r\n"
            "--boundary123\r\n"
            "Content-Type: text/html; charset=utf-8\r\n"
            "\r\n"
            "<html><body>HTML body</body></html>\r\n"
            "--boundary123--"
        )
        result = parse_email(raw)
        assert "Plain text body" in result["body"]
        assert "HTML body" not in result["body"]

    def test_bytes_input(self):
        raw = _build_raw_email(
            "user@example.com",
            "[SIR-1234567890] Bytes test",
            "Bytes content.",
        )
        result = parse_email(raw.encode("utf-8"))
        assert result["body"] == "Bytes content."


# --- extract_case_id tests ---


class TestExtractCaseId:
    def test_valid_case_id(self):
        assert extract_case_id("[SIR-1234567890] Some subject") == "1234567890"

    def test_long_case_id(self):
        assert extract_case_id("[SIR-12345678901234] Info") == "12345678901234"

    def test_no_case_id(self):
        assert extract_case_id("Random subject line") is None

    def test_case_id_in_reply_subject(self):
        assert extract_case_id("Re: [SIR-9876543210] Incident update") == "9876543210"

    def test_case_id_with_fwd_prefix(self):
        assert extract_case_id("Fwd: [SIR-1111111111] Info") == "1111111111"

    def test_too_short_case_id(self):
        assert extract_case_id("[SIR-123] Short ID") is None

    def test_empty_subject(self):
        assert extract_case_id("") is None

    def test_multiple_case_ids_takes_first(self):
        assert extract_case_id("[SIR-1111111111] and [SIR-2222222222]") == "1111111111"


# --- extract_action tests ---


class TestExtractAction:
    def test_default_comment(self):
        result = extract_action("Just a regular comment body.")
        assert result["type"] == "COMMENT"

    def test_update_status(self):
        body = "ACTION: UPDATE_STATUS\nSTATUS: Detection and Analysis"
        result = extract_action(body)
        assert result["type"] == "UPDATE_STATUS"
        assert result["status"] == "Detection and Analysis"

    def test_update_description(self):
        body = "ACTION: UPDATE_DESCRIPTION\nDESCRIPTION: New description here"
        result = extract_action(body)
        assert result["type"] == "UPDATE_DESCRIPTION"
        assert result["description"] == "New description here"

    def test_add_accounts(self):
        body = "ACTION: ADD_ACCOUNTS\nACCOUNTS: 123456789012, 234567890123"
        result = extract_action(body)
        assert result["type"] == "ADD_ACCOUNTS"
        assert result["accounts"] == ["123456789012", "234567890123"]

    def test_add_accounts_zero_pads(self):
        body = "ACTION: ADD_ACCOUNTS\nACCOUNTS: 123123123"
        result = extract_action(body)
        assert result["accounts"] == ["000123123123"]

    def test_invalid_status_falls_back_to_comment(self):
        body = "ACTION: UPDATE_STATUS\nSTATUS: InvalidStatus"
        result = extract_action(body)
        assert result["type"] == "COMMENT"

    def test_action_without_required_param_falls_back(self):
        body = "ACTION: UPDATE_STATUS\n"
        result = extract_action(body)
        assert result["type"] == "COMMENT"

    def test_action_case_insensitive(self):
        body = "action: update_status\nstatus: Detection and Analysis"
        result = extract_action(body)
        assert result["type"] == "UPDATE_STATUS"

    def test_empty_body_returns_comment(self):
        result = extract_action("")
        assert result["type"] == "COMMENT"

    def test_action_with_extra_whitespace(self):
        body = "  ACTION:   UPDATE_DESCRIPTION  \n  DESCRIPTION:   Trimmed desc  "
        result = extract_action(body)
        assert result["type"] == "UPDATE_DESCRIPTION"
        assert result["description"] == "Trimmed desc"

    def test_add_accounts_empty_list(self):
        body = "ACTION: ADD_ACCOUNTS\nACCOUNTS: "
        result = extract_action(body)
        assert result["type"] == "COMMENT"

    def test_unknown_action_falls_back(self):
        body = "ACTION: DELETE_CASE\n"
        result = extract_action(body)
        assert result["type"] == "COMMENT"


# --- strip_reply_text tests ---


class TestStripReplyText:
    def test_strips_on_wrote(self):
        text = "My reply.\n\nOn Mon, Jan 1, 2025, Bob wrote:\n> quoted"
        result = strip_reply_text(text)
        assert "My reply" in result
        assert "quoted" not in result

    def test_no_markers_returns_full(self):
        text = "Just a clean email body."
        assert strip_reply_text(text) == text

    def test_strips_forwarded_from_header(self):
        text = "My notes.\n\nFrom: someone@corp.com\nSent: Monday"
        result = strip_reply_text(text)
        assert "My notes" in result
        assert "someone@corp.com" not in result

    def test_strips_angle_bracket_quotes(self):
        text = "Fresh reply.\n> quoted line 1\n> quoted line 2"
        result = strip_reply_text(text)
        assert "Fresh reply" in result
        assert "quoted line" not in result

    def test_empty_string(self):
        assert strip_reply_text("") == ""


# --- handler integration test ---


class TestHandler:
    @patch("email_handler.s3_client")
    @patch("email_handler.sir_client")
    def test_handler_adds_comment(self, mock_sir, mock_s3):
        mock_sir.get_case.return_value = {"watchers": [{"email": "user@example.com"}]}
        mock_sir.create_case_comment.return_value = {"commentId": "000001"}

        raw_email = _build_raw_email(
            "user@example.com",
            "[SIR-1234567890] Update",
            "Found additional IOCs in the logs.",
        )
        mock_s3.get_object.return_value = {
            "Body": type("Body", (), {"read": lambda self: raw_email.encode("utf-8")})()
        }

        event = {
            "Records": [
                {
                    "Sns": {
                        "Message": json.dumps(
                            {
                                "mail": {},
                                "receipt": {
                                    "action": {
                                        "bucketName": "test-bucket",
                                        "objectKey": "incoming/abc123",
                                    }
                                },
                            }
                        )
                    }
                }
            ]
        }

        result = handler(event, None)
        assert result["statusCode"] == 200

        mock_sir.create_case_comment.assert_called_once_with(
            caseId="1234567890",
            body="Found additional IOCs in the logs.",
        )

    @patch("email_handler.s3_client")
    @patch("email_handler.sir_client")
    def test_handler_rejects_unauthorized_sender(self, mock_sir, mock_s3):
        mock_sir.get_case.return_value = {
            "watchers": [{"email": "authorized@example.com"}]
        }

        raw_email = _build_raw_email(
            "unauthorized@attacker.com",
            "[SIR-1234567890] Update",
            "Malicious update attempt.",
        )
        mock_s3.get_object.return_value = {
            "Body": type("Body", (), {"read": lambda self: raw_email.encode("utf-8")})()
        }

        event = {
            "Records": [
                {
                    "Sns": {
                        "Message": json.dumps(
                            {
                                "mail": {},
                                "receipt": {
                                    "action": {
                                        "bucketName": "test-bucket",
                                        "objectKey": "incoming/abc123",
                                    }
                                },
                            }
                        )
                    }
                }
            ]
        }

        handler(event, None)

        # Should NOT have called create_case_comment
        mock_sir.create_case_comment.assert_not_called()

    @patch("email_handler.sir_client")
    def test_handler_fallback_direct_content(self, mock_sir):
        """Test direct content fallback for Lambda invoke testing."""
        mock_sir.get_case.return_value = {"watchers": [{"email": "user@example.com"}]}
        mock_sir.create_case_comment.return_value = {"commentId": "000001"}

        raw_email = _build_raw_email(
            "user@example.com",
            "[SIR-1234567890] Test",
            "Direct invoke test.",
        )

        event = {"Records": [{"Sns": {"Message": json.dumps({"content": raw_email})}}]}

        result = handler(event, None)
        assert result["statusCode"] == 200
        mock_sir.create_case_comment.assert_called_once()

    @patch("email_handler.s3_client")
    @patch("email_handler.sir_client")
    def test_handler_no_case_id_in_subject(self, mock_sir, mock_s3):
        """Email without [SIR-xxx] in subject should be silently skipped."""
        raw_email = _build_raw_email(
            "user@example.com",
            "No case ID here",
            "Some body text.",
        )
        mock_s3.get_object.return_value = {
            "Body": type("Body", (), {"read": lambda self: raw_email.encode("utf-8")})()
        }

        event = {
            "Records": [
                {
                    "Sns": {
                        "Message": json.dumps(
                            {
                                "mail": {},
                                "receipt": {
                                    "action": {
                                        "bucketName": "test-bucket",
                                        "objectKey": "incoming/abc123",
                                    }
                                },
                            }
                        )
                    }
                }
            ]
        }

        result = handler(event, None)
        assert result["statusCode"] == 200
        mock_sir.get_case.assert_not_called()
        mock_sir.create_case_comment.assert_not_called()

    @patch("email_handler.s3_client")
    @patch("email_handler.sir_client")
    def test_handler_case_not_found(self, mock_sir, mock_s3):
        """Email referencing a non-existent case should be skipped."""
        from botocore.exceptions import ClientError

        mock_sir.get_case.side_effect = ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "Not found"}},
            "GetCase",
        )

        raw_email = _build_raw_email(
            "user@example.com",
            "[SIR-9999999999] Update",
            "Update for missing case.",
        )
        mock_s3.get_object.return_value = {
            "Body": type("Body", (), {"read": lambda self: raw_email.encode("utf-8")})()
        }

        event = {
            "Records": [
                {
                    "Sns": {
                        "Message": json.dumps(
                            {
                                "mail": {},
                                "receipt": {
                                    "action": {
                                        "bucketName": "test-bucket",
                                        "objectKey": "incoming/abc123",
                                    }
                                },
                            }
                        )
                    }
                }
            ]
        }

        result = handler(event, None)
        assert result["statusCode"] == 200
        mock_sir.create_case_comment.assert_not_called()

    @patch("email_handler.s3_client")
    @patch("email_handler.sir_client")
    def test_handler_empty_body_skips_comment(self, mock_sir, mock_s3):
        """Email with empty body should not create a comment."""
        mock_sir.get_case.return_value = {"watchers": [{"email": "user@example.com"}]}

        raw_email = _build_raw_email(
            "user@example.com",
            "[SIR-1234567890] Empty",
            "",
        )
        mock_s3.get_object.return_value = {
            "Body": type("Body", (), {"read": lambda self: raw_email.encode("utf-8")})()
        }

        event = {
            "Records": [
                {
                    "Sns": {
                        "Message": json.dumps(
                            {
                                "mail": {},
                                "receipt": {
                                    "action": {
                                        "bucketName": "test-bucket",
                                        "objectKey": "incoming/abc123",
                                    }
                                },
                            }
                        )
                    }
                }
            ]
        }

        handler(event, None)
        mock_sir.create_case_comment.assert_not_called()

    @patch("email_handler.s3_client")
    @patch("email_handler.sir_client")
    def test_handler_display_name_sender_verified(self, mock_sir, mock_s3):
        """Sender with display name format should still be verified."""
        mock_sir.get_case.return_value = {"watchers": [{"email": "john@example.com"}]}
        mock_sir.create_case_comment.return_value = {"commentId": "000002"}

        raw_email = (
            "From: John Doe <john@example.com>\r\n"
            "To: sir-cases@example.com\r\n"
            "Subject: [SIR-1234567890] Update\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            "\r\n"
            "Update from John."
        )
        mock_s3.get_object.return_value = {
            "Body": type("Body", (), {"read": lambda self: raw_email.encode("utf-8")})()
        }

        event = {
            "Records": [
                {
                    "Sns": {
                        "Message": json.dumps(
                            {
                                "mail": {},
                                "receipt": {
                                    "action": {
                                        "bucketName": "test-bucket",
                                        "objectKey": "incoming/abc123",
                                    }
                                },
                            }
                        )
                    }
                }
            ]
        }

        handler(event, None)
        mock_sir.create_case_comment.assert_called_once()

    @patch("email_handler.s3_client")
    @patch("email_handler.sir_client")
    def test_handler_s3_fetch_failure(self, mock_sir, mock_s3):
        """S3 fetch failure should be handled gracefully."""
        from botocore.exceptions import ClientError

        mock_s3.get_object.side_effect = ClientError(
            {"Error": {"Code": "NoSuchKey", "Message": "Not found"}},
            "GetObject",
        )

        event = {
            "Records": [
                {
                    "Sns": {
                        "Message": json.dumps(
                            {
                                "mail": {},
                                "receipt": {
                                    "action": {
                                        "bucketName": "test-bucket",
                                        "objectKey": "incoming/missing",
                                    }
                                },
                            }
                        )
                    }
                }
            ]
        }

        result = handler(event, None)
        assert result["statusCode"] == 200
        mock_sir.get_case.assert_not_called()

    def test_handler_empty_event(self):
        """Empty event with no records should not crash."""
        result = handler({"Records": []}, None)
        assert result["statusCode"] == 200

    @patch("email_handler.s3_client")
    @patch("email_handler.sir_client")
    def test_handler_multiple_records(self, mock_sir, mock_s3):
        """Handler should process multiple SNS records."""
        mock_sir.get_case.return_value = {"watchers": [{"email": "user@example.com"}]}
        mock_sir.create_case_comment.return_value = {"commentId": "000001"}

        raw_email = _build_raw_email(
            "user@example.com",
            "[SIR-1234567890] Update",
            "First update.",
        )
        mock_s3.get_object.return_value = {
            "Body": type("Body", (), {"read": lambda self: raw_email.encode("utf-8")})()
        }

        sns_msg = json.dumps(
            {
                "mail": {},
                "receipt": {
                    "action": {
                        "bucketName": "test-bucket",
                        "objectKey": "incoming/abc123",
                    }
                },
            }
        )

        event = {
            "Records": [
                {"Sns": {"Message": sns_msg}},
                {"Sns": {"Message": sns_msg}},
            ]
        }

        handler(event, None)
        assert mock_sir.create_case_comment.call_count == 2
