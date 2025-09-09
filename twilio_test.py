#!/usr/bin/env python3
import os
from twilio.rest import Client

TWILIO_SID  = os.getenv("TWILIO_SID", "")
TWILIO_AUTH = os.getenv("TWILIO_AUTH", "")
TWILIO_FROM = os.getenv("TWILIO_FROM", "")
TWILIO_TO   = os.getenv("TWILIO_TO", "")

def client():
    return Client(TWILIO_SID, TWILIO_AUTH)

def send_call():
    c = client()
    call = c.calls.create(
        to=TWILIO_TO,
        from_=TWILIO_FROM,
        twiml="<Response><Say>This is a test call from your GitHub monitor workflow. It works!</Say></Response>"
    )
    print(f"Placed call SID={call.sid}")

def send_sms():
    c = client()
    msg = c.messages.create(
        to=TWILIO_TO,
        from_=TWILIO_FROM,
        body="This is a test SMS from your GitHub monitor workflow."
    )
    print(f"Sent SMS SID={msg.sid}")

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--call", action="store_true")
    p.add_argument("--sms", action="store_true")
    args = p.parse_args()
    if args.call: send_call()
    if args.sms:  send_sms()
    if not (args.call or args.sms):
        print("Use --call or --sms")
