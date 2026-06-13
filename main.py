"""
Autopostleey FastAPI Backend
Handles PayPal payments, user plans, and post scheduling
"""
import os
from datetime import datetime
from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import httpx as _httpx

app = FastAPI(title="Autopostleey API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://autopostleey.com",
        "https://www.autopostleey.com",
        "http://localhost:3000",
        "http://localhost:8080",
        "*"
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

# ── ENV VARS ──────────────────────────────────────────────
SUPABASE_URL     = os.getenv("SUPABASE_URL", "")
SUPABASE_ANON    = os.getenv("SUPABASE_ANON_KEY", "")
SUPABASE_SERVICE = os.getenv("SUPABASE_SERVICE_KEY", "")
PAYPAL_CLIENT_ID = os.getenv("PAYPAL_CLIENT_ID", "")
PAYPAL_SECRET    = os.getenv("PAYPAL_SECRET", "")
PAYPAL_BASE      = "https://api-m.paypal.com"
DEV_TOKEN        = os.getenv("API_TOKEN", "autopostleey2025")

# ── AUTH ──────────────────────────────────────────────────
async def get_current_user(
    authorization: str = Header(None),
    x_api_token:   str = Header(None)
):
    if x_api_token and x_api_token == DEV_TOKEN:
        return {"user_id": "dev", "plan": "agency"}

    if not authorization or not authorization.startswith("Bearer "):
        return {"user_id": "guest", "plan": "free"}

    token = authorization.split(" ", 1)[1]

    # Decode JWT to get email
    try:
        import base64, json as _json
        payload = token.split(".")[1]
        payload += "=" * (4 - len(payload) % 4)
        decoded = _json.loads(base64.b64decode(payload))
        if decoded.get("email") == "alexclaor@gmail.com":
            return {"user_id": decoded.get("sub", "admin"), "plan": "agency"}
    except Exception:
        pass

    if not SUPABASE_URL:
        return {"user_id": "guest", "plan": "free"}

    try:
        async with _httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(
                f"{SUPABASE_URL}/auth/v1/user",
                headers={"Authorization": f"Bearer {token}", "apikey": SUPABASE_ANON}
            )
            if r.status_code != 200:
                return {"user_id": "guest", "plan": "free"}
        user = r.json()
        meta = user.get("user_metadata", {})
        return {
            "user_id": user["id"],
            "email":   user.get("email"),
            "plan":    meta.get("plan", "free"),
        }
    except Exception:
        return {"user_id": "guest", "plan": "free"}


# ── PAYPAL ────────────────────────────────────────────────
async def get_paypal_token():
    async with _httpx.AsyncClient() as client:
        r = await client.post(
            f"{PAYPAL_BASE}/v1/oauth2/token",
            auth=(PAYPAL_CLIENT_ID, PAYPAL_SECRET),
            data={"grant_type": "client_credentials"},
            headers={"Content-Type": "application/x-www-form-urlencoded"}
        )
        return r.json().get("access_token")


class CreateOrderRequest(BaseModel):
    user_id: str = ""
    email:   str = ""
    plan:    str = "starter"


@app.post("/paypal/create-order")
async def create_paypal_order(
    req: CreateOrderRequest,
    user: dict = Depends(get_current_user)
):
    plan = req.plan or "starter"

    plan_config = {
        "starter": ("9.00",  "Autopostleey Starter - Monthly"),
        "growth":  ("19.00", "Autopostleey Growth - Monthly"),
        "agency":  ("49.00", "Autopostleey Agency - Monthly"),
    }
    amount, description = plan_config.get(plan, ("9.00", "Autopostleey Starter - Monthly"))

    token = await get_paypal_token()
    async with _httpx.AsyncClient() as client:
        r = await client.post(
            f"{PAYPAL_BASE}/v2/checkout/orders",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type":  "application/json"
            },
            json={
                "intent": "CAPTURE",
                "purchase_units": [{
                    "amount": {"currency_code": "USD", "value": amount},
                    "description": description
                }],
                "application_context": {
                    "return_url": "https://autopostleey.com/dashboard?payment=success",
                    "cancel_url": "https://autopostleey.com/dashboard?payment=cancel",
                    "brand_name": "Autopostleey",
                    "user_action": "PAY_NOW"
                }
            }
        )
    data = r.json()
    order_id    = data.get("id")
    approve_url = next(
        (l["href"] for l in data.get("links", []) if l["rel"] == "approve"),
        None
    )
    return {"order_id": order_id, "approve_url": approve_url}


@app.post("/paypal/capture/{order_id}")
async def capture_paypal_order(
    order_id: str,
    user: dict = Depends(get_current_user)
):
    token = await get_paypal_token()
    async with _httpx.AsyncClient() as client:
        r = await client.post(
            f"{PAYPAL_BASE}/v2/checkout/orders/{order_id}/capture",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type":  "application/json"
            }
        )
    data = r.json()

    if data.get("status") == "COMPLETED":
        # Determine plan from description
        units = data.get("purchase_units", [{}])
        desc  = units[0].get("description", "") if units else ""
        if "Growth"  in desc: new_plan = "growth"
        elif "Agency" in desc: new_plan = "agency"
        else:                  new_plan = "starter"

        # Update user plan in Supabase
        if SUPABASE_URL and user.get("user_id") not in ("dev", "guest"):
            async with _httpx.AsyncClient() as client:
                await client.put(
                    f"{SUPABASE_URL}/auth/v1/admin/users/{user['user_id']}",
                    headers={
                        "apikey":        SUPABASE_SERVICE,
                        "Authorization": f"Bearer {SUPABASE_SERVICE}",
                        "Content-Type":  "application/json"
                    },
                    json={"user_metadata": {"plan": new_plan}}
                )

        return {
            "status":  "success",
            "message": f"Upgraded to {new_plan}!",
            "plan":    new_plan
        }

    return {"status": "failed", "message": "Payment not completed"}


# ── HEALTH ────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {
        "status":  "ok",
        "service": "Autopostleey API",
        "time":    datetime.utcnow().isoformat()
    }


# ── USER PLAN ─────────────────────────────────────────────
@app.get("/user/plan")
async def get_user_plan(user: dict = Depends(get_current_user)):
    return {
        "user_id": user.get("user_id"),
        "plan":    user.get("plan", "free"),
        "email":   user.get("email", "")
    }
