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
LOGIN_URL    = "https://identity.wuxiaworld.com/Account/Login"
REWARDS_URL  = f"{BASE_URL}/manage/subscriptions/daily-rewards"
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
    # Quick negative check: if a "LOG IN" button is visible, we are NOT logged in
    try:
        login_btn = await page.query_selector(
            "button:has-text('LOG IN'), "
            "a:has-text('LOG IN'), "
            "button:has-text('Log In'), "
            "a:has-text('Sign In')"
        )
        if login_btn and await login_btn.is_visible():
            log.info("'LOG IN' button visible → not logged in")
            return False
    except Exception:
        pass

    # Positive check: look for elements that only appear when logged in
    logged_in_selectors = [
        "button[aria-label*='notification' i]",
        "div.MuiBadge-root",
        "a[href*='/notifications' i]",
        "a[href*='/profile' i]",
        "a[href*='/manage' i]",
        "a[href*='/account' i]",
        "button[aria-label*='account' i]",
    ]
    selector_string = ", ".join(logged_in_selectors)
    try:
        await page.wait_for_selector(selector_string, timeout=10_000)
        return True
    except PWTimeoutError:
        log.info("No logged-in indicators found on page")
        return False


async def login(page, context):
    log.info("Navigating to login page …")
    await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=TIMEOUT)
    # Wait for the login form to be ready
    try:
        await page.wait_for_selector("#Username", timeout=15_000, state="visible")
    except PWTimeoutError:
        log.warning("Login form did not appear, taking screenshot …")
        await debug_screenshot(page, "login_form_missing")

    await debug_screenshot(page, "login_page")

    await page.fill("#Username", EMAIL)
    await page.fill("#Password", PASSWORD)

    log.info("Submitting credentials …")
    await page.click("button:has-text('Log in'), button[type='submit']")

    # ── Wait for the cross-domain redirect chain to finish ──
    # identity.wuxiaworld.com → www.wuxiaworld.com callback → www.wuxiaworld.com
    # The identity server redirects back with auth tokens; the SPA on the main
    # domain needs to hydrate and establish the session.
    try:
        # First wait: leave the identity domain
        await page.wait_for_url(
            lambda u: "identity.wuxiaworld.com" not in u,
            timeout=TIMEOUT,
        )
        log.info("Redirected away from identity server → %s", page.url)
    except PWTimeoutError:
        log.error("Never redirected away from identity server")
        await debug_screenshot(page, "login_stuck_on_identity")
        raise RuntimeError("Login failed — stuck on identity page.")

    await debug_screenshot(page, "after_redirect")

    # ── Navigate to the main site and wait for the SPA to fully load ──
    log.info("Navigating to main site to verify session …")
    await safe_goto(page, BASE_URL)
    await debug_screenshot(page, "main_site_after_login")

    # ── Verify we are actually logged in ──
    if await is_logged_in(page):
        log.info("Login verified on main site ✓")
    else:
        log.warning("Not logged in after first attempt, retrying navigation …")
        # Sometimes the SPA needs an extra load to pick up the auth cookies
        await page.wait_for_timeout(5_000)
        await page.reload(wait_until="domcontentloaded")
        await wait_for_spa_ready(page)
        await debug_screenshot(page, "main_site_retry")

        if await is_logged_in(page):
            log.info("Login verified on retry ✓")
        else:
            await debug_screenshot(page, "login_failed")
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

    # Navigate to rewards page to trigger the popup
    await safe_goto(page, REWARDS_URL)

    await debug_screenshot(page, "rewards_page_before_popup")

    # ── Check for "Access Denied" — means session is invalid ──
    try:
        access_denied = await page.query_selector("text='Access Denied'")
        if access_denied and await access_denied.is_visible():
            log.error("Rewards page shows 'Access Denied' — not authenticated!")
            await debug_screenshot(page, "rewards_access_denied")
            # Try clicking the "Click here to login" link to re-authenticate
            login_link = await page.query_selector(
                "a:has-text('Click here to login'), "
                "a:has-text('login'), "
                "button:has-text('login')"
            )
            if login_link and await login_link.is_visible():
                log.info("Clicking 'Click here to login' to re-authenticate …")
                await login_link.click()
                await page.wait_for_timeout(3_000)
            return False
    except Exception:
        pass

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
