#!/usr/bin/env python3
"""Run the daily event screener and email the Markdown report."""

from __future__ import annotations

import argparse
import datetime as dt
import os
import smtplib
import ssl
from email.message import EmailMessage

import event_bottom_fishing
from reporting import write_outputs


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_ENV_PATH = os.path.join(ROOT, ".env")
DEFAULT_TO = "lvyongyu@gmail.com"


def load_env_file(path: str) -> None:
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def generate_report(top: int, lookback_days: int, max_news: int) -> tuple[str, str]:
    args = event_bottom_fishing.parse_args([])
    args.top = top
    args.lookback_days = lookback_days
    args.max_news = max_news
    candidates = event_bottom_fishing.scan(args)
    if not candidates:
        raise RuntimeError("No candidates found; check network access or widen the universe/lookback window.")

    os.makedirs(event_bottom_fishing.OUTPUT_DIR, exist_ok=True)
    today = dt.datetime.now().strftime("%Y-%m-%d")
    path_prefix = os.path.join(event_bottom_fishing.OUTPUT_DIR, f"daily_event_bottom_fishing_{today}")
    return write_outputs(candidates, path_prefix)


def send_email(subject: str, body: str, markdown_path: str, json_path: str, to_address: str) -> None:
    host = required_env("SMTP_HOST")
    port = int(os.environ.get("SMTP_PORT", "587"))
    username = required_env("SMTP_USERNAME")
    password = required_env("SMTP_PASSWORD")
    from_address = os.environ.get("SMTP_FROM") or username

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = from_address
    message["To"] = to_address
    message.set_content(body)

    for path, mime_subtype in ((markdown_path, "markdown"), (json_path, "json")):
        with open(path, "rb") as handle:
            message.add_attachment(
                handle.read(),
                maintype="application",
                subtype=mime_subtype,
                filename=os.path.basename(path),
            )

    context = ssl.create_default_context()
    with smtplib.SMTP(host, port, timeout=30) as server:
        server.starttls(context=context)
        server.login(username, password)
        server.send_message(message)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--to", default=os.environ.get("SMTP_TO") or DEFAULT_TO)
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--lookback-days", type=int, default=14)
    parser.add_argument("--max-news", type=int, default=8)
    parser.add_argument("--env-file", default=DEFAULT_ENV_PATH)
    args = parser.parse_args()

    load_env_file(args.env_file)
    markdown_path, json_path = generate_report(args.top, args.lookback_days, args.max_news)
    with open(markdown_path, "r", encoding="utf-8") as handle:
        report_body = handle.read()

    today = dt.datetime.now().strftime("%Y-%m-%d")
    subject = f"Daily US event-driven stock watchlist - {today}"
    send_email(subject, report_body, markdown_path, json_path, args.to)
    print(f"Sent daily report to {args.to}")
    print(f"Markdown: {markdown_path}")
    print(f"JSON: {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
