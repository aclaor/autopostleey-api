"""
Autopostleey FastAPI Backend
Handles PayPal payments, user plans, and post scheduling
"""
import os
from datetime import datetime
from fastapi import FastAPI, HTTPException, Depends, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import httpx as _httpx

app = FastAPI(title="Autopostleey API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
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
CF_WORKER        = os.getenv("CF_WORKER_URL", "https://autopostleey-ai.alexclaor.workers.dev")

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

    # Decode JWT locally — no external calls needed
    try:
        import base64 as _b64, json as _json
        # JWT has 3 parts: header.payload.signature
        parts = token.split(".")
        if len(parts) != 3:
            return {"user_id": "guest", "plan": "free"}

        payload_b64 = parts[1]
        # Add padding
        payload_b64 += "=" * (4 - len(payload_b64) % 4)
        decoded = _json.loads(_b64.b64decode(payload_b64.replace("-","+").replace("_","/")))

        user_id = decoded.get("sub", "")
        email   = decoded.get("email", "")
        meta    = decoded.get("user_metadata", {})
        plan    = meta.get("plan", "free") if isinstance(meta, dict) else "free"
        exp     = decoded.get("exp", 0)

        # Check token not expired
        import time
        if exp and exp < time.time():
            return {"user_id": "guest", "plan": "free"}

        if user_id:
            return {
                "user_id": user_id,
                "email":   email,
                "plan":    plan,
            }
    except Exception as e:
        print(f"JWT decode error: {e}")

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
                print(f"Telegram sendPhoto: chat_id={chat_id}, photo={image_url[:80]}")
                r = await client.post(
                    f"https://api.telegram.org/bot{bot_token}/sendPhoto",
                    json={"chat_id": chat_id, "photo": image_url, "caption": content}
                )
                print(f"Telegram sendPhoto response: {r.status_code} {r.text[:200]}")
                # If sendPhoto fails, fall back to text only
                data = r.json()
                if not data.get("ok"):
                    print(f"sendPhoto failed, falling back to text: {data}")
                    r = await client.post(
                        f"https://api.telegram.org/bot{bot_token}/sendMessage",
                        json={"chat_id": chat_id, "text": content + "\n\n🖼 " + image_url, "parse_mode": "HTML"}
                    )
                    data = r.json()
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
                headers={"apikey": SUPABASE_SERVICE, "Authorization": f"Bearer {SUPABASE_SERVICE}"}
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

        elif platform == "threads":
            token   = conn.get("access_token", "")
            user_id = conn.get("page_id", "")
            if token and user_id:
                result = await publish_to_threads(req.content, req.image_url, token, user_id)
            else:
                result = {"success": False, "platform": "threads", "error": "Not connected"}

        elif platform == "bluesky":
            handle   = conn.get("page_name", "")
            app_pass = conn.get("access_token", "")
            if handle and app_pass:
                # Route through Cloudflare Worker (no domain restrictions)
                try:
                    async with _httpx.AsyncClient(timeout=30.0) as client:
                        r = await client.post(
                            f"{CF_WORKER}/bluesky/publish",
                            json={
                                "handle":       handle,
                                "app_password": app_pass,
                                "content":      req.content,
                                "image_url":    req.image_url or ""
                            }
                        )
                        result = r.json()
                        if "success" not in result:
                            result = {"success": False, "platform": "bluesky", "error": "Worker error"}
                except Exception as e:
                    result = {"success": False, "platform": "bluesky", "error": str(e)}
            else:
                result = {"success": False, "platform": "bluesky", "error": "Not connected"}

        elif platform == "google_business":
            token       = conn.get("access_token", "")
            location_id = conn.get("page_id", "")
            if token and location_id:
                result = await publish_to_google_business(req.content, req.image_url, token, location_id)
            else:
                result = {"success": False, "platform": "google_business", "error": "Not connected"}

        elif platform == "linkedin":
            token     = conn.get("access_token", "")
            author_id = conn.get("page_id", "")
            if token and author_id:
                result = await publish_to_linkedin(req.content, token, author_id)
            else:
                result = {"success": False, "platform": "linkedin", "error": "Not connected"}

        elif platform == "twitter":
            token = conn.get("access_token", "")
            if token:
                result = await publish_to_twitter(req.content, token)
            else:
                result = {"success": False, "platform": "twitter", "error": "Not connected"}

        elif platform == "facebook":
            token   = conn.get("access_token", "")
            page_id = conn.get("page_id", "")
            if token and page_id:
                result = await publish_to_facebook(req.content, token, page_id)
            else:
                result = {"success": False, "platform": "facebook", "error": "Not connected"}

        elif platform == "instagram":
            token     = conn.get("access_token", "")
            ig_usr_id = conn.get("page_id", "")
            if token and ig_usr_id:
                result = await publish_to_instagram(req.content, token, ig_usr_id, req.image_url or "")
            else:
                result = {"success": False, "platform": "instagram", "error": "Not connected"}

        else:
            result = {"success": False, "platform": platform, "error": "Platform coming soon"}

        results.append(result)
        if result["success"]: success += 1
        else: failed += 1

    # Update post status in Supabase
    final_status = "posted" if success > 0 else "failed"
    errors = [r.get("error") for r in results if not r["success"]]
    await update_post_status(req.post_id, final_status, "; ".join(filter(None, errors)))

    # Send email notifications
    user_email = user.get("email", "")
    if user_email and user_email != "guest":
        for result in results:
            platform = result.get("platform", "")
            if result.get("success"):
                html = post_success_email(user_email, platform, req.content)
                await send_email(
                    user_email,
                    f"✅ Your post is live on {platform.capitalize()}!",
                    html
                )
            else:
                error = result.get("error", "Unknown error")
                html = post_failed_email(user_email, platform, req.content, error)
                await send_email(
                    user_email,
                    f"⚠️ Post failed on {platform.capitalize()}",
                    html
                )

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
        async with _httpx.AsyncClient(timeout=15.0) as client:
            # Use upsert to handle duplicate user_id+platform
            r = await client.post(
                f"{SUPABASE_URL}/rest/v1/autopostleey_connections",
                headers={
                    "apikey":           SUPABASE_SERVICE,
                    "Authorization":    f"Bearer {SUPABASE_SERVICE}",
                    "Content-Type":     "application/json",
                    "Prefer":           "resolution=merge-duplicates,return=representation"
                },
                json=conn_data
            )
        print(f"Save connection response: {r.status_code} {r.text[:200]}")
        if r.status_code in (200, 201):
            return {"success": True, "platform": req.platform}
        # Try DELETE then INSERT if duplicate key
        elif r.status_code in (409, 422, 500, 400):
            async with _httpx.AsyncClient(timeout=15.0) as client:
                # Delete existing
                await client.delete(
                    f"{SUPABASE_URL}/rest/v1/autopostleey_connections",
                    params={"user_id": f"eq.{user_id}", "platform": f"eq.{req.platform}"},
                    headers={"apikey": SUPABASE_ANON, "Authorization": f"Bearer {SUPABASE_ANON}"}
                )
                # Insert fresh
                r2 = await client.post(
                    f"{SUPABASE_URL}/rest/v1/autopostleey_connections",
                    headers={"apikey": SUPABASE_ANON, "Authorization": f"Bearer {SUPABASE_ANON}", "Content-Type": "application/json"},
                    json=conn_data
                )
            if r2.status_code in (200, 201):
                return {"success": True, "platform": req.platform}
            raise HTTPException(500, f"Failed to save after retry: {r2.text}")
        else:
            raise HTTPException(500, f"Failed to save: {r.text}")
    except HTTPException:
        raise
    except Exception as e:
        print(f"Save connection error: {e}")
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


# ── FACEBOOK DATA DELETION CALLBACK ──────────────────────
import hashlib, hmac, base64, json as _json

class DeletionRequest(BaseModel):
    signed_request: str = ""

@app.post("/facebook/deletion")
async def facebook_data_deletion(req: DeletionRequest):
    """
    Facebook requires this endpoint to handle user data deletion requests.
    https://developers.facebook.com/docs/development/create-an-app/app-dashboard/data-deletion-callback
    """
    try:
        signed_request = req.signed_request
        if not signed_request:
            # Also handle GET for confirmation page
            return {"url": "https://autopostleey.com/deletion-confirmation.html", "confirmation_code": "autopostleey_deletion"}

        # Parse signed request from Facebook
        encoded_sig, payload = signed_request.split('.', 1)

        # Decode payload
        payload_padded = payload + '=' * (4 - len(payload) % 4)
        data = _json.loads(base64.urlsafe_b64decode(payload_padded))
        user_id = data.get('user_id', '')

        # Delete user data from Supabase if we have it
        if user_id and SUPABASE_URL:
            try:
                async with _httpx.AsyncClient(timeout=10.0) as client:
                    # Delete from all autopostleey tables
                    for table in ['autopostleey_posts', 'autopostleey_businesses', 'autopostleey_connections', 'autopostleey_waitlist']:
                        await client.delete(
                            f"{SUPABASE_URL}/rest/v1/{table}",
                            params={"facebook_user_id": f"eq.{user_id}"},
                            headers={
                                "apikey": SUPABASE_SERVICE,
                                "Authorization": f"Bearer {SUPABASE_SERVICE}"
                            }
                        )
            except Exception as e:
                print(f"Deletion error: {e}")

        confirmation_code = f"ap_{user_id}_{int(__import__('time').time())}"
        return {
            "url": "https://autopostleey.com/deletion-confirmation.html",
            "confirmation_code": confirmation_code
        }
    except Exception as e:
        print(f"Data deletion error: {e}")
        return {
            "url": "https://autopostleey.com/deletion-confirmation.html",
            "confirmation_code": "autopostleey_deletion_processed"
        }

@app.get("/facebook/deletion")
async def facebook_data_deletion_get():
    return {
        "url": "https://autopostleey.com/deletion-confirmation.html",
        "confirmation_code": "autopostleey_deletion"
    }


# ── FACEBOOK OAUTH ────────────────────────────────────────
FB_APP_ID     = os.getenv("FB_APP_ID", "2167420754104440")
FB_APP_SECRET = os.getenv("FB_APP_SECRET", "")
FB_REDIRECT   = "https://autopostleey.com/facebook-callback.html"

@app.get("/facebook/auth")
async def facebook_auth(user_id: str = ""):
    """Redirect user to Facebook OAuth"""
    import urllib.parse
    params = {
        "client_id":     FB_APP_ID,
        "redirect_uri":  FB_REDIRECT,
        "scope":         "pages_manage_posts,pages_read_engagement",
        "state":         user_id,  # Pass user_id through state
        "response_type": "code",
    }
    url = "https://www.facebook.com/dialog/oauth?" + urllib.parse.urlencode(params)
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url)


@app.get("/facebook/callback")
async def facebook_callback(code: str = "", state: str = "", error: str = ""):
    """Handle Facebook OAuth callback"""
    from fastapi.responses import RedirectResponse
    import urllib.parse

    if error:
        return RedirectResponse("https://autopostleey.com/dashboard?fb_error=cancelled")

    if not code:
        return RedirectResponse("https://autopostleey.com/dashboard?fb_error=no_code")

    user_id = state  # user_id passed via state param

    try:
        # Exchange code for access token
        async with _httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(
                "https://graph.facebook.com/v21.0/oauth/access_token",
                params={
                    "client_id":     FB_APP_ID,
                    "client_secret": FB_APP_SECRET,
                    "redirect_uri":  FB_REDIRECT,
                    "code":          code,
                }
            )
            token_data = r.json()

        if "error" in token_data:
            print(f"Token exchange error: {token_data}")
            return RedirectResponse("https://autopostleey.com/dashboard?fb_error=token_failed")

        user_token = token_data.get("access_token")

        # Get list of pages the user manages
        async with _httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(
                "https://graph.facebook.com/v21.0/me/accounts",
                params={"access_token": user_token}
            )
            pages_data = r.json()

        pages = pages_data.get("data", [])

        if not pages:
            return RedirectResponse("https://autopostleey.com/dashboard?fb_error=no_pages")

        # Use first page (most users have one page)
        page = pages[0]
        page_token = page.get("access_token")
        page_id    = page.get("id")
        page_name  = page.get("name")

        # Save connection to Supabase
        print(f"Saving Facebook connection for user {user_id}, page: {page_name}")
        if SUPABASE_URL and user_id:
            conn_data = {
                "user_id":      user_id,
                "platform":     "facebook",
                "access_token": page_token,
                "page_id":      page_id,
                "page_name":    page_name,
                "connected_at": datetime.utcnow().isoformat(),
            }
            async with _httpx.AsyncClient(timeout=10.0) as client:
                save_resp = await client.post(
                    f"{SUPABASE_URL}/rest/v1/autopostleey_connections",
                    headers={
                        "apikey":        SUPABASE_ANON,
                        "Authorization": f"Bearer {SUPABASE_ANON}",
                        "Content-Type":  "application/json",
                        "Prefer":        "resolution=merge-duplicates,return=representation"
                    },
                    params={"on_conflict": "user_id,platform"},
                    json=conn_data
                )
                print(f"Supabase save status: {save_resp.status_code}, body: {save_resp.text[:200]}")
        else:
            print(f"Skipping save — SUPABASE_URL={bool(SUPABASE_URL)}, user_id={bool(user_id)}")

        # Redirect back to dashboard with success
        params = urllib.parse.urlencode({
            "fb_success": "1",
            "page_name":  page_name or "Your Page"
        })
        return RedirectResponse(f"https://autopostleey.com/dashboard?{params}")

    except Exception as e:
        print(f"OAuth callback error: {e}")
        return RedirectResponse("https://autopostleey.com/dashboard?fb_error=server_error")


@app.get("/facebook/pages")
async def get_facebook_pages(user_token: str = "", user: dict = Depends(get_current_user)):
    """Get list of pages user can manage - for page selection UI"""
    if not user_token:
        raise HTTPException(400, "user_token required")
    try:
        async with _httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(
                "https://graph.facebook.com/v21.0/me/accounts",
                params={"access_token": user_token}
            )
            data = r.json()
        pages = [{"id": p["id"], "name": p["name"]} for p in data.get("data", [])]
        return {"pages": pages}
    except Exception as e:
        raise HTTPException(500, str(e))


# ── THREADS PUBLISHING ────────────────────────────────────
async def publish_to_threads(content: str, image_url: str, access_token: str, user_id: str) -> dict:
    """Publish to Threads via Meta Graph API"""
    try:
        async with _httpx.AsyncClient(timeout=30.0) as client:
            # Step 1: Create media container
            payload = {
                "media_type": "TEXT" if not image_url else "IMAGE",
                "text": content,
                "access_token": access_token
            }
            if image_url:
                payload["image_url"] = image_url

            r = await client.post(
                f"https://graph.threads.net/v1.0/{user_id}/threads",
                data=payload
            )
            data = r.json()
            if "error" in data:
                return {"success": False, "platform": "threads", "error": data["error"].get("message", "Unknown error")}

            container_id = data.get("id")
            if not container_id:
                return {"success": False, "platform": "threads", "error": "No container ID returned"}

            # Step 2: Publish the container
            r2 = await client.post(
                f"https://graph.threads.net/v1.0/{user_id}/threads_publish",
                data={"creation_id": container_id, "access_token": access_token}
            )
            data2 = r2.json()
            if "id" in data2:
                return {"success": True, "platform": "threads", "post_id": data2["id"]}
            return {"success": False, "platform": "threads", "error": data2.get("error", {}).get("message", "Publish failed")}
    except Exception as e:
        return {"success": False, "platform": "threads", "error": str(e)}


# ── BLUESKY PUBLISHING ────────────────────────────────────
async def publish_to_bluesky(content: str, image_url: str, handle: str, app_password: str) -> dict:
    """Publish to Bluesky via AT Protocol"""
    try:
        async with _httpx.AsyncClient(timeout=30.0) as client:
            # Step 1: Get auth token
            auth = await client.post(
                "https://bsky.social/xrpc/com.atproto.server.createSession",
                json={"identifier": handle, "password": app_password}
            )
            auth_data = auth.json()
            if "error" in auth_data:
                return {"success": False, "platform": "bluesky", "error": auth_data.get("message", "Auth failed")}

            access_jwt = auth_data.get("accessJwt")
            did        = auth_data.get("did")

            # Step 2: Create post
            post_record = {
                "$type": "app.bsky.feed.post",
                "text": content[:300],  # Bluesky 300 char limit
                "createdAt": datetime.utcnow().isoformat() + "Z"
            }

            # Step 3: Upload image if provided
            if image_url:
                try:
                    img_r = await client.get(image_url)
                    blob_r = await client.post(
                        "https://bsky.social/xrpc/com.atproto.repo.uploadBlob",
                        content=img_r.content,
                        headers={
                            "Authorization": f"Bearer {access_jwt}",
                            "Content-Type": "image/jpeg"
                        }
                    )
                    blob_data = blob_r.json()
                    if "blob" in blob_data:
                        post_record["embed"] = {
                            "$type": "app.bsky.embed.images",
                            "images": [{
                                "alt": content[:100],
                                "image": blob_data["blob"]
                            }]
                        }
                except Exception:
                    pass  # Post without image if upload fails

            r = await client.post(
                "https://bsky.social/xrpc/com.atproto.repo.createRecord",
                headers={"Authorization": f"Bearer {access_jwt}"},
                json={
                    "repo":       did,
                    "collection": "app.bsky.feed.post",
                    "record":     post_record
                }
            )
            data = r.json()
            if "uri" in data:
                return {"success": True, "platform": "bluesky", "post_id": data["uri"]}
            return {"success": False, "platform": "bluesky", "error": data.get("message", "Post failed")}
    except Exception as e:
        return {"success": False, "platform": "bluesky", "error": str(e)}


# ── GOOGLE BUSINESS PROFILE PUBLISHING ────────────────────
async def publish_to_google_business(content: str, image_url: str, access_token: str, location_id: str) -> dict:
    """Publish to Google Business Profile"""
    try:
        async with _httpx.AsyncClient(timeout=30.0) as client:
            post_data = {
                "languageCode": "en-US",
                "summary": content,
                "callToAction": {"actionType": "LEARN_MORE"},
                "topicType": "STANDARD"
            }
            if image_url:
                post_data["media"] = [{
                    "mediaFormat": "PHOTO",
                    "sourceUrl": image_url
                }]

            r = await client.post(
                f"https://mybusiness.googleapis.com/v4/{location_id}/localPosts",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json"
                },
                json=post_data
            )
            data = r.json()
            if "name" in data:
                return {"success": True, "platform": "google_business", "post_id": data["name"]}
            return {"success": False, "platform": "google_business", "error": data.get("error", {}).get("message", "Post failed")}
    except Exception as e:
        return {"success": False, "platform": "google_business", "error": str(e)}


# ── EMAIL NOTIFICATIONS ───────────────────────────────────
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
FROM_EMAIL     = os.getenv("FROM_EMAIL", "notifications@autopostleey.com")

async def send_email(to: str, subject: str, html: str):
    """Send email via Resend"""
    if not RESEND_API_KEY:
        print(f"No RESEND_API_KEY — skipping email to {to}")
        return False
    try:
        async with _httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {RESEND_API_KEY}",
                    "Content-Type":  "application/json"
                },
                json={
                    "from":    f"Autopostleey <{FROM_EMAIL}>",
                    "to":      [to],
                    "subject": subject,
                    "html":    html
                }
            )
            print(f"Email sent to {to}: {r.status_code}")
            return r.status_code in (200, 201)
    except Exception as e:
        print(f"Email error: {e}")
        return False


def post_success_email(user_email: str, platform: str, content: str, post_url: str = "") -> str:
    """Generate success email HTML"""
    platform_colors = {
        "bluesky":  "#0085ff",
        "discord":  "#5865f2",
        "telegram": "#26a5e4",
        "facebook": "#1877f2",
        "instagram":"#e1306c",
        "linkedin": "#0077b5",
        "twitter":  "#1da1f2",
    }
    color = platform_colors.get(platform, "#7c3aed")
    preview = content[:100] + "..." if len(content) > 100 else content

    return f"""
    <!DOCTYPE html>
    <html>
    <body style="margin:0;padding:0;background:#f4f4f5;font-family:Inter,sans-serif;">
      <div style="max-width:600px;margin:40px auto;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.08);">

        <!-- Header -->
        <div style="background:linear-gradient(135deg,#7c3aed,#a855f7);padding:32px;text-align:center;">
          <div style="font-family:monospace;font-size:20px;font-weight:700;color:#fff;">auto<span style="color:#c084fc;">postleey</span></div>
          <div style="color:rgba(255,255,255,0.8);margin-top:8px;font-size:14px;">Your post is live! 🎉</div>
        </div>

        <!-- Content -->
        <div style="padding:32px;">
          <div style="display:inline-block;background:{color}20;border:1px solid {color}40;border-radius:20px;padding:4px 14px;font-size:13px;color:{color};font-weight:600;margin-bottom:20px;">
            ✓ Posted to {platform.capitalize()}
          </div>

          <h2 style="font-size:20px;font-weight:700;color:#111;margin:0 0 16px;">Your post went live!</h2>

          <div style="background:#f8f8f8;border-left:3px solid {color};border-radius:4px;padding:16px;margin-bottom:24px;">
            <p style="color:#444;font-size:14px;line-height:1.6;margin:0;">{preview}</p>
          </div>

          {'<a href="'+post_url+'" style="display:inline-block;background:linear-gradient(135deg,#7c3aed,#a855f7);color:#fff;text-decoration:none;padding:12px 24px;border-radius:8px;font-weight:600;font-size:14px;margin-bottom:24px;">View Post →</a>' if post_url else ''}

          <p style="color:#666;font-size:14px;line-height:1.6;">
            Your content is reaching your audience right now.
            Keep the momentum going — schedule your next post from your dashboard.
          </p>

          <a href="https://autopostleey.com/dashboard.html" style="display:inline-block;background:#f4f4f5;color:#333;text-decoration:none;padding:10px 20px;border-radius:8px;font-size:13px;font-weight:600;">
            Go to Dashboard →
          </a>
        </div>

        <!-- Footer -->
        <div style="padding:20px 32px;border-top:1px solid #eee;text-align:center;">
          <p style="color:#999;font-size:12px;margin:0;">
            You received this because you have posts scheduled on Autopostleey.<br>
            <a href="https://autopostleey.com" style="color:#7c3aed;">autopostleey.com</a>
          </p>
        </div>
      </div>
    </body>
    </html>
    """


def post_failed_email(user_email: str, platform: str, content: str, error: str) -> str:
    """Generate failure email HTML"""
    preview = content[:100] + "..." if len(content) > 100 else content
    return f"""
    <!DOCTYPE html>
    <html>
    <body style="margin:0;padding:0;background:#f4f4f5;font-family:Inter,sans-serif;">
      <div style="max-width:600px;margin:40px auto;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.08);">
        <div style="background:linear-gradient(135deg,#7c3aed,#a855f7);padding:32px;text-align:center;">
          <div style="font-family:monospace;font-size:20px;font-weight:700;color:#fff;">auto<span style="color:#c084fc;">postleey</span></div>
          <div style="color:rgba(255,255,255,0.8);margin-top:8px;font-size:14px;">Post failed ⚠️</div>
        </div>
        <div style="padding:32px;">
          <h2 style="font-size:20px;font-weight:700;color:#111;margin:0 0 16px;">Your post to {platform.capitalize()} failed</h2>
          <div style="background:#fff5f5;border-left:3px solid #ef4444;border-radius:4px;padding:16px;margin-bottom:20px;">
            <p style="color:#666;font-size:13px;margin:0 0 8px;"><strong>Error:</strong> {error}</p>
            <p style="color:#444;font-size:14px;margin:0;">{preview}</p>
          </div>
          <p style="color:#666;font-size:14px;">Common fixes:</p>
          <ul style="color:#666;font-size:14px;line-height:1.8;">
            <li>Check your platform connection is still active</li>
            <li>Make sure your access token hasn't expired</li>
            <li>Verify your account has posting permissions</li>
          </ul>
          <a href="https://autopostleey.com/dashboard.html" style="display:inline-block;background:linear-gradient(135deg,#7c3aed,#a855f7);color:#fff;text-decoration:none;padding:12px 24px;border-radius:8px;font-weight:600;font-size:14px;">
            Fix Connection →
          </a>
        </div>
        <div style="padding:20px 32px;border-top:1px solid #eee;text-align:center;">
          <p style="color:#999;font-size:12px;margin:0;"><a href="https://autopostleey.com" style="color:#7c3aed;">autopostleey.com</a></p>
        </div>
      </div>
    </body>
    </html>
    """


# ── LINKEDIN OAUTH ────────────────────────────────────────
LI_CLIENT_ID     = os.getenv("LINKEDIN_CLIENT_ID", "86r6hi7gg40xxm")
LI_CLIENT_SECRET = os.getenv("LINKEDIN_CLIENT_SECRET", "")
LI_REDIRECT      = "https://autopostleey-api-production.up.railway.app/linkedin/callback"

@app.get("/linkedin/auth")
async def linkedin_auth(user_id: str = ""):
    """Redirect user to LinkedIn OAuth"""
    import urllib.parse
    params = {
        "response_type": "code",
        "client_id":     LI_CLIENT_ID,
        "redirect_uri":  LI_REDIRECT,
        "state":         user_id,
        "scope":         "openid profile email w_member_social",
    }
    url = "https://www.linkedin.com/oauth/v2/authorization?" + urllib.parse.urlencode(params)
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url)


@app.get("/linkedin/callback")
async def linkedin_callback(code: str = "", state: str = "", error: str = ""):
    """Handle LinkedIn OAuth callback"""
    from fastapi.responses import RedirectResponse
    import urllib.parse

    if error:
        return RedirectResponse("https://autopostleey.com/dashboard.html?li_error=cancelled")
    if not code:
        return RedirectResponse("https://autopostleey.com/dashboard.html?li_error=no_code")

    user_id = state

    try:
        async with _httpx.AsyncClient(timeout=15.0) as client:
            # Exchange code for access token
            r = await client.post(
                "https://www.linkedin.com/oauth/v2/accessToken",
                data={
                    "grant_type":    "authorization_code",
                    "code":          code,
                    "redirect_uri":  LI_REDIRECT,
                    "client_id":     LI_CLIENT_ID,
                    "client_secret": LI_CLIENT_SECRET,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"}
            )
            token_data = r.json()

        if "error" in token_data:
            return RedirectResponse("https://autopostleey.com/dashboard.html?li_error=token_failed")

        access_token = token_data.get("access_token")

        # Get user profile
        async with _httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(
                "https://api.linkedin.com/v2/userinfo",
                headers={"Authorization": f"Bearer {access_token}"}
            )
            profile = r.json()

        page_name = profile.get("name", "LinkedIn User")
        page_id   = profile.get("sub", "")

        # Save connection
        if SUPABASE_URL and user_id:
            conn_data = {
                "user_id":      user_id,
                "platform":     "linkedin",
                "access_token": access_token,
                "page_id":      page_id,
                "page_name":    page_name,
                "connected_at": datetime.utcnow().isoformat(),
            }
            async with _httpx.AsyncClient(timeout=10.0) as client:
                # Delete existing then insert
                await client.delete(
                    f"{SUPABASE_URL}/rest/v1/autopostleey_connections",
                    params={"user_id": f"eq.{user_id}", "platform": "eq.linkedin"},
                    headers={"apikey": SUPABASE_SERVICE, "Authorization": f"Bearer {SUPABASE_SERVICE}"}
                )
                await client.post(
                    f"{SUPABASE_URL}/rest/v1/autopostleey_connections",
                    headers={"apikey": SUPABASE_SERVICE, "Authorization": f"Bearer {SUPABASE_SERVICE}", "Content-Type": "application/json"},
                    json=conn_data
                )

        params = urllib.parse.urlencode({"li_success": "1", "page_name": page_name})
        return RedirectResponse(f"https://autopostleey.com/dashboard.html?{params}")

    except Exception as e:
        print(f"LinkedIn OAuth error: {e}")
        return RedirectResponse("https://autopostleey.com/dashboard.html?li_error=server_error")


async def publish_to_linkedin(content: str, access_token: str, author_id: str) -> dict:
    """Publish a post to LinkedIn"""
    try:
        async with _httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                "https://api.linkedin.com/v2/ugcPosts",
                headers={
                    "Authorization":  f"Bearer {access_token}",
                    "Content-Type":   "application/json",
                    "X-Restli-Protocol-Version": "2.0.0"
                },
                json={
                    "author":          f"urn:li:person:{author_id}",
                    "lifecycleState":  "PUBLISHED",
                    "specificContent": {
                        "com.linkedin.ugc.ShareContent": {
                            "shareCommentary": {"text": content},
                            "shareMediaCategory": "NONE"
                        }
                    },
                    "visibility": {
                        "com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"
                    }
                }
            )
            if r.status_code in (200, 201):
                data = r.json()
                return {"success": True, "platform": "linkedin", "post_id": data.get("id", "")}
            return {"success": False, "platform": "linkedin", "error": f"HTTP {r.status_code}: {r.text[:200]}"}
    except Exception as e:
        return {"success": False, "platform": "linkedin", "error": str(e)}


# ── TWITTER/X OAUTH ───────────────────────────────────────
import hashlib, base64, secrets

TW_CLIENT_ID     = os.getenv("TWITTER_CLIENT_ID", "RkNaY2lINkJPODdoYW5kU3cyQmM6MTpjaQ")
TW_CLIENT_SECRET = os.getenv("TWITTER_CLIENT_SECRET", "")
TW_REDIRECT      = "https://autopostleey-api-production.up.railway.app/twitter/callback"

# Store code verifiers temporarily (in production use Redis/DB)
_tw_verifiers = {}

@app.get("/twitter/auth")
async def twitter_auth(user_id: str = ""):
    """Redirect user to Twitter/X OAuth 2.0"""
    import urllib.parse

    # Generate PKCE code verifier and challenge
    code_verifier  = secrets.token_urlsafe(32)
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode()).digest()
    ).rstrip(b'=').decode()

    state = f"{user_id}:{secrets.token_urlsafe(8)}"
    _tw_verifiers[state] = code_verifier

    params = {
        "response_type":         "code",
        "client_id":             TW_CLIENT_ID,
        "redirect_uri":          TW_REDIRECT,
        "scope":                 "tweet.write users.read offline.access",
        "state":                 state,
        "code_challenge":        code_challenge,
        "code_challenge_method": "S256",
    }
    url = "https://twitter.com/i/oauth2/authorize?" + urllib.parse.urlencode(params)
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url)


@app.get("/twitter/callback")
async def twitter_callback(code: str = "", state: str = "", error: str = ""):
    """Handle Twitter/X OAuth callback"""
    from fastapi.responses import RedirectResponse
    import urllib.parse

    if error:
        return RedirectResponse("https://autopostleey.com/dashboard.html?tw_error=cancelled")
    if not code or not state:
        return RedirectResponse("https://autopostleey.com/dashboard.html?tw_error=no_code")

    user_id      = state.split(":")[0]
    code_verifier = _tw_verifiers.pop(state, None)

    if not code_verifier:
        return RedirectResponse("https://autopostleey.com/dashboard.html?tw_error=invalid_state")

    try:
        # Exchange code for token
        async with _httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                "https://api.twitter.com/2/oauth2/token",
                data={
                    "grant_type":    "authorization_code",
                    "code":          code,
                    "redirect_uri":  TW_REDIRECT,
                    "code_verifier": code_verifier,
                    "client_id":     TW_CLIENT_ID,
                },
                auth=(TW_CLIENT_ID, TW_CLIENT_SECRET),
                headers={"Content-Type": "application/x-www-form-urlencoded"}
            )
            token_data = r.json()

        if "error" in token_data:
            print(f"Twitter token error: {token_data}")
            return RedirectResponse("https://autopostleey.com/dashboard.html?tw_error=token_failed")

        access_token  = token_data.get("access_token")
        refresh_token = token_data.get("refresh_token", "")

        # Get user profile
        async with _httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(
                "https://api.twitter.com/2/users/me",
                headers={"Authorization": f"Bearer {access_token}"}
            )
            profile = r.json()

        tw_user    = profile.get("data", {})
        page_name  = tw_user.get("username", "twitter_user")
        page_id    = tw_user.get("id", "")
        name       = tw_user.get("name", page_name)

        # Save connection
        if SUPABASE_URL and user_id:
            conn_data = {
                "user_id":      user_id,
                "platform":     "twitter",
                "access_token": access_token,
                "page_id":      page_id,
                "page_name":    f"@{page_name}",
                "webhook_url":  refresh_token,
                "connected_at": datetime.utcnow().isoformat(),
            }
            async with _httpx.AsyncClient(timeout=10.0) as client:
                await client.delete(
                    f"{SUPABASE_URL}/rest/v1/autopostleey_connections",
                    params={"user_id": f"eq.{user_id}", "platform": "eq.twitter"},
                    headers={"apikey": SUPABASE_SERVICE, "Authorization": f"Bearer {SUPABASE_SERVICE}"}
                )
                await client.post(
                    f"{SUPABASE_URL}/rest/v1/autopostleey_connections",
                    headers={"apikey": SUPABASE_SERVICE, "Authorization": f"Bearer {SUPABASE_SERVICE}", "Content-Type": "application/json"},
                    json=conn_data
                )

        params = urllib.parse.urlencode({"tw_success": "1", "page_name": f"@{page_name}"})
        return RedirectResponse(f"https://autopostleey.com/dashboard.html?{params}")

    except Exception as e:
        print(f"Twitter OAuth error: {e}")
        return RedirectResponse("https://autopostleey.com/dashboard.html?tw_error=server_error")


async def publish_to_twitter(content: str, access_token: str, access_secret: str = "") -> dict:
    """Publish a tweet via Twitter API v2 with OAuth 1.0a"""
    import hmac as _hmac, hashlib as _hashlib, time as _time, urllib.parse as _up, secrets as _sec
    try:
        tweet_text    = content[:280]
        TW_API_KEY    = os.getenv("TWITTER_API_KEY", "")
        TW_API_SECRET = os.getenv("TWITTER_API_SECRET", "")

        if not TW_API_KEY or not access_token:
            return {"success": False, "platform": "twitter", "error": "Missing Twitter credentials. Add TWITTER_API_KEY to Railway."}

        url = "https://api.twitter.com/2/tweets"

        oauth_params = {
            "oauth_consumer_key":     TW_API_KEY,
            "oauth_nonce":            _sec.token_hex(16),
            "oauth_signature_method": "HMAC-SHA1",
            "oauth_timestamp":        str(int(_time.time())),
            "oauth_token":            access_token,
            "oauth_version":          "1.0",
        }

        sorted_params = "&".join(
            f"{_up.quote(k, '')}={_up.quote(str(v), '')}"
            for k, v in sorted(oauth_params.items())
        )
        sig_base    = f"POST&{_up.quote(url, '')}&{_up.quote(sorted_params, '')}"
        signing_key = f"{_up.quote(TW_API_SECRET, '')}&{_up.quote(access_secret or '', '')}"
        sig = base64.b64encode(
            _hmac.new(signing_key.encode(), sig_base.encode(), _hashlib.sha1).digest()
        ).decode()
        oauth_params["oauth_signature"] = sig

        auth_header = "OAuth " + ", ".join(
            f'{_up.quote(k, "")}="{_up.quote(str(v), "")}"'
            for k, v in sorted(oauth_params.items())
        )

        async with _httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                url,
                headers={"Authorization": auth_header, "Content-Type": "application/json"},
                json={"text": tweet_text}
            )
            if r.status_code in (200, 201):
                data = r.json()
                return {"success": True, "platform": "twitter", "post_id": data.get("data", {}).get("id", "")}
            return {"success": False, "platform": "twitter", "error": f"HTTP {r.status_code}: {r.text[:200]}"}
    except Exception as e:
        return {"success": False, "platform": "twitter", "error": str(e)}


async def publish_to_linkedin(content: str, access_token: str, author_id: str) -> dict:
    """Publish a post to LinkedIn"""
    try:
        async with _httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                "https://api.linkedin.com/v2/ugcPosts",
                headers={
                    "Authorization":  f"Bearer {access_token}",
                    "Content-Type":   "application/json",
                    "X-Restli-Protocol-Version": "2.0.0"
                },
                json={
                    "author":          f"urn:li:person:{author_id}",
                    "lifecycleState":  "PUBLISHED",
                    "specificContent": {
                        "com.linkedin.ugc.ShareContent": {
                            "shareCommentary": {"text": content},
                            "shareMediaCategory": "NONE"
                        }
                    },
                    "visibility": {
                        "com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"
                    }
                }
            )
            if r.status_code in (200, 201):
                data = r.json()
                return {"success": True, "platform": "linkedin", "post_id": data.get("id", "")}
            return {"success": False, "platform": "linkedin", "error": f"HTTP {r.status_code}: {r.text[:200]}"}
    except Exception as e:
        return {"success": False, "platform": "linkedin", "error": str(e)}


# ── TWITTER/X OAUTH ───────────────────────────────────────
import hashlib, base64, secrets

TW_CLIENT_ID     = os.getenv("TWITTER_CLIENT_ID", "RkNaY2lINkJPODdoYW5kU3cyQmM6MTpjaQ")
TW_CLIENT_SECRET = os.getenv("TWITTER_CLIENT_SECRET", "")
TW_REDIRECT      = "https://autopostleey-api-production.up.railway.app/twitter/callback"

# Store code verifiers temporarily (in production use Redis/DB)
_tw_verifiers = {}

@app.get("/twitter/auth")
async def twitter_auth(user_id: str = ""):
    """Redirect user to Twitter/X OAuth 2.0"""
    import urllib.parse

    # Generate PKCE code verifier and challenge
    code_verifier  = secrets.token_urlsafe(32)
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode()).digest()
    ).rstrip(b'=').decode()

    state = f"{user_id}:{secrets.token_urlsafe(8)}"
    _tw_verifiers[state] = code_verifier

    params = {
        "response_type":         "code",
        "client_id":             TW_CLIENT_ID,
        "redirect_uri":          TW_REDIRECT,
        "scope":                 "tweet.write users.read offline.access",
        "state":                 state,
        "code_challenge":        code_challenge,
        "code_challenge_method": "S256",
    }
    url = "https://twitter.com/i/oauth2/authorize?" + urllib.parse.urlencode(params)
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url)


@app.get("/twitter/callback")
async def twitter_callback(code: str = "", state: str = "", error: str = ""):
    """Handle Twitter/X OAuth callback"""
    from fastapi.responses import RedirectResponse
    import urllib.parse

    if error:
        return RedirectResponse("https://autopostleey.com/dashboard.html?tw_error=cancelled")
    if not code or not state:
        return RedirectResponse("https://autopostleey.com/dashboard.html?tw_error=no_code")

    user_id      = state.split(":")[0]
    code_verifier = _tw_verifiers.pop(state, None)

    if not code_verifier:
        return RedirectResponse("https://autopostleey.com/dashboard.html?tw_error=invalid_state")

    try:
        # Exchange code for token
        async with _httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                "https://api.twitter.com/2/oauth2/token",
                data={
                    "grant_type":    "authorization_code",
                    "code":          code,
                    "redirect_uri":  TW_REDIRECT,
                    "code_verifier": code_verifier,
                    "client_id":     TW_CLIENT_ID,
                },
                auth=(TW_CLIENT_ID, TW_CLIENT_SECRET),
                headers={"Content-Type": "application/x-www-form-urlencoded"}
            )
            token_data = r.json()

        if "error" in token_data:
            print(f"Twitter token error: {token_data}")
            return RedirectResponse("https://autopostleey.com/dashboard.html?tw_error=token_failed")

        access_token  = token_data.get("access_token")
        refresh_token = token_data.get("refresh_token", "")

        # Get user profile
        async with _httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(
                "https://api.twitter.com/2/users/me",
                headers={"Authorization": f"Bearer {access_token}"}
            )
            profile = r.json()

        tw_user    = profile.get("data", {})
        page_name  = tw_user.get("username", "twitter_user")
        page_id    = tw_user.get("id", "")
        name       = tw_user.get("name", page_name)

        # Save connection
        if SUPABASE_URL and user_id:
            conn_data = {
                "user_id":      user_id,
                "platform":     "twitter",
                "access_token": access_token,
                "page_id":      page_id,
                "page_name":    f"@{page_name}",
                "webhook_url":  refresh_token,
                "connected_at": datetime.utcnow().isoformat(),
            }
            async with _httpx.AsyncClient(timeout=10.0) as client:
                await client.delete(
                    f"{SUPABASE_URL}/rest/v1/autopostleey_connections",
                    params={"user_id": f"eq.{user_id}", "platform": "eq.twitter"},
                    headers={"apikey": SUPABASE_SERVICE, "Authorization": f"Bearer {SUPABASE_SERVICE}"}
                )
                await client.post(
                    f"{SUPABASE_URL}/rest/v1/autopostleey_connections",
                    headers={"apikey": SUPABASE_SERVICE, "Authorization": f"Bearer {SUPABASE_SERVICE}", "Content-Type": "application/json"},
                    json=conn_data
                )

        params = urllib.parse.urlencode({"tw_success": "1", "page_name": f"@{page_name}"})
        return RedirectResponse(f"https://autopostleey.com/dashboard.html?{params}")

    except Exception as e:
        print(f"Twitter OAuth error: {e}")
        return RedirectResponse("https://autopostleey.com/dashboard.html?tw_error=server_error")


async def publish_to_twitter(content: str, access_token: str) -> dict:
    """Publish a tweet via Twitter API v2"""
    try:
        # Twitter 280 char limit
        tweet_text = content[:280]
        async with _httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                "https://api.twitter.com/2/tweets",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type":  "application/json"
                },
                json={"text": tweet_text}
            )
            if r.status_code in (200, 201):
                data = r.json()
                return {"success": True, "platform": "twitter", "post_id": data.get("data", {}).get("id", "")}
            return {"success": False, "platform": "twitter", "error": f"HTTP {r.status_code}: {r.text[:200]}"}
    except Exception as e:
        return {"success": False, "platform": "twitter", "error": str(e)}


# ── FACEBOOK OAUTH ────────────────────────────────────────
FB_APP_ID     = os.getenv("FB_APP_ID", "474990059818223")
FB_APP_SECRET = os.getenv("FB_APP_SECRET", "")
FB_REDIRECT   = "https://autopostleey.com/facebook-callback.html"

@app.get("/facebook/auth")
async def facebook_auth(user_id: str = ""):
    """Redirect user to Facebook OAuth"""
    import urllib.parse
    params = {
        "client_id":     FB_APP_ID,
        "redirect_uri":  FB_REDIRECT,
        "scope":         "pages_manage_posts,pages_read_engagement,pages_show_list",
        "state":         user_id,
        "response_type": "code",
    }
    url = "https://www.facebook.com/dialog/oauth?" + urllib.parse.urlencode(params)
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url)


@app.get("/facebook/callback")
async def facebook_callback(code: str = "", state: str = "", error: str = ""):
    """Handle Facebook OAuth callback"""
    from fastapi.responses import RedirectResponse
    import urllib.parse

    if error:
        return RedirectResponse("https://autopostleey.com/dashboard.html?fb_error=cancelled")
    if not code:
        return RedirectResponse("https://autopostleey.com/dashboard.html?fb_error=no_code")

    user_id = state

    try:
        async with _httpx.AsyncClient(timeout=15.0) as client:
            # Exchange code for user token
            r = await client.get(
                "https://graph.facebook.com/v21.0/oauth/access_token",
                params={
                    "client_id":     FB_APP_ID,
                    "client_secret": FB_APP_SECRET,
                    "redirect_uri":  FB_REDIRECT,
                    "code":          code,
                }
            )
            token_data = r.json()

        if "error" in token_data:
            print(f"Facebook token error: {token_data}")
            return RedirectResponse("https://autopostleey.com/dashboard.html?fb_error=token_failed")

        user_token = token_data.get("access_token")
        print(f"Got user token: {user_token[:20] if user_token else 'None'}...")
        
        # Check token permissions
        async with _httpx.AsyncClient(timeout=10.0) as client:
            perm_r = await client.get(
                "https://graph.facebook.com/v21.0/me/permissions",
                params={"access_token": user_token}
            )
            print(f"Token permissions: {perm_r.json()}")

        # Get pages the user manages
        async with _httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(
                "https://graph.facebook.com/v21.0/me/accounts",
                params={"access_token": user_token, "fields": "id,name,access_token,category"}
            )
            pages_data = r.json()

        print(f"Facebook pages response: {pages_data}")
        pages = pages_data.get("data", [])
        print(f"Pages found: {len(pages)}")

        if not pages:
            # No pages found — save user token instead
            async with _httpx.AsyncClient(timeout=15.0) as client:
                r = await client.get(
                    "https://graph.facebook.com/v21.0/me",
                    params={"access_token": user_token, "fields": "id,name"}
                )
                me = r.json()
            page_name = me.get("name", "Facebook User")
            page_id   = me.get("id", "")
            page_token = user_token
        else:
            # Use first page
            page       = pages[0]
            page_token = page.get("access_token")
            page_id    = page.get("id")
            page_name  = page.get("name")

        # Save connection
        if SUPABASE_URL and user_id:
            conn_data = {
                "user_id":      user_id,
                "platform":     "facebook",
                "access_token": page_token,
                "page_id":      page_id,
                "page_name":    page_name,
                "connected_at": datetime.utcnow().isoformat(),
            }
            async with _httpx.AsyncClient(timeout=10.0) as client:
                await client.delete(
                    f"{SUPABASE_URL}/rest/v1/autopostleey_connections",
                    params={"user_id": f"eq.{user_id}", "platform": "eq.facebook"},
                    headers={"apikey": SUPABASE_SERVICE, "Authorization": f"Bearer {SUPABASE_SERVICE}"}
                )
                await client.post(
                    f"{SUPABASE_URL}/rest/v1/autopostleey_connections",
                    headers={"apikey": SUPABASE_SERVICE, "Authorization": f"Bearer {SUPABASE_SERVICE}", "Content-Type": "application/json"},
                    json=conn_data
                )

        params = urllib.parse.urlencode({"fb_success": "1", "page_name": page_name})
        return RedirectResponse(f"https://autopostleey.com/dashboard.html?{params}")

    except Exception as e:
        print(f"Facebook OAuth error: {e}")
        return RedirectResponse("https://autopostleey.com/dashboard.html?fb_error=server_error")


# ── INSTAGRAM OAUTH ───────────────────────────────────────
IG_APP_ID     = os.getenv("IG_APP_ID", "474990059818223")
IG_APP_SECRET = os.getenv("IG_APP_SECRET", "")
IG_REDIRECT   = "https://autopostleey.com/instagram-callback.html"

@app.get("/instagram/auth")
async def instagram_auth(user_id: str = ""):
    """Redirect user to Instagram OAuth via Facebook"""
    import urllib.parse
    params = {
        "client_id":     IG_APP_ID,
        "redirect_uri":  IG_REDIRECT,
        "scope":         "instagram_basic,instagram_content_publish,pages_read_engagement",
        "state":         user_id,
        "response_type": "code",
    }
    url = "https://www.facebook.com/dialog/oauth?" + urllib.parse.urlencode(params)
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url)


@app.get("/instagram/callback")
async def instagram_callback(code: str = "", state: str = "", error: str = ""):
    """Handle Instagram OAuth callback"""
    from fastapi.responses import RedirectResponse
    import urllib.parse

    if error:
        return RedirectResponse("https://autopostleey.com/dashboard.html?ig_error=cancelled")
    if not code:
        return RedirectResponse("https://autopostleey.com/dashboard.html?ig_error=no_code")

    user_id = state

    try:
        async with _httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(
                "https://graph.facebook.com/v21.0/oauth/access_token",
                params={
                    "client_id":     IG_APP_ID,
                    "client_secret": IG_APP_SECRET,
                    "redirect_uri":  IG_REDIRECT,
                    "code":          code,
                }
            )
            token_data = r.json()

        if "error" in token_data:
            return RedirectResponse("https://autopostleey.com/dashboard.html?ig_error=token_failed")

        user_token = token_data.get("access_token")

        # Get Instagram Business Account
        async with _httpx.AsyncClient(timeout=15.0) as client:
            # First get Facebook pages
            r = await client.get(
                "https://graph.facebook.com/v21.0/me/accounts",
                params={"access_token": user_token}
            )
            pages = r.json().get("data", [])

        ig_id   = None
        ig_name = None
        ig_token = user_token

        for page in pages:
            page_token = page.get("access_token")
            page_id    = page.get("id")
            async with _httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(
                    f"https://graph.facebook.com/v21.0/{page_id}",
                    params={"fields": "instagram_business_account", "access_token": page_token}
                )
                ig_data = r.json()
            if ig_data.get("instagram_business_account"):
                ig_id    = ig_data["instagram_business_account"]["id"]
                ig_name  = page.get("name", "Instagram")
                ig_token = page_token
                break

        if not ig_id:
            return RedirectResponse("https://autopostleey.com/dashboard.html?ig_error=no_instagram")

        # Save connection
        if SUPABASE_URL and user_id:
            conn_data = {
                "user_id":      user_id,
                "platform":     "instagram",
                "access_token": ig_token,
                "page_id":      ig_id,
                "page_name":    ig_name,
                "connected_at": datetime.utcnow().isoformat(),
            }
            async with _httpx.AsyncClient(timeout=10.0) as client:
                await client.delete(
                    f"{SUPABASE_URL}/rest/v1/autopostleey_connections",
                    params={"user_id": f"eq.{user_id}", "platform": "eq.instagram"},
                    headers={"apikey": SUPABASE_SERVICE, "Authorization": f"Bearer {SUPABASE_SERVICE}"}
                )
                await client.post(
                    f"{SUPABASE_URL}/rest/v1/autopostleey_connections",
                    headers={"apikey": SUPABASE_SERVICE, "Authorization": f"Bearer {SUPABASE_SERVICE}", "Content-Type": "application/json"},
                    json=conn_data
                )

        params = urllib.parse.urlencode({"ig_success": "1", "page_name": ig_name})
        return RedirectResponse(f"https://autopostleey.com/dashboard.html?{params}")

    except Exception as e:
        print(f"Instagram OAuth error: {e}")
        return RedirectResponse("https://autopostleey.com/dashboard.html?ig_error=server_error")


async def publish_to_facebook(content: str, access_token: str, page_id: str) -> dict:
    """Publish to Facebook Page"""
    try:
        async with _httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                f"https://graph.facebook.com/v21.0/{page_id}/feed",
                data={"message": content, "access_token": access_token}
            )
            data = r.json()
            if "id" in data:
                return {"success": True, "platform": "facebook", "post_id": data["id"]}
            return {"success": False, "platform": "facebook", "error": data.get("error", {}).get("message", "Post failed")}
    except Exception as e:
        return {"success": False, "platform": "facebook", "error": str(e)}


async def publish_to_instagram(content: str, access_token: str, ig_user_id: str, image_url: str = "") -> dict:
    """Publish to Instagram Business Account"""
    try:
        async with _httpx.AsyncClient(timeout=30.0) as client:
            if image_url:
                # Photo post
                r = await client.post(
                    f"https://graph.facebook.com/v21.0/{ig_user_id}/media",
                    data={"image_url": image_url, "caption": content, "access_token": access_token}
                )
            else:
                # Text/carousel - use text overlay on blank image not supported
                # Use a simple approach with just caption
                r = await client.post(
                    f"https://graph.facebook.com/v21.0/{ig_user_id}/media",
                    data={"media_type": "REELS", "caption": content, "access_token": access_token}
                )
            container = r.json()
            if "error" in container:
                return {"success": False, "platform": "instagram", "error": container["error"].get("message", "Container failed")}

            container_id = container.get("id")
            if not container_id:
                return {"success": False, "platform": "instagram", "error": "No container ID"}

            # Publish the container
            r2 = await client.post(
                f"https://graph.facebook.com/v21.0/{ig_user_id}/media_publish",
                data={"creation_id": container_id, "access_token": access_token}
            )
            data2 = r2.json()
            if "id" in data2:
                return {"success": True, "platform": "instagram", "post_id": data2["id"]}
            return {"success": False, "platform": "instagram", "error": data2.get("error", {}).get("message", "Publish failed")}
    except Exception as e:
        return {"success": False, "platform": "instagram", "error": str(e)}


# ── TWITTER/X OAUTH 2.0 ───────────────────────────────────
import hashlib as _hashlib, secrets as _secrets

TW_CLIENT_ID     = os.getenv("TWITTER_CLIENT_ID", "RkNaY2lINkJPODdoYW5kU3cyQmM6MTpjaQ")
TW_CLIENT_SECRET = os.getenv("TWITTER_CLIENT_SECRET", "")
TW_REDIRECT      = "https://autopostleey.com/twitter-callback.html"

_tw_verifiers = {}

@app.get("/twitter/auth")
async def twitter_auth(user_id: str = ""):
    import urllib.parse
    code_verifier  = _secrets.token_urlsafe(32)
    code_challenge = base64.b64encode(
        _hashlib.sha256(code_verifier.encode()).digest()
    ).rstrip(b"=").decode()
    state = f"{user_id}:{_secrets.token_urlsafe(8)}"
    _tw_verifiers[state] = code_verifier
    params = {
        "response_type":         "code",
        "client_id":             TW_CLIENT_ID,
        "redirect_uri":          TW_REDIRECT,
        "scope":                 "tweet.write users.read offline.access",
        "state":                 state,
        "code_challenge":        code_challenge,
        "code_challenge_method": "S256",
    }
    from fastapi.responses import RedirectResponse
    return RedirectResponse("https://twitter.com/i/oauth2/authorize?" + urllib.parse.urlencode(params))


@app.get("/twitter/callback")
async def twitter_callback(code: str = "", state: str = "", error: str = ""):
    from fastapi.responses import RedirectResponse
    import urllib.parse
    if error or not code:
        return RedirectResponse("https://autopostleey.com/dashboard.html?tw_error=cancelled")
    user_id       = state.split(":")[0]
    code_verifier = _tw_verifiers.pop(state, None)
    if not code_verifier:
        return RedirectResponse("https://autopostleey.com/dashboard.html?tw_error=invalid_state")
    try:
        async with _httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                "https://api.twitter.com/2/oauth2/token",
                data={
                    "grant_type":    "authorization_code",
                    "code":          code,
                    "redirect_uri":  TW_REDIRECT,
                    "code_verifier": code_verifier,
                    "client_id":     TW_CLIENT_ID,
                },
                auth=(TW_CLIENT_ID, TW_CLIENT_SECRET),
                headers={"Content-Type": "application/x-www-form-urlencoded"}
            )
            token_data = r.json()
        if "error" in token_data:
            print(f"Twitter token error: {token_data}")
            return RedirectResponse("https://autopostleey.com/dashboard.html?tw_error=token_failed")
        access_token = token_data.get("access_token")
        async with _httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(
                "https://api.twitter.com/2/users/me",
                headers={"Authorization": f"Bearer {access_token}"}
            )
            profile = r.json()
        tw_user   = profile.get("data", {})
        page_name = f"@{tw_user.get('username', 'twitter_user')}"
        page_id   = tw_user.get("id", "")
        if SUPABASE_URL and user_id:
            conn_data = {
                "user_id": user_id, "platform": "twitter",
                "access_token": access_token, "page_id": page_id,
                "page_name": page_name, "connected_at": datetime.utcnow().isoformat(),
            }
            async with _httpx.AsyncClient(timeout=10.0) as client:
                await client.delete(
                    f"{SUPABASE_URL}/rest/v1/autopostleey_connections",
                    params={"user_id": f"eq.{user_id}", "platform": "eq.twitter"},
                    headers={"apikey": SUPABASE_SERVICE, "Authorization": f"Bearer {SUPABASE_SERVICE}"}
                )
                await client.post(
                    f"{SUPABASE_URL}/rest/v1/autopostleey_connections",
                    headers={"apikey": SUPABASE_SERVICE, "Authorization": f"Bearer {SUPABASE_SERVICE}", "Content-Type": "application/json"},
                    json=conn_data
                )
        params = urllib.parse.urlencode({"tw_success": "1", "page_name": page_name})
        return RedirectResponse(f"https://autopostleey.com/dashboard.html?{params}")
    except Exception as e:
        print(f"Twitter OAuth error: {e}")
        return RedirectResponse("https://autopostleey.com/dashboard.html?tw_error=server_error")


# ── TELEGRAM BOT AUTO-CONNECT ─────────────────────────────
TG_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    """Handle Telegram bot webhook for auto-connect"""
    try:
        body = await request.json()
        message = body.get("message", {})
        text    = message.get("text", "")
        chat    = message.get("chat", {})
        chat_id = str(chat.get("id", ""))
        chat_type = chat.get("type", "")
        
        # Handle /start command with user_id
        if text.startswith("/start connect_"):
            user_id = text.replace("/start connect_", "").strip()
            
            # Save connection to Supabase
            if user_id and SUPABASE_URL:
                conn_data = {
                    "user_id":      user_id,
                    "platform":     "telegram",
                    "bot_token":    TG_BOT_TOKEN,
                    "chat_id":      chat_id,
                    "page_name":    chat.get("title") or chat.get("first_name", "Telegram"),
                    "connected_at": datetime.utcnow().isoformat(),
                }
                async with _httpx.AsyncClient(timeout=10.0) as client:
                    await client.delete(
                        f"{SUPABASE_URL}/rest/v1/autopostleey_connections",
                        params={"user_id": f"eq.{user_id}", "platform": "eq.telegram"},
                        headers={"apikey": SUPABASE_SERVICE, "Authorization": f"Bearer {SUPABASE_SERVICE}"}
                    )
                    await client.post(
                        f"{SUPABASE_URL}/rest/v1/autopostleey_connections",
                        headers={"apikey": SUPABASE_SERVICE, "Authorization": f"Bearer {SUPABASE_SERVICE}", "Content-Type": "application/json"},
                        json=conn_data
                    )
            
            # Send success message to user
            async with _httpx.AsyncClient(timeout=10.0) as client:
                await client.post(
                    f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
                    json={
                        "chat_id": chat_id,
                        "text": "✅ Connected to Autopostleey!\n\nYour posts will be published here automatically. Go back to your dashboard to start scheduling posts! 🚀",
                        "parse_mode": "HTML"
                    }
                )
        
        return {"ok": True}
    except Exception as e:
        print(f"Telegram webhook error: {e}")
        return {"ok": False}


@app.get("/telegram/setup-webhook")
async def setup_telegram_webhook():
    """Set up Telegram webhook - call this once after deployment"""
    webhook_url = "https://autopostleey-api-production.up.railway.app/telegram/webhook"
    async with _httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/setWebhook",
            json={"url": webhook_url}
        )
        return r.json()


# ── DISCORD OAUTH ─────────────────────────────────────────
DC_CLIENT_ID     = os.getenv("DISCORD_CLIENT_ID", "1516692416986484837")
DC_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET", "")
DC_REDIRECT      = "https://autopostleey.com/discord-callback.html"

@app.get("/discord/auth")
async def discord_auth(user_id: str = ""):
    """Redirect to Discord OAuth"""
    import urllib.parse
    params = {
        "client_id":     DC_CLIENT_ID,
        "redirect_uri":  DC_REDIRECT,
        "response_type": "code",
        "scope":         "webhook.incoming",
        "state":         user_id,
    }
    from fastapi.responses import RedirectResponse
    return RedirectResponse("https://discord.com/api/oauth2/authorize?" + urllib.parse.urlencode(params))


@app.get("/discord/callback")
async def discord_callback(code: str = "", state: str = "", error: str = ""):
    """Handle Discord OAuth callback"""
    from fastapi.responses import RedirectResponse
    import urllib.parse

    print(f"Discord callback received: code={bool(code)}, state={bool(state)}, error={error!r}")

    if error:
        print(f"Discord error param: {error}")
        return RedirectResponse("https://autopostleey.com/dashboard.html?dc_error=cancelled")

    if not code:
        print("Discord: no code received")
        return RedirectResponse("https://autopostleey.com/dashboard.html?dc_error=no_code")

    user_id = state
    try:
        print(f"Discord token exchange: client_id={DC_CLIENT_ID}, redirect={DC_REDIRECT}, secret_set={bool(DC_CLIENT_SECRET)}")
        async with _httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                "https://discord.com/api/oauth2/token",
                data={
                    "client_id":     DC_CLIENT_ID,
                    "client_secret": DC_CLIENT_SECRET,
                    "grant_type":    "authorization_code",
                    "code":          code,
                    "redirect_uri":  DC_REDIRECT,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"}
            )
            print(f"Discord token status: {r.status_code}, body: {r.text[:300]}")
            if not r.text.strip():
                return RedirectResponse("https://autopostleey.com/dashboard.html?dc_error=empty_response")
            data = r.json()

        if "error" in data:
            print(f"Discord token error: {data}")
            return RedirectResponse("https://autopostleey.com/dashboard.html?dc_error=token_failed")

        # Discord returns webhook info directly
        webhook = data.get("webhook", {})
        webhook_url  = webhook.get("url", "")
        channel_name = webhook.get("channel_id", "discord")
        guild_name   = webhook.get("guild", {}).get("name", "Discord Server")

        if not webhook_url:
            return RedirectResponse("https://autopostleey.com/dashboard.html?dc_error=no_webhook")

        # Save connection
        print(f"Saving Discord connection for user {user_id}, guild: {guild_name}, webhook: {bool(webhook_url)}")
        if SUPABASE_URL and user_id:
            conn_data = {
                "user_id":      user_id,
                "platform":     "discord",
                "access_token": webhook_url,  # store webhook_url in access_token column
                "page_name":    guild_name,
                "connected_at": datetime.utcnow().isoformat(),
            }
            async with _httpx.AsyncClient(timeout=10.0) as client:
                # Delete existing then insert fresh
                await client.delete(
                    f"{SUPABASE_URL}/rest/v1/autopostleey_connections",
                    params={"user_id": f"eq.{user_id}", "platform": "eq.discord"},
                    headers={"apikey": SUPABASE_SERVICE or SUPABASE_ANON, "Authorization": f"Bearer {SUPABASE_SERVICE or SUPABASE_ANON}"}
                )
                save_resp = await client.post(
                    f"{SUPABASE_URL}/rest/v1/autopostleey_connections",
                    headers={"apikey": SUPABASE_SERVICE or SUPABASE_ANON, "Authorization": f"Bearer {SUPABASE_SERVICE or SUPABASE_ANON}", "Content-Type": "application/json"},
                    json=conn_data
                )
                print(f"Discord save status: {save_resp.status_code}, body: {save_resp.text[:200]}")

        params = urllib.parse.urlencode({"dc_success": "1", "page_name": guild_name})
        return RedirectResponse(f"https://autopostleey.com/dashboard.html?{params}")

    except Exception as e:
        print(f"Discord OAuth error: {e}")
        import traceback
        traceback.print_exc()
        return RedirectResponse("https://autopostleey.com/dashboard.html?dc_error=server_error")
