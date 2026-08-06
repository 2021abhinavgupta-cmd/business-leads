"""
SES Sender — delivers cold emails through Amazon SES.

Message construction, unsubscribe headers and the suppression check all live
in BaseSender; this file is only the AWS-specific transport and quota lookup.
"""

import boto3
from botocore.exceptions import ClientError

import config
from emailer.base_sender import BaseSender, TransientSendError


class SESSender(BaseSender):
    """Send cold emails via AWS Simple Email Service."""

    provider_name = "SES"

    def __init__(self):
        super().__init__(from_email=config.FROM_EMAIL)
        self.client = boto3.client(
            "ses",
            region_name=config.AWS_REGION,
            aws_access_key_id=config.AWS_ACCESS_KEY,
            aws_secret_access_key=config.AWS_SECRET_KEY,
        )

    def _transport_send(self, msg, to_email: str) -> None:
        try:
            self.client.send_raw_email(
                Source=self.from_email,
                Destinations=[to_email],
                RawMessage={"Data": msg.as_string()},
            )
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            error_message = e.response.get("Error", {}).get("Message", "")

            if "Daily message quota exceeded" in error_message or "LimitExceeded" in error_code:
                raise Exception(f"SES Daily sending quota exceeded: {error_message}") from e

            if error_code == "MessageRejected":
                raise Exception(f"SES rejected the message: {error_message}") from e

            if error_code == "Throttling":
                raise TransientSendError(f"SES throttled the send: {error_message}") from e

            raise Exception(f"SES error ({error_code}): {error_message}") from e

    def check_quota(self) -> dict:
        """
        Get the remaining SES daily sending quota.

        Returns:
            A dict containing 'Max24HourSend', 'SentLast24Hours', and 'Remaining'.
        """
        try:
            response = self.client.get_send_quota()
            max_send = response.get("Max24HourSend", 0.0)
            sent = response.get("SentLast24Hours", 0.0)
            return {
                "Max24HourSend": max_send,
                "SentLast24Hours": sent,
                "Remaining": max(0.0, max_send - sent),
            }
        except Exception as e:
            print(f"Error getting SES quota: {e}")
            return {"Max24HourSend": 0.0, "SentLast24Hours": 0.0, "Remaining": 0.0}

    # Kept so any older caller (or a copy-pasted script) still works.
    def check_ses_quota(self) -> dict:
        return self.check_quota()
