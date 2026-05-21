"""
Lambda handler for processing inbound emails from SES via SNS
and updating AWS Security Incident Response cases.

SES stores the raw email in S3 and sends a notification to SNS.
This Lambda reads the email from S3, parses it, and calls the SIR API.
"""

import json
import re
import logging
from email import policy
from email.parser import BytesParser

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

sir_client = boto3.client("security-ir")
s3_client = boto3.client("s3")

# Regex to extract case ID from email subject: [SIR-1234567890]
CASE_ID_PATTERN = re.compile(r"\[SIR-(\d{10,32})\]")

# Supported actions that can be specified in the email body
VALID_STATUSES = {
    "submitted",
    "detection and analysis",
    "containment, eradication and recovery",
    "post-incident activities",
}


def handler(event, context):
    """
    Entry point. Receives SNS event containing SES email notification.
    """
    for record in event.get("Records", []):
        sns_message = json.loads(record["Sns"]["Message"])
        process_ses_notification(sns_message)

    return {"statusCode": 200}


def process_ses_notification(ses_notification):
    """
    Parse the SES notification and route to the appropriate SIR API call.
    The email is stored in S3 by the SES receipt rule. The SNS notification
    contains the S3 bucket/key reference and email metadata.
    """
    # SES S3 action sends a notification with mail metadata and receipt info
    receipt = ses_notification.get("receipt", {})

    # Get the S3 location of the stored email
    s3_action = receipt.get("action", {})
    bucket = s3_action.get("bucketName", "")
    key = s3_action.get("objectKey", "")

    if not bucket or not key:
        # Fallback: try reading content directly (for direct Lambda invoke testing)
        raw_email = ses_notification.get("content", "")
        if not raw_email:
            logger.warning("No S3 reference or email content in notification")
            return
    else:
        # Fetch the raw email from S3
        try:
            response = s3_client.get_object(Bucket=bucket, Key=key)
            raw_email = response["Body"].read()
        except ClientError as e:
            logger.error("Failed to fetch email from s3://%s/%s: %s", bucket, key, e)
            return

    parsed = parse_email(raw_email)
    logger.info(
        "Parsed email — From: %s, Subject: %s", parsed["from"], parsed["subject"]
    )

    # Extract case ID from subject
    case_id = extract_case_id(parsed["subject"])
    if not case_id:
        logger.warning("No SIR case ID found in subject: %s", parsed["subject"])
        return

    # Verify the case exists
    if not verify_case_exists(case_id):
        logger.error("Case %s not found or not accessible", case_id)
        return

    # Verify sender is authorized (is a watcher on the case)
    if not verify_sender(case_id, parsed["from"]):
        logger.warning("Sender %s is not a watcher on case %s", parsed["from"], case_id)
        return

    # Parse the email body for action keywords or treat as comment
    body = parsed["body"]
    action = extract_action(body)

    if action["type"] == "UPDATE_STATUS":
        update_case_status(case_id, action["status"])
    elif action["type"] == "UPDATE_DESCRIPTION":
        update_case(case_id, description=action["description"])
    elif action["type"] == "ADD_ACCOUNTS":
        update_case(case_id, accounts_to_add=action["accounts"])
    else:
        # Default: add the email body as a comment
        add_comment(case_id, body)


def parse_email(raw_email):
    """
    Parse raw MIME email into structured fields.
    Returns dict with 'from', 'subject', 'body' keys.
    """
    if isinstance(raw_email, str):
        raw_email = raw_email.encode("utf-8")

    msg = BytesParser(policy=policy.default).parsebytes(raw_email)

    # Get plain text body, stripping quoted replies
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                body = part.get_content()
                break
    else:
        body = msg.get_content()

    # Strip common email reply markers
    body = strip_reply_text(body)

    return {
        "from": msg.get("From", ""),
        "subject": msg.get("Subject", ""),
        "body": body.strip(),
    }


def strip_reply_text(body):
    """
    Remove quoted reply text from email body.
    Strips everything after common reply markers.
    """
    # Common patterns that indicate the start of quoted text
    markers = [
        r"\n\s*On .+ wrote:\s*\n",  # "On Mon, Jan 1, 2025, X wrote:"
        r"\n\s*-{3,}\s*Original Message\s*-{3,}",  # "--- Original Message ---"
        r"\n\s*>{1,}",  # Lines starting with >
        r"\n\s*From:\s+",  # "From: someone@..."
    ]

    for marker in markers:
        match = re.search(marker, body, re.IGNORECASE)
        if match:
            body = body[: match.start()]

    return body


def extract_case_id(subject):
    """
    Extract SIR case ID from email subject line.
    Expected format: [SIR-1234567890]
    """
    match = CASE_ID_PATTERN.search(subject)
    return match.group(1) if match else None


def extract_action(body):
    """
    Parse email body for structured action keywords.
    If none found, returns type='COMMENT' (default).
    """
    lines = body.strip().split("\n")
    action_type = None
    params = {}

    for line in lines:
        line = line.strip()
        if line.upper().startswith("ACTION:"):
            action_type = line.split(":", 1)[1].strip().upper()
        elif line.upper().startswith("STATUS:"):
            params["status"] = line.split(":", 1)[1].strip()
        elif line.upper().startswith("DESCRIPTION:"):
            params["description"] = line.split(":", 1)[1].strip()
        elif line.upper().startswith("ACCOUNTS:"):
            raw = line.split(":", 1)[1].strip()
            params["accounts"] = [
                a.strip().zfill(12) for a in raw.split(",") if a.strip()
            ]

    if action_type == "UPDATE_STATUS" and "status" in params:
        status = params["status"].lower()
        if status in VALID_STATUSES:
            return {"type": "UPDATE_STATUS", "status": params["status"]}
        else:
            logger.warning("Invalid status: %s", params["status"])
            return {"type": "COMMENT"}

    if action_type == "UPDATE_DESCRIPTION" and "description" in params:
        return {"type": "UPDATE_DESCRIPTION", "description": params["description"]}

    if action_type == "ADD_ACCOUNTS" and "accounts" in params:
        if params["accounts"]:
            return {"type": "ADD_ACCOUNTS", "accounts": params["accounts"]}

    return {"type": "COMMENT"}


def verify_case_exists(case_id):
    """Check that the SIR case exists and is accessible."""
    try:
        sir_client.get_case(caseId=case_id)
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceNotFoundException":
            return False
        logger.error("Error verifying case %s: %s", case_id, e)
        raise


def verify_sender(case_id, sender_email):
    """
    Verify the sender is authorized to update the case.
    Checks if sender is a watcher on the case OR a member of the
    incident response team configured in the SIR membership.
    Extracts email from 'Display Name <email@domain.com>' format.
    """
    # Extract bare email from "Name <email>" format
    email_match = re.search(r"<(.+?)>", sender_email)
    bare_email = email_match.group(1) if email_match else sender_email.strip()

    try:
        response = sir_client.get_case(caseId=case_id)
        watchers = response.get("watchers", [])
        watcher_emails = {w.get("email", "").lower() for w in watchers}
        if bare_email.lower() in watcher_emails:
            return True

        # Fallback: check if sender is on the incident response team
        return is_ir_team_member(bare_email)
    except ClientError as e:
        logger.error("Error checking watchers for case %s: %s", case_id, e)
        return False


def is_ir_team_member(email):
    """
    Check if the email belongs to a member of the incident response team
    by querying the SIR membership.
    """
    try:
        memberships = sir_client.list_memberships()
        for item in memberships.get("items", []):
            membership_id = item.get("membershipId")
            if not membership_id:
                continue
            membership = sir_client.get_membership(membershipId=membership_id)
            ir_team = membership.get("incidentResponseTeam", [])
            ir_emails = {m.get("email", "").lower() for m in ir_team}
            if email.lower() in ir_emails:
                logger.info(
                    "Sender %s authorized via IR team membership %s",
                    email,
                    membership_id,
                )
                return True
        return False
    except ClientError as e:
        logger.error("Error checking IR team membership: %s", e)
        return False


def add_comment(case_id, body):
    """Add a comment to the SIR case."""
    if not body:
        logger.warning("Empty comment body, skipping")
        return

    # Truncate to API limit
    if len(body) > 12000:
        body = body[:11990] + "\n[truncated]"

    try:
        response = sir_client.create_case_comment(caseId=case_id, body=body)
        logger.info("Added comment %s to case %s", response.get("commentId"), case_id)
    except ClientError as e:
        logger.error("Failed to add comment to case %s: %s", case_id, e)
        raise


def update_case(case_id, description=None, accounts_to_add=None):
    """Update case fields."""
    kwargs = {"caseId": case_id}
    if description:
        kwargs["description"] = description
    if accounts_to_add:
        kwargs["impactedAccountsToAdd"] = accounts_to_add

    try:
        sir_client.update_case(**kwargs)
        logger.info("Updated case %s", case_id)
    except ClientError as e:
        logger.error("Failed to update case %s: %s", case_id, e)
        raise


def update_case_status(case_id, new_status):
    """Update case status."""
    try:
        sir_client.update_case_status(caseId=case_id, caseStatus=new_status)
        logger.info("Updated case %s status to %s", case_id, new_status)
    except ClientError as e:
        logger.error("Failed to update status for case %s: %s", case_id, e)
        raise
