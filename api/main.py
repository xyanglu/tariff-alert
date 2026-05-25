"""
Tariff Alert - FastAPI Backend
Signup, Stripe checkout, subscriber management, unsubscribe.
"""

import os
import uuid
import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import stripe
import uvicorn

app = FastAPI(title="Tariff Alert API")

# CORS for GitHub Pages
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Config ---
DB_PATH = Path(os.getenv("DB_PATH", "tariff_alert.db"))
STRIPE_SECRET = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PRICE_ID = os.getenv("STRIPE_PRICE_ID", "")
SITE_URL = os.getenv("SITE_URL", "https://xyanglu.github.io/tariff-alert")
SENDER_EMAIL = os.getenv("SENDER_EMAIL", "tariffalert@gmail.com")

stripe.api_key = STRIPE_SECRET

# --- DB ---
def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS subscribers (
        id TEXT PRIMARY KEY,
        email TEXT UNIQUE NOT NULL,
        name TEXT DEFAULT '',
        company TEXT DEFAULT '',
        plan TEXT DEFAULT 'free',
        stripe_customer_id TEXT,
        stripe_subscription_id TEXT,
        subscribed_at TEXT,
        cancelled_at TEXT,
        active INTEGER DEFAULT 1,
        created_at TEXT NOT NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS unsubscribes (
        token TEXT PRIMARY KEY,
        email TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""")
    conn.commit()
    return conn

def generate_unsub_token(email: str) -> str:
    raw = f"{email}-{uuid.uuid4()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


# --- Routes ---

@app.get("/health")
def health():
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}


@app.post("/api/signup")
def signup(data: dict):
    email = (data.get("email") or "").strip().lower()
    name = (data.get("name") or "").strip()
    company = (data.get("company") or "").strip()
    plan = data.get("plan", "free").strip().lower()

    if not email or "@" not in email:
        raise HTTPException(400, "Valid email required")
    if plan not in ("free", "paid"):
        raise HTTPException(400, "Plan must be free or paid")

    conn = get_db()
    try:
        existing = conn.execute("SELECT id, plan, active FROM subscribers WHERE email = ?", (email,)).fetchone()
        if existing:
            if existing["active"]:
                return {"status": "already_subscribed", "plan": existing["plan"]}
            else:
                conn.execute("UPDATE subscribers SET active = 1, cancelled_at = NULL, plan = ? WHERE email = ?", (plan, email))
                conn.commit()
                token = generate_unsub_token(email)
                conn.execute("INSERT OR REPLACE INTO unsubscribes (token, email, created_at) VALUES (?, ?, ?)",
                             (token, email, datetime.now(timezone.utc).isoformat()))
                conn.commit()
                return {"status": "reactivated", "plan": plan}

        sub_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO subscribers (id, email, name, company, plan, active, created_at) VALUES (?, ?, ?, ?, ?, 1, ?)",
            (sub_id, email, name, company, plan, datetime.now(timezone.utc).isoformat())
        )
        token = generate_unsub_token(email)
        conn.execute("INSERT INTO unsubscribes (token, email, created_at) VALUES (?, ?, ?)",
                     (token, email, datetime.now(timezone.utc).isoformat()))
        conn.commit()
        return {"status": "subscribed", "plan": plan, "email": email}
    finally:
        conn.close()


@app.post("/api/checkout")
def create_checkout(data: dict):
    email = (data.get("email") or "").strip().lower()
    name = (data.get("name") or "").strip()

    if not email or "@" not in email:
        raise HTTPException(400, "Valid email required")
    if not STRIPE_SECRET or not STRIPE_PRICE_ID:
        raise HTTPException(500, "Stripe not configured")

    conn = get_db()
    try:
        existing = conn.execute("SELECT stripe_customer_id FROM subscribers WHERE email = ?", (email,)).fetchone()
        customer_id = existing["stripe_customer_id"] if existing else None
    finally:
        conn.close()

    try:
        if customer_id:
            session = stripe.checkout.Session.create(
                customer=customer_id,
                mode="subscription",
                payment_method_types=["card"],
                line_items=[{"price": STRIPE_PRICE_ID, "quantity": 1}],
                success_url=f"{SITE_URL}/?checkout=success",
                cancel_url=f"{SITE_URL}/?checkout=cancelled",
                metadata={"email": email, "name": name},
            )
        else:
            session = stripe.checkout.Session.create(
                mode="subscription",
                payment_method_types=["card"],
                customer_email=email,
                line_items=[{"price": STRIPE_PRICE_ID, "quantity": 1}],
                success_url=f"{SITE_URL}/?checkout=success",
                cancel_url=f"{SITE_URL}/?checkout=cancelled",
                metadata={"email": email, "name": name},
            )
        return {"url": session.url, "session_id": session.id}
    except stripe.error.StripeError as e:
        raise HTTPException(400, str(e))


@app.post("/webhook/stripe")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")

    if STRIPE_WEBHOOK_SECRET:
        try:
            event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
        except stripe.error.SignatureVerificationError:
            raise HTTPException(400, "Invalid signature")
    else:
        import json
        event = json.loads(payload)

    conn = get_db()
    try:
        if event["type"] == "checkout.session.completed":
            session = event["data"]["object"]
            email = session.get("customer_email") or session.get("metadata", {}).get("email", "")
            name = session.get("metadata", {}).get("name", "")
            customer_id = session.get("customer")
            subscription_id = session.get("subscription")

            if email:
                existing = conn.execute("SELECT id FROM subscribers WHERE email = ?", (email,)).fetchone()
                if existing:
                    conn.execute(
                        "UPDATE subscribers SET plan = 'paid', stripe_customer_id = ?, stripe_subscription_id = ?, subscribed_at = ?, active = 1, cancelled_at = NULL WHERE email = ?",
                        (customer_id, subscription_id, datetime.now(timezone.utc).isoformat(), email)
                    )
                else:
                    sub_id = str(uuid.uuid4())
                    conn.execute(
                        "INSERT INTO subscribers (id, email, name, plan, stripe_customer_id, stripe_subscription_id, subscribed_at, active, created_at) VALUES (?, ?, ?, 'paid', ?, ?, ?, 1, ?)",
                        (sub_id, email, name, customer_id, subscription_id, datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat())
                    )
                conn.commit()

        elif event["type"] == "customer.subscription.deleted":
            sub = event["data"]["object"]
            sub_id = sub.get("id")
            if sub_id:
                conn.execute("UPDATE subscribers SET plan = 'free', cancelled_at = ?, active = 1 WHERE stripe_subscription_id = ?",
                             (datetime.now(timezone.utc).isoformat(), sub_id))
                conn.commit()

        elif event["type"] == "invoice.payment_failed":
            sub = event["data"]["object"]
            cust_id = sub.get("customer")
            if cust_id:
                conn.execute("UPDATE subscribers SET plan = 'free' WHERE stripe_customer_id = ?", (cust_id,))
                conn.commit()
    finally:
        conn.close()

    return {"status": "ok"}


@app.get("/unsubscribe/{token}")
def unsubscribe_page(token: str):
    conn = get_db()
    try:
        row = conn.execute("SELECT email FROM unsubscribes WHERE token = ?", (token,)).fetchone()
    finally:
        conn.close()

    if not row:
        return HTMLResponse("<h1>Invalid or expired link</h1>", status_code=404)

    return HTMLResponse(f"""<!DOCTYPE html>
<html><head><title>Unsubscribe</title></head>
<body style="font-family: Arial; max-width: 500px; margin: 50px auto; text-align: center;">
    <h2>Unsubscribe from Tariff Alert</h2>
    <p>You are about to unsubscribe <strong>{row['email']}</strong></p>
    <form method="POST" action="/unsubscribe/{token}">
        <button type="submit" style="padding: 10px 20px; background: #e74c3c; color: white; border: none; border-radius: 4px; cursor: pointer; font-size: 16px;">
            Yes, unsubscribe me
        </button>
    </form>
    <p style="color: #666; font-size: 13px; margin-top: 20px;">Tariff Alert</p>
</body></html>""")


@app.post("/unsubscribe/{token}")
def unsubscribe_confirm(token: str):
    conn = get_db()
    try:
        row = conn.execute("SELECT email FROM unsubscribes WHERE token = ?", (token,)).fetchone()
        if not row:
            return HTMLResponse("<h1>Invalid link</h1>", status_code=404)
        email = row["email"]
        conn.execute("UPDATE subscribers SET active = 0, cancelled_at = ? WHERE email = ?",
                     (datetime.now(timezone.utc).isoformat(), email))
        conn.commit()
    finally:
        conn.close()

    return HTMLResponse(f"""<!DOCTYPE html>
<html><head><title>Unsubscribed</title></head>
<body style="font-family: Arial; max-width: 500px; margin: 50px auto; text-align: center;">
    <h2>You have been unsubscribed</h2>
    <p>{email} will no longer receive Tariff Alert emails.</p>
    <p style="color: #666; font-size: 13px; margin-top: 20px;">
        <a href="https://xyanglu.github.io/tariff-alert/">Back to Tariff Alert</a>
    </p>
</body></html>""")


@app.get("/api/subscribers")
def list_subscribers():
    conn = get_db()
    try:
        rows = conn.execute("SELECT email, name, company, plan, active, created_at FROM subscribers ORDER BY created_at DESC").fetchall()
    finally:
        conn.close()
    return {"subscribers": [dict(r) for r in rows], "count": len(rows)}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
