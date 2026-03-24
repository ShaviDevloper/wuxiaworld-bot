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
TIMEOUT     = 60_000          # bumped to 60 s — the SPA is slow
STATE_FILE  = "wuxiaworld_state.json"
LOG_FILE    = "wuxiaworld_bot.log"
DEBUG_SHOTS = os.environ.get("CI", "false").lower() == "true"  # save debug screenshots in CI

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

# Counter for debug screenshots
_shot_counter = 0


def _dbg_shot_path(label: str) -> str:
    global _shot_counter
    _shot_counter += 1
    return f"debug_{_shot_counter:02d}_{label}.png"


async def debug_screenshot(page, label: str):
    """Save a screenshot for debugging (only in CI)."""
    if not DEBUG_SHOTS:
        return
    path = _dbg_shot_path(label)
    try:
        await page.screenshot(path=path, full_page=False)
        log.info("📸 Debug screenshot → %s", path)
    except Exception as e:
        log.debug("Could not save debug screenshot: %s", e)


# ══════════════════════════════════════════════════════════════
#  STEALTH — remove automation fingerprints
# ══════════════════════════════════════════════════════════════

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
Object.defineProperty(navigator, 'plugins', {
    get: () => [1, 2, 3, 4, 5],   // non-empty plugin list
});
window.chrome = { runtime: {} };
"""


# ══════════════════════════════════════════════════════════════
#  SPA WAIT HELPERS
# ══════════════════════════════════════════════════════════════

async def wait_for_spa_ready(page, timeout: int = 30_000):
    """Wait for WuxiaWorld's React SPA to finish its initial render.

    The site shows a "Loading..." placeholder while React hydrates.
    We wait until real content (nav bar links) appears.
    """
    log.info("Waiting for SPA to finish rendering …")
    try:
        # Wait for the nav bar to appear — a reliable SPA-ready signal
        await page.wait_for_selector(
            "header a[href*='/series'], "       # desktop nav
            "nav a[href*='/series'], "
            "a[href='/series']",
            timeout=timeout,
            state="visible",
        )
        # Extra settle time for late-loading popups
        await page.wait_for_timeout(3_000)
        log.info("SPA is ready ✓")
    except PWTimeoutError:
        log.warning("SPA ready check timed out — proceeding anyway")


async def safe_goto(page, url: str, wait: str = "domcontentloaded"):
    """Navigate to a URL using domcontentloaded (faster & more reliable for SPAs)
    then wait for the SPA to finish rendering."""
    try:
        await page.goto(url, wait_until=wait, timeout=TIMEOUT)
    except PWTimeoutError:
        log.warning("Navigation to %s timed out, continuing …", url)
    await wait_for_spa_ready(page)


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
    """Detect and dismiss any WuxiaWorld popup/modal that might block interactions."""
    dismissed = False

    # ── 1. MUI / generic dialog ──
    dialog_selectors = [
        ".MuiDialog-root",
        "[role='dialog']",
        ".streaks-dialog",
        "div[class*='dialog']",
        "div[class*='modal']",
    ]
    for sel in dialog_selectors:
        try:
            dlg = await page.wait_for_selector(sel, timeout=timeout, state="visible")
            if dlg:
                log.info("Detected popup via %s", sel)
                # Try close button
                close = await page.query_selector(
                    f"{sel} button[aria-label='close'], "
                    f"{sel} .MuiIconButton-root, "
                    f"{sel} button:has(svg[data-testid='CloseIcon']), "
                    f"{sel} button:has(svg), "
                    "button[aria-label='Close']"
                )
                if close and await close.is_visible():
                    await close.click()
                    log.info("Closed popup via close button ✓")
                    dismissed = True
                else:
                    await page.keyboard.press("Escape")
                    log.info("Closed popup via Escape ✓")
                    dismissed = True
                await page.wait_for_timeout(500)
                break
        except PWTimeoutError:
            continue

    # ── 2. Cookie / consent banners ──
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
            "a[href*='/profile'], "
            "[data-testid='user-menu'], "
            ".user-avatar, img.avatar, "
            "[class*='username'], "
            "img[alt*='avatar'], "
            "button[aria-label*='profile'], "
            "a[href*='/account']",
            timeout=8_000,
        )
        return True
    except PWTimeoutError:
        return False


async def login(page, context):
    log.info("Navigating to login page …")
    await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=TIMEOUT)
    # Wait for the login form to be ready
    try:
        await page.wait_for_selector("#Email", timeout=15_000, state="visible")
    except PWTimeoutError:
        log.warning("Login form did not appear, taking screenshot …")
        await debug_screenshot(page, "login_form_missing")

    await debug_screenshot(page, "login_page")

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
        await debug_screenshot(page, "login_failed")
        if not await is_logged_in(page):
            log.error("Login failed — check WW_EMAIL / WW_PASSWORD credentials.")
            raise RuntimeError("Login failed.")

    await debug_screenshot(page, "after_login")
    await save_state(context)


# ══════════════════════════════════════════════════════════════
#  DAILY LOGIN REWARD COLLECTION
# ══════════════════════════════════════════════════════════════

async def collect_daily_login_reward(page) -> bool:
    """Collect the daily login key reward from the streaks popup.

    WuxiaWorld shows a 'Daily rewards' dialog (MUI Dialog) after login.
    It contains a streak tracker and an action button.

    Returns True if a reward was collected.
    """
    log.info("── Collecting daily login reward ──")

    # Navigate to homepage to trigger the popup
    await safe_goto(page, BASE_URL)

    await debug_screenshot(page, "homepage_before_popup")

    # Wait for the daily rewards dialog to appear.
    # The popup is rendered by React and may take several seconds.
    popup = None
    popup_selectors = [
        ".MuiDialog-root",
        "[role='dialog']",
        ".streaks-dialog",
        "div[class*='streak']",
        "div[class*='daily-reward']",
        "div[class*='reward-dialog']",
    ]

    for sel in popup_selectors:
        try:
            popup = await page.wait_for_selector(
                sel, timeout=20_000, state="visible"
            )
            if popup:
                log.info("Daily reward popup appeared via %s ✓", sel)
                break
        except PWTimeoutError:
            continue

    if not popup:
        log.info("No daily reward popup detected after waiting 20 s")
        await debug_screenshot(page, "no_popup_found")

        # Fallback: try clicking the key/rewards icon in the nav bar
        # to manually open the rewards dialog
        key_icon_selectors = [
            "button[aria-label*='reward']",
            "button[aria-label*='key']",
            "a[href*='reward']",
            "[class*='key-icon']",
            "header button:has(svg)",
        ]
        for sel in key_icon_selectors:
            try:
                icons = await page.query_selector_all(sel)
                for icon in icons:
                    if await icon.is_visible():
                        log.info("Trying to open rewards via icon: %s", sel)
                        await icon.click()
                        await page.wait_for_timeout(3_000)
                        await debug_screenshot(page, "after_icon_click")
                        # Check if dialog appeared
                        for dsel in popup_selectors:
                            try:
                                popup = await page.wait_for_selector(
                                    dsel, timeout=5_000, state="visible"
                                )
                                if popup:
                                    log.info("Popup appeared after clicking icon ✓")
                                    break
                            except PWTimeoutError:
                                continue
                        if popup:
                            break
            except Exception:
                continue
            if popup:
                break

    if not popup:
        log.warning("Could not find daily reward popup — skipping")
        await debug_screenshot(page, "popup_fallback_failed")
        return False

    # Small pause to let the dialog fully render
    await page.wait_for_timeout(2_000)
    await debug_screenshot(page, "popup_visible")

    # ── Try to click the action / claim button ──
    action_selectors = [
        # Specific reward buttons
        ".streaks-action-button",
        "button[class*='action']",
        "button:has-text('Claim')",
        "button:has-text('Collect')",
        "button:has-text('Check In')",
        "button:has-text('Sign In')",
        # MUI primary button inside the dialog
        ".MuiDialog-root .MuiButton-containedPrimary",
        ".MuiDialog-root button.MuiButton-root",
        "[role='dialog'] button[class*='action']",
        "[role='dialog'] button:has-text('Claim')",
        "[role='dialog'] button:has-text('Collect')",
    ]

    for sel in action_selectors:
        try:
            btn = await page.query_selector(sel)
            if btn and await btn.is_visible():
                btn_text = ""
                try:
                    btn_text = (await btn.inner_text()).strip()
                except Exception:
                    pass
                # Skip the "CONTINUE READING" button — it's not the claim button
                if btn_text.upper() in ("CONTINUE READING", ""):
                    continue
                log.info("Found reward button: [%s] via %s", btn_text, sel)
                await btn.click()
                await page.wait_for_timeout(2_000)
                await debug_screenshot(page, "after_claim_click")

                # Check if a secondary confirmation or result appeared
                try:
                    result = await page.wait_for_selector(
                        "[class*='result'], [class*='success'], "
                        "[class*='reward'], [class*='key'], "
                        "[class*='congratulation']",
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

    # If we saw the popup but couldn't find a dedicated claim button,
    # the popup might show "CONTINUE READING" which means today's
    # reward was auto-claimed on popup display (some days work this way).
    log.info("Popup was visible but no dedicated claim button found")
    log.info("Checking for 'CONTINUE READING' — reward may be auto-claimed")

    try:
        cont_btn = await page.query_selector(
            "button:has-text('CONTINUE READING'), "
            "button:has-text('Continue Reading'), "
            "[role='dialog'] button"
        )
        if cont_btn and await cont_btn.is_visible():
            fb_text = ""
            try:
                fb_text = (await cont_btn.inner_text()).strip()
            except Exception:
                pass
            log.info("Clicking dialog button: [%s]", fb_text)
            await cont_btn.click()
            await page.wait_for_timeout(1_000)
            log.info("Daily reward likely auto-collected on popup display ✓")
            return True
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
    await safe_goto(page, CHECKIN_URL)

    await debug_screenshot(page, "checkin_page")

    # Dismiss any popup that might overlay the check-in page
    await dismiss_popup(page, timeout=3_000)

    selectors = [
        "button:has-text('Check In')",
        "button:has-text('Sign In')",
        "button:has-text('Attend')",
        ".attendance-btn:not(.disabled):not(.checked)",
        "[class*='checkin']:not([class*='disabled']):not([class*='checked'])",
        "button[class*='attend']:not([disabled])",
        # MUI buttons
        ".MuiButton-root:has-text('Check')",
        ".MuiButton-root:has-text('Attend')",
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
        await debug_screenshot(page, "checkin_btn_missing")
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

    await debug_screenshot(page, "after_checkin")
    return True


# ══════════════════════════════════════════════════════════════
#  MISSIONS
# ══════════════════════════════════════════════════════════════

async def do_missions(page):
    log.info("Navigating to missions page …")
    await safe_goto(page, MISSIONS_URL)

    await debug_screenshot(page, "missions_page")

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
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-infobars",
                "--disable-dev-shm-usage",
            ],
        )

        ctx_kwargs = {
            "viewport": {"width": 1280, "height": 800},
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/145.0.0.0 Safari/537.36"
            ),
            "locale": "en-US",
            "timezone_id": "America/New_York",
        }
        if saved_state:
            ctx_kwargs["storage_state"] = saved_state

        context = await browser.new_context(**ctx_kwargs)
        context.set_default_timeout(TIMEOUT)
        page = await context.new_page()

        # ── Inject stealth scripts ──
        await page.add_init_script(STEALTH_JS)

        try:
            await safe_goto(page, BASE_URL)

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
