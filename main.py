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


# ── PLATFORM PUBLISHING ───────────────────────────────────

class PublishRequest(BaseModel):
    post_id:   str = ""
    content:   str = ""
    platforms: list = []
    image_url: str = ""
    user_id:   str = ""

async def publish_to_facebook(content: str, image_url: str, page_token: str, page_id: str) -> dict:
    """Publish to Facebook Page"""
    try:
        async with _httpx.AsyncClient(timeout=30.0) as client:
            if image_url:
                # Post with image
                r = await client.post(
                    f"https://graph.facebook.com/v21.0/{page_id}/photos",
                    data={
                        "url":          image_url,
                        "caption":      content,
                        "access_token": page_token
                    }
                )
            else:
                # Text only post
                r = await client.post(
                    f"https://graph.facebook.com/v21.0/{page_id}/feed",
                    data={
                        "message":      content,
                        "access_token": page_token
                    }
                )
            data = r.json()
            if "id" in data:
                return {"success": True,  "platform": "facebook", "post_id": data["id"]}
            else:
                return {"success": False, "platform": "facebook", "error": data.get("error", {}).get("message", "Unknown error")}
    except Exception as e:
        return {"success": False, "platform": "facebook", "error": str(e)}


async def publish_to_discord(content: str, image_url: str, webhook_url: str) -> dict:
    """Publish to Discord via webhook"""
    try:
        payload = {"content": content}
        if image_url:
            payload["embeds"] = [{"image": {"url": image_url}}]
        async with _httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(webhook_url, json=payload)
            if r.status_code in (200, 204):
                return {"success": True,  "platform": "discord"}
            else:
                return {"success": False, "platform": "discord", "error": f"Status {r.status_code}"}
    except Exception as e:
        return {"success": False, "platform": "discord", "error": str(e)}


async def publish_to_telegram(content: str, image_url: str, bot_token: str, chat_id: str) -> dict:
    """Publish to Telegram channel"""
    try:
        async with _httpx.AsyncClient(timeout=15.0) as client:
            if image_url:
                r = await client.post(
                    f"https://api.telegram.org/bot{bot_token}/sendPhoto",
                    json={"chat_id": chat_id, "photo": image_url, "caption": content}
                )
            else:
                r = await client.post(
                    f"https://api.telegram.org/bot{bot_token}/sendMessage",
                    json={"chat_id": chat_id, "text": content, "parse_mode": "HTML"}
                )
            data = r.json()
            if data.get("ok"):
                return {"success": True,  "platform": "telegram"}
            else:
                return {"success": False, "platform": "telegram", "error": data.get("description", "Unknown error")}
    except Exception as e:
        return {"success": False, "platform": "telegram", "error": str(e)}


async def get_user_connections(user_id: str) -> dict:
    """Get user's platform connections from Supabase"""
    if not SUPABASE_URL:
        return {}
    try:
        async with _httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"{SUPABASE_URL}/rest/v1/autopostleey_connections",
                params={"user_id": f"eq.{user_id}", "select": "*"},
                headers={"apikey": SUPABASE_ANON, "Authorization": f"Bearer {SUPABASE_ANON}"}
            )
            if r.status_code == 200:
                rows = r.json()
                return {row["platform"]: row for row in rows}
    except Exception as e:
        print(f"Get connections error: {e}")
    return {}


async def update_post_status(post_id: str, status: str, error: str = None):
    """Update post status in Supabase"""
    if not SUPABASE_URL or not post_id:
        return
    try:
        update_data = {
            "status":    status,
            "posted_at": datetime.utcnow().isoformat() if status == "posted" else None
        }
        if error:
            update_data["error_msg"] = error
        async with _httpx.AsyncClient(timeout=10.0) as client:
            await client.patch(
                f"{SUPABASE_URL}/rest/v1/autopostleey_posts",
                params={"id": f"eq.{post_id}"},
                headers={
                    "apikey":        SUPABASE_ANON,
                    "Authorization": f"Bearer {SUPABASE_ANON}",
                    "Content-Type":  "application/json"
                },
                json=update_data
            )
    except Exception as e:
        print(f"Update post status error: {e}")


@app.post("/publish")
async def publish_post(req: PublishRequest, user: dict = Depends(get_current_user)):
    """Publish a post to selected platforms"""
    if not req.content:
        raise HTTPException(400, "Post content is required")

    user_id = user.get("user_id") or req.user_id
    if not user_id or user_id in ("guest", "anon"):
        raise HTTPException(401, "Authentication required")

    # Get user's platform connections
    connections = await get_user_connections(user_id)

    results  = []
    success  = 0
    failed   = 0

    for platform in req.platforms:
        conn = connections.get(platform, {})

        if platform == "facebook":
            page_token = conn.get("access_token", os.getenv("FB_PAGE_ACCESS_TOKEN", ""))
            page_id    = conn.get("page_id",       os.getenv("FB_PAGE_ID", ""))
            if page_token and page_id:
                result = await publish_to_facebook(req.content, req.image_url, page_token, page_id)
            else:
                result = {"success": False, "platform": "facebook", "error": "Not connected"}

        elif platform == "discord":
            webhook = conn.get("webhook_url", os.getenv("DISCORD_WEBHOOK", ""))
            if webhook:
                result = await publish_to_discord(req.content, req.image_url, webhook)
            else:
                result = {"success": False, "platform": "discord", "error": "Not connected"}

        elif platform == "telegram":
            bot_token = conn.get("bot_token", os.getenv("TELEGRAM_BOT_TOKEN", ""))
            chat_id   = conn.get("chat_id",   os.getenv("TELEGRAM_CHAT_ID", ""))
            if bot_token and chat_id:
                result = await publish_to_telegram(req.content, req.image_url, bot_token, chat_id)
            else:
                result = {"success": False, "platform": "telegram", "error": "Not connected"}

        else:
            result = {"success": False, "platform": platform, "error": "Platform coming soon"}

        results.append(result)
        if result["success"]: success += 1
        else: failed += 1

    # Update post status in Supabase
    final_status = "posted" if success > 0 else "failed"
    errors = [r.get("error") for r in results if not r["success"]]
    await update_post_status(req.post_id, final_status, "; ".join(filter(None, errors)))

    return {
        "status":  final_status,
        "success": success,
        "failed":  failed,
        "results": results
    }


# ── PLATFORM CONNECTIONS ──────────────────────────────────

class ConnectionRequest(BaseModel):
    platform:     str = ""
    access_token: str = ""
    page_id:      str = ""
    page_name:    str = ""
    webhook_url:  str = ""
    bot_token:    str = ""
    chat_id:      str = ""


@app.post("/connections/save")
async def save_connection(req: ConnectionRequest, user: dict = Depends(get_current_user)):
    """Save a platform connection for the user"""
    user_id = user.get("user_id")
    if not user_id or user_id in ("guest", "anon"):
        raise HTTPException(401, "Authentication required")

    conn_data = {
        "user_id":      user_id,
        "platform":     req.platform,
        "access_token": req.access_token or None,
        "page_id":      req.page_id      or None,
        "page_name":    req.page_name    or None,
        "webhook_url":  req.webhook_url  or None,
        "bot_token":    req.bot_token    or None,
        "chat_id":      req.chat_id      or None,
        "connected_at": datetime.utcnow().isoformat(),
    }

    try:
        async with _httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(
                f"{SUPABASE_URL}/rest/v1/autopostleey_connections",
                headers={
                    "apikey":        SUPABASE_ANON,
                    "Authorization": f"Bearer {SUPABASE_ANON}",
                    "Content-Type":  "application/json",
                    "Prefer":        "resolution=merge-duplicates"
                },
                json=conn_data
            )
        if r.status_code in (200, 201):
            return {"success": True, "platform": req.platform}
        else:
            raise HTTPException(500, f"Failed to save: {r.text}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/connections")
async def get_connections(user: dict = Depends(get_current_user)):
    """Get all platform connections for the user"""
    user_id = user.get("user_id")
    if not user_id or user_id in ("guest", "anon"):
        return []
    connections = await get_user_connections(user_id)
    # Return safe version (no tokens)
    safe = {}
    for platform, conn in connections.items():
        safe[platform] = {
            "platform":  conn.get("platform"),
            "page_name": conn.get("page_name"),
            "connected": True,
            "connected_at": conn.get("connected_at")
        }
    return safe
