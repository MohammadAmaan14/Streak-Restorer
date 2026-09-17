"""
Combined score-checker + forum form filler.

Step 1: scans the friends list at SCORE_SITE_URL using SeleniumBase UC mode
        (persistent Chrome profile) to find which of TARGETS currently show
        a lost score.
Step 2: maps each "lost" name to its forum username via NAME_TO_USERNAME,
        then runs the existing sb_cdp + Playwright form-filler logic once
        per mapped username as the friend's username, logging results
        to results.csv exactly as before.

Setup (unchanged from the two source scripts):
  pip install seleniumbase playwright

config.json still needs: default_url, my_name, my_email, my_phone,
selectors (name_field/email_field/phone_field/customer_name_field/
submit_button), and optionally results_log, typing_delay_ms,
delay_before_typing, delay_between_fields, delay_after_submit,
delay_between_customers, captcha_solve_wait, url_change_wait,
resubmit_after_captcha, headless, use_chromium, user_data_dir.

The names_file / names.txt flow from the original form filler is no
longer used — the list of friend usernames now comes from Step 1.
"""

import argparse
import csv
import json
import sys
import time
from pathlib import Path

from seleniumbase import Driver, sb_cdp
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).parent

# ---------------------------------------------------------------------------
# Step 1 — score checker config
# ---------------------------------------------------------------------------

TARGETS = [
    'Mohammed Abdul Rafay', 'Kamina Bhai', 'Mohammed Rahman', 'Saq',
    'Barish', 'Dan3sh', 'Shaikh Hamza', 'Muhammed Fadil', 'sufyan',
    'Hamza Hi IQ', 'Ammar Feroz', 'لقمان~'
]

# Friend display name -> forum username to submit for
# them if their score is found to be lost.
NAME_TO_USERNAME = {
    'Mohammed Abdul Rafay': 'm_abdulrafa2448',
    'Kamina Bhai': 'izme_noman',
    'Mohammed Rahman': 'mdrahman_16',
    'Saq': 's_qeee3i',
    'Barish': 'danish.gg15',
    'Dan3sh': 'sheik_danesh',
    'Shaikh Hamza': 'shaikh-hamza211',
    'Muhammed Fadil': 'muhmedfadil',
    'sufyan': 'suufyxn',
    'Hamza Hi IQ': 'hamzaaqsr',
    'Ammar Feroz': 'axxar.sa7',
    'لقمان~': 'flick_z21',
}

SCORE_SITE_URL = "https://www.snapchat.com/web"

# Persistent Chrome profile for the score-check step (kept separate from
# the form filler's own profile since it's a different logged-in site).
SCORE_CHECK_USER_DATA_DIR = ROOT / 'my-profile'

SCAN_JS = r"""
const normalise = s => s?.trim().replace(/\s+/g, ' ');
const results = {};
document.querySelectorAll('.O4POs').forEach(row => {
    const nameEl = row.querySelector('[id^="title-"]');
    const name = normalise(nameEl?.innerText);
    if (!name) return;
    const meta = row.querySelector('.ovUsZ');
    const scoreEl = meta
        ? [...meta.querySelectorAll('span')].find(s => /^\d+\s*\ud83d\udd25$/.test(s.innerText.trim()))
        : null;
    results[name] = {
        score: scoreEl ? parseInt(scoreEl.innerText) : null,
        hasscore: !!scoreEl
    };
});
return results;
"""

# The friend list is virtualized (react-window style): only rows near the
# viewport actually exist in the DOM, positioned with `top: Npx` inside a
# scrollable ancestor, and get unmounted/recycled as you scroll past them.
# So we can't track progress by counting .O4POs elements (that count stays
# roughly flat no matter how far down the list you are) — instead we find
# the real scrollable container and drive + measure *its* scrollTop.
SCROLL_STEP_JS = r"""
function getScrollContainer() {
    const item = document.querySelector('.O4POs');
    if (!item) return document.scrollingElement || document.documentElement;
    let el = item.parentElement;
    while (el && el !== document.body) {
        const style = window.getComputedStyle(el);
        const scrollable = (style.overflowY === 'auto' || style.overflowY === 'scroll');
        if (scrollable && el.scrollHeight > el.clientHeight + 5) {
            return el;
        }
        el = el.parentElement;
    }
    return document.scrollingElement || document.documentElement;
}
const c = getScrollContainer();
const before = c.scrollTop;
c.scrollTop = before + Math.max(250, Math.round(c.clientHeight * 0.85));
return {
    scrollTop: c.scrollTop,
    scrollHeight: c.scrollHeight,
    clientHeight: c.clientHeight,
    moved: c.scrollTop !== before,
    atBottom: (c.scrollTop + c.clientHeight) >= (c.scrollHeight - 4)
};
"""

# True if any currently-rendered row's username hasn't lazy-loaded in yet
# (row/card exists, but the [id^="title-"] element is missing or empty).
NAMES_PENDING_JS = r"""
return [...document.querySelectorAll('.O4POs')].some(row => {
    const el = row.querySelector('[id^="title-"]');
    return !el || !el.innerText.trim().length;
});
"""


def wait_for_names(driver, timeout=2.5, poll_interval=0.15):
    """Usernames lazy-load in after their row/card is already on the page,
    so reading a row too early can catch it before the name (and thus the
    score next to it) is there. Poll briefly until every currently-rendered
    row has its name filled in, or we give up after `timeout` seconds
    (e.g. a row that's a placeholder/ad and never gets a title)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not driver.execute_script(NAMES_PENDING_JS):
            return
        time.sleep(poll_interval)


def scan_scores(driver, targets, max_iterations=250, stall_limit=4,
                  settle_timeout=1.0, poll_interval=0.15, name_load_timeout=2.5):
    """Scroll the (virtualized) friends list, scanning after each scroll
    step, until every target name has been seen or the scroll container
    genuinely can't move any further (checked via real scrollTop, not
    rendered-row count, since rows are recycled)."""
    results = {}
    stalls = 0

    for _ in range(max_iterations):
        # Let any rows that are currently on screen finish lazy-loading
        # their username before we read them — otherwise a row can get
        # skipped just because we caught it mid-render.
        wait_for_names(driver, timeout=name_load_timeout, poll_interval=poll_interval)

        scanned = driver.execute_script(SCAN_JS)
        for name, data in scanned.items():
            if name in targets:
                results[name] = data

        if all(t in results for t in targets):
            break

        step = driver.execute_script(SCROLL_STEP_JS)
        if not step:
            break

        # Give newly-mounted rows a moment to render, then let their names
        # finish lazy-loading before the next scan.
        time.sleep(settle_timeout)
        wait_for_names(driver, timeout=name_load_timeout, poll_interval=poll_interval)

        if not step["moved"]:
            stalls += 1
            if stalls >= stall_limit:
                print("Scroll container stopped moving — assuming end of friend list reached.")
                break
        else:
            stalls = 0

    # Final pass in case the last scroll's content settled after the loop exited
    wait_for_names(driver, timeout=name_load_timeout, poll_interval=poll_interval)
    scanned = driver.execute_script(SCAN_JS)
    for name, data in scanned.items():
        if name in targets:
            results[name] = data

    return results


def get_lost_score_names():
    """Runs the UC-mode score checker and returns the list of TARGET names
    whose score was found to be lost."""
    driver_kwargs = dict(
        browser="chrome",
        uc=True,
        headless=False,
        user_data_dir=str(SCORE_CHECK_USER_DATA_DIR),
        chromium_arg=(
            '--disable-blink-features=AutomationControlled,'
            '--no-sandbox,--disable-dev-shm-usage'
        ),
    )

    # Only wire up extensions if the folders actually exist next to this
    # script — passing a bad extension_dir path breaks launch.
    ublock_dir = ROOT / 'ublock'
    nopecha_dir = ROOT / 'NopeCHA'
    ext_dirs = [str(d) for d in (ublock_dir, nopecha_dir) if d.is_dir()]
    if ext_dirs:
        driver_kwargs['extension_dir'] = ','.join(ext_dirs)

    driver = Driver(**driver_kwargs)

    try:
        driver.get(SCORE_SITE_URL)
        print("Waiting for the app to hydrate...")
        time.sleep(5)  # let the SPA finish its initial render before scanning

        results = scan_scores(driver, TARGETS)

        still_going, lost, never_found = [], [], []
        for name in TARGETS:
            data = results.get(name)
            if data is None:
                never_found.append(name)
            elif data["hasscore"]:
                still_going.append((name, data["score"]))
            else:
                lost.append(name)

        print("\n🔥 score still active:")
        for name, score in still_going:
            print(f"  {name}: {score}")

        print("\n💔 score lost:")
        for name in lost:
            print(f"  {name}")

        if never_found:
            print("\n❓ Never found in list (scroll may have ended early, or removed as a friend):")
            for name in never_found:
                print(f"  {name}")

        return lost

    finally:
        driver.quit()


# ---------------------------------------------------------------------------
# Step 2 — forum form filler (unchanged logic; friend usernames now come
# from get_lost_score_names() instead of a names.txt file)
# ---------------------------------------------------------------------------

# Is any reCAPTCHA UI actually on screen right now?
# Covers both the checkbox widget ("anchor") and the image-grid popup
# ("bframe"). A hidden/zero-size iframe doesn't count.
JS_CAPTCHA_VISIBLE = """
() => {
  const frames = document.querySelectorAll('iframe[src*="recaptcha"]');
  for (const f of frames) {
    const src = f.src || '';
    if (!src.includes('anchor') && !src.includes('bframe')) continue;
    const box = f.getBoundingClientRect();
    if (box.width < 40 || box.height < 40) continue;
    if (box.bottom < 0 || box.right < 0) continue;
    let node = f, hidden = false;
    for (let i = 0; i < 4 && node; i++) {
      const st = window.getComputedStyle(node);
      if (st.visibility === 'hidden' || st.display === 'none' || st.opacity === '0') {
        hidden = true;
        break;
      }
      node = node.parentElement;
    }
    if (!hidden) return true;
  }
  return false;
}
"""


def safe_eval(page, script):
    """Evaluate JS in the page, swallowing transient navigation errors."""
    try:
        return page.evaluate(script)
    except Exception:
        return None


def captcha_visible(page) -> bool:
    return safe_eval(page, JS_CAPTCHA_VISIBLE) is True


def captcha_solved(page) -> bool:
    """
    True once the checkbox is ticked. Read from inside the anchor iframe,
    where reCAPTCHA adds the `recaptcha-checkbox-checked` class.
    """
    try:
        for frame in page.frames:
            url = frame.url or ""
            if "recaptcha" in url and "anchor" in url:
                checked = frame.evaluate(
                    "() => !!document.querySelector('.recaptcha-checkbox-checked')"
                )
                if checked:
                    return True
    except Exception:
        pass
    return False


def handle_captcha_and_wait(page, sb, url_before, submit_selector,
                            solve_timeout, url_change_timeout,
                            resubmit_after_captcha):
    """
    Called right after clicking submit.

    Returns (url_after, captcha_note).

    Flow:
      1. Poll for a URL change. If it happens, we're done.
      2. If a reCAPTCHA shows up, print a prompt and wait for it to be
         solved (checkbox ticked, or the widget goes away), pausing the
         failure clock for up to `solve_timeout`.
      3. After it clears, give the page a moment; if it still hasn't
         navigated, click submit once more.
      4. Resume the URL watch with a fresh timeout.
    """
    note = "none"
    deadline = time.time() + url_change_timeout

    while time.time() < deadline:
        if page.url != url_before:
            return page.url, note

        if captcha_visible(page):
            note = "shown"
            print(f"    -> reCAPTCHA appeared. Waiting for it to be solved "
                  f"(up to {int(solve_timeout)}s)...")
            solve_deadline = time.time() + solve_timeout
            cleared = False
            while time.time() < solve_deadline:
                if page.url != url_before:
                    return page.url, "shown-solved"
                if captcha_solved(page) or not captcha_visible(page):
                    cleared = True
                    break
                time.sleep(1.0)

            if not cleared:
                print("    -> Captcha not solved in time, skipping this one.")
                return page.url, "shown-timeout"

            note = "shown-solved"
            print("    -> Captcha cleared.")
            sb.sleep(2)

            # Some forms submit themselves once the captcha passes; others
            # need the button pressed again.
            if page.url == url_before and resubmit_after_captcha:
                try:
                    page.wait_for_selector(submit_selector, timeout=5000)
                    page.click(submit_selector)
                    print("    -> Re-clicked submit after captcha.")
                except Exception as e:
                    print(f"    -> Could not re-click submit: {e}")

            deadline = time.time() + url_change_timeout

        time.sleep(0.5)

    return page.url, note


# --------------------------------------------------------------------------
# config / io helpers
# --------------------------------------------------------------------------

def load_config(path: Path) -> dict:
    if not path.exists():
        sys.exit(f"[!] Config file not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def init_results_log(path: Path):
    is_new = not path.exists()
    f = open(path, "a", newline="", encoding="utf-8")
    writer = csv.writer(f)
    if is_new:
        writer.writerow(["timestamp", "friend_username", "success", "captcha",
                         "url_before", "url_after", "error"])
    return f, writer


def fill_field(page, selector: str, value: str, typing_delay_ms: int, label: str):
    """Wait for a field, click it, clear it, and type into it human-like."""
    try:
        page.wait_for_selector(selector, timeout=10000)
    except Exception as e:
        raise RuntimeError(f"Could not find field '{label}' using selector '{selector}': {e}")
    page.click(selector)
    # Clear any pre-filled value first
    page.fill(selector, "")
    page.locator(selector).press_sequentially(value, delay=typing_delay_ms)


def run_form_filler(config_path: Path, names: list):
    """Same logic as the original form_filler.run(), except the list of
    friend usernames is passed in directly (from the lost-score scan)
    instead of being read from a names.txt file."""
    cfg = load_config(config_path)

    if not names:
        print("[*] No lost-score names to submit for — nothing to do.")
        return

    sel = cfg["selectors"]
    results_path = Path(cfg.get("results_log", "results.csv"))
    if not results_path.is_absolute():
        results_path = config_path.parent / results_path
    log_file, log_writer = init_results_log(results_path)

    typing_delay_ms = cfg.get("typing_delay_ms", 60)
    d_before_typing = cfg.get("delay_before_typing", 1.2)
    d_between_fields = cfg.get("delay_between_fields", 0.5)
    d_after_submit = cfg.get("delay_after_submit", 1.5)
    # NOTE: still read from the "delay_between_customers" config key so
    # existing config.json files keep working without edits.
    d_between_friends = cfg.get("delay_between_customers", 2.5)
    captcha_solve_wait = cfg.get("captcha_solve_wait", 180)
    url_change_wait = cfg.get("url_change_wait", 15)
    resubmit_after_captcha = cfg.get("resubmit_after_captcha", True)

    print(f"[*] Submitting for {len(names)} friend(s): {names}")
    print(f"[*] Target form: {cfg['default_url']}")
    print(f"[*] Logging results to: {results_path}\n")

    # --- Strict CDP Mode: launch a stealthy Chrome via SeleniumBase CDP ---
    sb = sb_cdp.Chrome(
        cfg["default_url"],
        headless=cfg.get("headless", False),
        use_chromium=cfg.get("use_chromium", False),
        user_data_dir=cfg.get("user_data_dir", "my-profile")
    )
    endpoint_url = sb.get_endpoint_url()

    success_count = 0
    fail_count = 0

    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(endpoint_url)
            page = browser.contexts[0].pages[0]

            for i, friend_username in enumerate(names, start=1):
                print(f"[{i}/{len(names)}] Submitting for friend: {friend_username!r}")
                error_msg = ""
                url_before = ""
                url_after = ""
                captcha_note = ""
                success = False

                try:
                    # Fresh visit each time so the form starts empty
                    sb.goto(cfg["default_url"])
                    sb.sleep(d_before_typing)

                    url_before = page.url

                    fill_field(page, sel["name_field"], cfg["my_name"], typing_delay_ms, "name_field")
                    sb.sleep(d_between_fields)

                    fill_field(page, sel["email_field"], cfg["my_email"], typing_delay_ms, "email_field")
                    sb.sleep(d_between_fields)

                    fill_field(page, sel["phone_field"], cfg["my_phone"], typing_delay_ms, "phone_field")
                    sb.sleep(d_between_fields)

                    fill_field(page, sel["customer_name_field"], friend_username, typing_delay_ms, "customer_name_field")
                    sb.sleep(d_between_fields)

                    page.wait_for_selector(sel["submit_button"], timeout=10000)
                    page.click(sel["submit_button"])
                    sb.sleep(d_after_submit)

                    # --- deal with a captcha if one shows up, then read result ---
                    url_after, captcha_note = handle_captcha_and_wait(
                        page, sb, url_before, sel["submit_button"],
                        captcha_solve_wait, url_change_wait,
                        resubmit_after_captcha,
                    )

                    success = url_after != url_before

                    if success:
                        success_count += 1
                        print(f"    -> SUCCESS (URL changed: {url_before} -> {url_after})")
                    else:
                        fail_count += 1
                        print(f"    -> FAILED (URL unchanged: {url_after})")

                except Exception as e:
                    fail_count += 1
                    error_msg = str(e)
                    print(f"    -> ERROR: {error_msg}")

                log_writer.writerow([
                    time.strftime("%Y-%m-%d %H:%M:%S"),
                    friend_username,
                    success,
                    captcha_note,
                    url_before,
                    url_after,
                    error_msg,
                ])
                log_file.flush()

                if i < len(names):
                    sb.sleep(d_between_friends)

    finally:
        log_file.close()
        try:
            sb.driver.quit()
        except Exception:
            pass

    print(f"\n[*] Done. {success_count} succeeded, {fail_count} failed out of {len(names)}.")
    print(f"[*] Full results saved to: {results_path}")


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Scan friend scores, then submit the forum form for anyone who lost their score."
    )
    parser.add_argument("--config", default="config.json", help="Path to config.json")
    args = parser.parse_args()
    config_path = Path(args.config).resolve()

    lost = get_lost_score_names()

    if not lost:
        print("\n[*] Nobody's score was found lost — nothing to submit.")
        return

    unmapped = [n for n in lost if n not in NAME_TO_USERNAME]
    if unmapped:
        print(f"\n[!] No username mapping for: {unmapped} — skipping them.")

    friend_usernames = [NAME_TO_USERNAME[n] for n in lost if n in NAME_TO_USERNAME]

    if not friend_usernames:
        print("\n[*] None of the lost-score names had a username mapping — nothing to submit.")
        return

    print(f"\n[*] Proceeding to submit the form for: {friend_usernames}\n")
    run_form_filler(config_path, friend_usernames)


if __name__ == "__main__":
    main()