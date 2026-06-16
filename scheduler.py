"""
Autopostleey Auto-Scheduler
Runs every 15 minutes via GitHub Actions
Checks for posts due to be published and publishes them
"""
import os, asyncio, json
from datetime import datetime, timezone, timedelta
import httpx

# ── CONFIG ────────────────────────────────────────────────
SUPABASE_URL     = os.getenv("SUPABASE_URL", "https://gkxpwqryakgzgvvprbkl.supabase.co")
SUPABASE_SERVICE = os.getenv("SUPABASE_SERVICE_KEY", "")
CF_WORKER_URL    = os.getenv("CF_WORKER_URL", "https://autopostleey-ai.alexclaor.workers.dev")
DRY_RUN          = os.getenv("DRY_RUN", "false").lower() == "true"

print(f"🚀 Autopostleey Scheduler starting...")
print(f"   Time: {datetime.now(timezone.utc).isoformat()}")
print(f"   Dry run: {DRY_RUN}")


async def get_due_posts():
    """Get posts that are scheduled and due to be published"""
    now = datetime.now(timezone.utc)
    # Get posts scheduled up to 30 mins in the future (buffer for delays)
    window_end = (now + timedelta(minutes=30)).isoformat()

    headers = {
        "apikey":        SUPABASE_SERVICE,
        "Authorization": f"Bearer {SUPABASE_SERVICE}",
        "Content-Type":  "application/json"
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(
            f"{SUPABASE_URL}/rest/v1/autopostleey_posts",
            params={
                "status":       "eq.scheduled",
                "scheduled_at": f"lte.{window_end}",
                "select":       "*",
                "order":        "scheduled_at.asc",
                "limit":        "50"
            },
            headers=headers
        )
        if r.status_code != 200:
            print(f"❌ Failed to fetch posts: {r.status_code} {r.text}")
            return []
        return r.json()


async def get_user_connections(user_id: str):
    """Get user platform connections"""
    headers = {
        "apikey":        SUPABASE_SERVICE,
        "Authorization": f"Bearer {SUPABASE_SERVICE}",
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(
            f"{SUPABASE_URL}/rest/v1/autopostleey_connections",
            params={"user_id": f"eq.{user_id}", "select": "*"},
            headers=headers
        )
        if r.status_code == 200:
            rows = r.json()
            return {row["platform"]: row for row in rows}
    return {}


async def get_user_email(user_id: str) -> str:
    """Get user email from Supabase auth"""
    headers = {
        "apikey":        SUPABASE_SERVICE,
        "Authorization": f"Bearer {SUPABASE_SERVICE}",
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(
            f"{SUPABASE_URL}/auth/v1/admin/users/{user_id}",
            headers=headers
        )
        if r.status_code == 200:
            return r.json().get("email", "")
    return ""


async def publish_post(post: dict, connections: dict) -> dict:
    """Publish a post via Cloudflare Worker"""
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(
            f"{CF_WORKER_URL}/publish",
            json={
                "post_id":     post["id"],
                "content":     post["content"],
                "platforms":   post.get("platforms", []),
                "user_id":     post["user_id"],
                "connections": connections
            },
            headers={"Content-Type": "application/json"}
        )
        if r.status_code == 200:
            return r.json()
        return {"status": "failed", "success": 0, "failed": 1, "error": f"HTTP {r.status_code}"}


async def update_post_status(post_id: str, status: str, error: str = None):
    """Update post status in Supabase"""
    headers = {
        "apikey":        SUPABASE_SERVICE,
        "Authorization": f"Bearer {SUPABASE_SERVICE}",
        "Content-Type":  "application/json"
    }
    update_data = {
        "status":    status,
        "posted_at": datetime.now(timezone.utc).isoformat() if status == "posted" else None
    }
    if error:
        update_data["error_msg"] = error[:500]

    async with httpx.AsyncClient(timeout=10.0) as client:
        await client.patch(
            f"{SUPABASE_URL}/rest/v1/autopostleey_posts",
            params={"id": f"eq.{post_id}"},
            headers=headers,
            json=update_data
        )


async def main():
    if not SUPABASE_SERVICE:
        print("❌ No SUPABASE_SERVICE_KEY — cannot run scheduler")
        return

    # Get due posts
    posts = await get_due_posts()
    print(f"📋 Found {len(posts)} post(s) due for publishing")

    if not posts:
        print("✅ Nothing to publish right now")
        return

    success_count = 0
    failed_count  = 0

    for post in posts:
        post_id   = post["id"]
        user_id   = post["user_id"]
        platforms = post.get("platforms", [])
        content   = post.get("content", "")[:100]

        print(f"\n📤 Processing post {post_id[:8]}...")
        print(f"   Platforms: {platforms}")
        print(f"   Content: {content}...")
        print(f"   Scheduled: {post.get('scheduled_at')}")

        if DRY_RUN:
            print(f"   [DRY RUN] Would publish to {platforms}")
            continue

        # Get user connections
        connections = await get_user_connections(user_id)
        if not connections:
            print(f"   ⚠️ No connections found for user {user_id[:8]}")
            await update_post_status(post_id, "failed", "No platform connections found")
            failed_count += 1
            continue

        # Check all requested platforms have connections
        missing = [p for p in platforms if p not in connections]
        if missing:
            print(f"   ⚠️ Missing connections for: {missing}")

        # Publish
        try:
            result = await publish_post(post, connections)
            print(f"   Result: {result.get('status')} — {result.get('success', 0)} succeeded, {result.get('failed', 0)} failed")

            if result.get("success", 0) > 0:
                await update_post_status(post_id, "posted")
                success_count += 1
                print(f"   ✅ Published successfully!")

                # Log results
                for r in result.get("results", []):
                    if r.get("success"):
                        print(f"      ✅ {r.get('platform')}: {r.get('post_id', 'ok')}")
                    else:
                        print(f"      ❌ {r.get('platform')}: {r.get('error')}")
            else:
                errors = [r.get("error", "") for r in result.get("results", []) if not r.get("success")]
                error_msg = "; ".join(filter(None, errors)) or "Publish failed"
                await update_post_status(post_id, "failed", error_msg)
                failed_count += 1
                print(f"   ❌ Failed: {error_msg}")

        except Exception as e:
            print(f"   ❌ Exception: {e}")
            await update_post_status(post_id, "failed", str(e))
            failed_count += 1

    print(f"\n✅ Scheduler complete: {success_count} published, {failed_count} failed")


if __name__ == "__main__":
    asyncio.run(main())
