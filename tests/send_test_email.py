#!/usr/bin/env python3
"""
Send a test email to the SIR email integration endpoint via SES.
Usage:
    python send_test_email.py \
        --case-id 1234567890 \
        --to sir-cases@yourdomain.com \
        --from your-verified@email.com \
        --comment "Test comment from email integration"
"""

import argparse
import boto3


def main():
    parser = argparse.ArgumentParser(description="Send test email to SIR integration")
    parser.add_argument("--case-id", required=True, help="SIR case ID")
    parser.add_argument("--to", required=True, help="Recipient address (SES endpoint)")
    parser.add_argument(
        "--from",
        dest="from_addr",
        required=True,
        help="Sender address (must be SES verified)",
    )
    parser.add_argument("--comment", default="Test comment via email integration")
    parser.add_argument("--region", default="us-east-1")
    args = parser.parse_args()

    session = boto3.Session(region_name=args.region)
    ses = session.client("ses")

    subject = f"[SIR-{args.case_id}] Test update"

    ses.send_email(
        Source=args.from_addr,
        Destination={"ToAddresses": [args.to]},
        Message={
            "Subject": {"Data": subject},
            "Body": {"Text": {"Data": args.comment}},
        },
    )

    print(f"Sent test email to {args.to}")
    print(f"Subject: {subject}")
    print(f"Body: {args.comment}")


if __name__ == "__main__":
    main()
