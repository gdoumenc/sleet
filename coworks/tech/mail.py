import os
import smtplib
from email.message import EmailMessage
from typing import List

from aws_xray_sdk.core import xray_recorder
from chalice import ChaliceViewError

from ..coworks import TechMicroService


class MailMicroService(TechMicroService):

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.smtp_server = self.smtp_login = self.smtp_passwd = None

    def _check_env_vars(self):
        self.smtp_server = os.getenv('SMTP_SERVER')
        if not self.smtp_server:
            raise EnvironmentError('SMTP_SERVER not defined in environment')
        self.smtp_login = os.getenv('SMTP_LOGIN')
        if not self.smtp_login:
            raise EnvironmentError('SMTP_LOGIN not defined in environment')
        self.smtp_passwd = os.getenv('SMTP_PASSWD')
        if not self.smtp_passwd:
            raise EnvironmentError('SMTP_PASSWD not defined in environment')

    def post_send(self, subject="", from_addr: str = None, to_addrs: List[str] = None, body="", starttls=False):
        """Send mail."""

        # Ckecks parameters
        self._check_env_vars()
        from_addr = from_addr or os.getenv('from_addr')
        if not from_addr:
            raise ChaliceViewError("From address not defined (from_addr:str)")
        to_addrs = to_addrs or os.getenv('to_addrs')
        if not to_addrs:
            raise ChaliceViewError("To addresses not defined (to_addrs:[str])")

        # Creates email
        try:
            msg = EmailMessage()
            msg['Subject'] = subject
            msg['From'] = from_addr
            msg['To'] = ', '.join(to_addrs)
            msg.set_content(body)
        except Exception as e:
            raise ChaliceViewError(f"Cannot create email message (Error: {str(e)}).")

        # Send emails
        try:
            with smtplib.SMTP(self.smtp_server) as server:
                if starttls:
                    server.starttls()
                server.login(self.smtp_login, self.smtp_passwd)

                subsegment = xray_recorder.begin_subsegment(f"SMTP sending")
                try:
                    subsegment.put_metadata('message', msg.as_string())
                    server.send_message(msg)
                finally:
                    xray_recorder.end_subsegment()

            return f"Mail sent to {msg['To']}"
        except smtplib.SMTPAuthenticationError:
            raise ChaliceViewError("Wrong username/password.")
        except Exception as e:
            raise ChaliceViewError(f"Cannot send email message (Error: {str(e)}).")
