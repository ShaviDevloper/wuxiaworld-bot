"""
WuxiaWorld Daily Auto Sign-In Bot
==================================
Automates daily check-in and mission rewards on wuxiaworld.com.
Uses email + password login.

Setup:
    pip install playwright
    playwright install chromium

Usage:
    python wuxiaworld_bot.py

Credentials via environment variables (recommended):
    export WW_EMAIL="your_email@example.com"
    export WW_PASSWORD="your_password"
"""

import asyncio
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright, TimeoutError as PWTimeoutError

# ─────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────
EMAIL       = os.environ.get("WW_EMAIL",    "your_email@example.com")
PASSWORD    = os.environ.get("WW_PASSWORD", "your_password")

HEADLESS    = os.environ.get("CI", "false").lower() == "true"  # auto headless on GitHub Actions
SLOW_MO     = 200
TIMEOUT     = 30_000
STATE_FILE  = "wuxiaworld_state.json"
LOG_FILE    = "wuxiaworld_bot.log"

BASE_URL     = "https://www.wuxiaworld.com"
LOGIN_URL    = f"{BASE_URL}/account/login"
CHECKIN_URL  = f"{BASE_URL}/profile/monthly-attendance"
MISSIONS_URL = f"{BASE_URL}/profile/missions"
# ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
log = logging.getLogger("ww_bot")


# ══════════════════════════════════════════════════════════════
#  SESSION HELPERS
# ══════════════════════════════════════════════════════════════

async def save_state(context):
    state = await context.storage_state()
    Path(STATE_FILE).write_text(json.dumps(state))
    log.info("Session saved → %s", STATE_FILE)


def load_state() -> dict | None:
    if Path(STATE_FILE).exists():
        try:
            state = json.loads(Path(STATE_FILE).read_text())
            log.info("Loaded saved session from %s ✓", STATE_FILE)
            return state
        except Exception as e:
            log.warning("Could not load session file: %s", e)
    return None


# ══════════════════════════════════════════════════════════════
#  POPUP / OVERLAY HELPERS
# ══════════════════════════════════════════════════════════════

async def dismiss_popup(page, timeout: int = 3_000) -> bool:
    """Detect and dismiss any WuxiaWorld popup/modal that might block interactions.

    Handles the daily-rewards streaks dialog, generic MUI dialogs,
    cookie banners, and other overlays.

    Returns True if a popup was dismissed.
    """
    dismissed = False

    # ── 1. Streaks / daily-reward dialog ──
    try:
        streaks = await page.wait_for_selector(
            ".streaks-dialog, .MuiDialog-root.streaks-dialog",
            timeout=timeout,
            state="visible",
        )
        if streaks:
            log.info("Detected streaks/daily-reward popup")
            # Try the close (X) button first
            close_btn = await page.query_selector(
                ".streaks-dialog button[aria-label='close'], "
                ".streaks-dialog .MuiIconButton-root, "
                ".streaks-dialog button:has(svg)"
            )
            if close_btn:
                await close_btn.click()
                log.info("Closed streaks popup via close button ✓")
                dismissed = True
            else:
                # Press Escape as fallback
                await page.keyboard.press("Escape")
                log.info("Closed streaks popup via Escape ✓")
                dismissed = True
            await page.wait_for_timeout(500)
    except PWTimeoutError:
        pass

    # ── 2. Generic MUI dialog overlay ──
    if not dismissed:
        try:
            mui_backdrop = await page.query_selector(
                ".MuiBackdrop-root, .MuiDialog-root"
            )
            if mui_backdrop and await mui_backdrop.is_visible():
                await page.keyboard.press("Escape")
                log.info("Dismissed generic MUI dialog via Escape ✓")
                dismissed = True
                await page.wait_for_timeout(500)
        except Exception:
            pass

    # ── 3. Cookie / consent banners ──
    try:
        cookie_btn = await page.query_selector(
            "button:has-text('Accept'), button:has-text('Got it'), "
            "button:has-text('I agree'), [class*='cookie'] button"
        )
        if cookie_btn and await cookie_btn.is_visible():
            await cookie_btn.click()
            log.info("Dismissed cookie/consent banner ✓")
            dismissed = True
            await page.wait_for_timeout(500)
    except Exception:
        pass

    return dismissed


# ══════════════════════════════════════════════════════════════
#  LOGIN
# ══════════════════════════════════════════════════════════════

async def is_logged_in(page) -> bool:
    try:
        await page.wait_for_selector(
            "a[href*='/profile'], [data-testid='user-menu'], "
            ".user-avatar, img.avatar, [class*='username']",
            timeout=6_000,
        )
        return True
    except PWTimeoutError:
        return False


async def login(page, context):
    log.info("Navigating to login page …")
    await page.goto(LOGIN_URL, wait_until="networkidle")

    await page.fill("#Email", EMAIL)
    await page.fill("#Password", PASSWORD)

    # Check "Remember Me" if available
    try:
        remember = await page.query_selector("#RememberMe")
        if remember and not await remember.is_checked():
            await remember.check()
            log.info("Checked 'Remember Me'")
    except Exception:
        pass

    log.info("Submitting credentials …")
    await page.click("button.btn-inverse, button:has-text('Sign In')")

    try:
        await page.wait_for_url(lambda u: "login" not in u, timeout=TIMEOUT)
        log.info("Login successful ✓")
    except PWTimeoutError:
        if not await is_logged_in(page):
            log.error("Login failed — check WW_EMAIL / WW_PASSWORD credentials.")
            raise RuntimeError("Login failed.")

    await save_state(context)


async def collect_daily_login_reward(page) -> bool:
    """Collect the daily login key reward from the streaks popup.

    WuxiaWorld shows a 'Daily rewards' popup (streaks-dialog) after login.
    It contains a streak tracker and a claim button (streaks-action-button).

    Returns True if a reward was collected.
    """
    log.info("── Collecting daily login reward ──")

    # Navigate to homepage to trigger the popup
    try:
        await page.goto(BASE_URL, wait_until="networkidle")
    except PWTimeoutError:
        log.warning("Homepage load timed out, still checking for popup …")

    # Wait for the streaks dialog to appear
    try:
        await page.wait_for_selector(
            ".streaks-dialog, .MuiDialog-root.streaks-dialog",
            timeout=10_000,
            state="visible",
        )
        log.info("Daily reward popup appeared ✓")
    except PWTimeoutError:
        log.info("No daily reward popup detected — may already be collected")
        return False

    # Small pause to let the dialog fully render
    await page.wait_for_timeout(1_500)

    # ── Try to click the action button ──
    action_selectors = [
        ".streaks-action-button",
        ".streaks-dialog button[class*='action']",
        ".streaks-dialog button:has-text('Claim')",
        ".streaks-dialog button:has-text('Collect')",
        ".streaks-dialog button:has-text('Check In')",
        ".streaks-dialog button:has-text('Sign In')",
    ]

    for sel in action_selectors:
        try:
            btn = await page.query_selector(sel)
            if btn and await btn.is_visible():
                btn_text = (await btn.inner_text()).strip()
                log.info("Found reward button: [%s] via %s", btn_text, sel)
                await btn.click()
                await page.wait_for_timeout(2_000)

                # Check if a secondary confirmation or result appeared
                try:
                    result = await page.wait_for_selector(
                        ".streaks-dialog [class*='result'], "
                        ".streaks-dialog [class*='success'], "
                        ".streaks-dialog [class*='reward'], "
                        ".streaks-dialog [class*='key']",
                        timeout=5_000,
                    )
                    if result:
                        log.info("Daily reward result confirmed ✓")
                except PWTimeoutError:
                    log.info("Reward button clicked (no result element detected)")

                # Dismiss the dialog after claiming
                await dismiss_popup(page, timeout=2_000)
                log.info("Daily login reward collected! ✓")
                return True
        except Exception as e:
            log.debug("Selector %s failed: %s", sel, e)
            continue

    # If we saw the popup but couldn't click any button, try the
    # "Continue Reading" / generic dismiss button
    try:
        fallback = await page.query_selector(
            ".streaks-dialog button, .streaks-dialog a[role='button']"
        )
        if fallback and await fallback.is_visible():
            fb_text = (await fallback.inner_text()).strip()
            log.info("Clicking fallback button in reward popup: [%s]", fb_text)
            await fallback.click()
            await page.wait_for_timeout(1_000)
    except Exception:
        pass

    await dismiss_popup(page, timeout=2_000)
    log.info("Daily reward popup handled (reward may already be collected)")
    return False


# ══════════════════════════════════════════════════════════════
#  DAILY CHECK-IN
# ══════════════════════════════════════════════════════════════

async def do_checkin(page) -> bool:
    log.info("Navigating to check-in page …")
    await page.goto(CHECKIN_URL, wait_until="networkidle")

    # Dismiss any popup that might overlay the check-in page
    await dismiss_popup(page, timeout=3_000)

    selectors = [
        "button:has-text('Check In')",
        "button:has-text('Sign In')",
        "button:has-text('Attend')",
        ".attendance-btn:not(.disabled):not(.checked)",
        "[class*='checkin']:not([class*='disabled']):not([class*='checked'])",
        "button[class*='attend']:not([disabled])",
    ]

    btn = None
    for sel in selectors:
        try:
            btn = await page.wait_for_selector(sel, timeout=4_000, state="visible")
            if btn:
                log.info("Found check-in button via: %s", sel)
                break
        except PWTimeoutError:
            continue

    if not btn:
        already = await page.query_selector(
            ".checked, .today.active, [class*='today'][class*='done'], "
            "button:disabled:has-text('Check')"
        )
        if already:
            log.info("Already checked in today ✓")
            return False
        log.warning("Check-in button not found — site layout may have changed.")
        return False

    await btn.click()
    log.info("Clicked check-in button …")

    try:
        await page.wait_for_selector(
            ".success, .modal, [class*='reward'], [class*='key'], .notification",
            timeout=8_000,
        )
        log.info("Check-in SUCCESS — keys collected! ✓")
    except PWTimeoutError:
        log.info("Check-in clicked (no popup detected, may still be OK)")

    return True


# ══════════════════════════════════════════════════════════════
#  MISSIONS
# ══════════════════════════════════════════════════════════════

async def do_missions(page):
    log.info("Navigating to missions page …")
    await page.goto(MISSIONS_URL, wait_until="networkidle")
    await page.wait_for_timeout(2_000)

    # Dismiss any popup that might overlay the missions page
    await dismiss_popup(page, timeout=3_000)

    claim_selectors = [
        "button:has-text('Claim')",
        "button:has-text('Collect')",
        "button:has-text('Receive')",
        "[class*='claim']:not([disabled])",
        "[class*='collect']:not([disabled])",
    ]

    claimed = 0
    for sel in claim_selectors:
        buttons = await page.query_selector_all(sel)
        for btn in buttons:
            try:
                if await btn.get_attribute("disabled") is not None:
                    continue
                text = (await btn.inner_text()).strip()
                log.info("Claiming mission reward: [%s]", text)
                await btn.scroll_into_view_if_needed()
                await btn.click()
                await page.wait_for_timeout(1_500)
                claimed += 1
            except Exception as e:
                log.debug("Skipped a button: %s", e)

    if claimed:
        log.info("Missions claimed: %d reward(s) ✓", claimed)
    else:
        log.info("No claimable missions found today")


# ══════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════

async def main():
    log.info("=" * 55)
    log.info("WuxiaWorld Bot — %s", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    log.info("=" * 55)

    saved_state = load_state()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=HEADLESS,
            slow_mo=SLOW_MO,
            args=["--disable-blink-features=AutomationControlled"],
        )

        ctx_kwargs = {
            "viewport": {"width": 1280, "height": 800},
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
        }
        if saved_state:
            ctx_kwargs["storage_state"] = saved_state

        context = await browser.new_context(**ctx_kwargs)
        context.set_default_timeout(TIMEOUT)
        page = await context.new_page()

        try:
            await page.goto(BASE_URL, wait_until="networkidle")

            if not await is_logged_in(page):
                log.info("Not logged in — performing login …")
                await login(page, context)
            else:
                log.info("Already logged in via saved session ✓")

            # ── Collect daily login key reward (popup after login) ──
            await collect_daily_login_reward(page)

            await do_checkin(page)
            await do_missions(page)
            await save_state(context)

            log.info("All done! ✓")

        except Exception as e:
            log.error("Bot error: %s", e, exc_info=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            await page.screenshot(path=f"error_{ts}.png")
            log.info("Error screenshot saved → error_%s.png", ts)

        finally:
            await context.close()
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
