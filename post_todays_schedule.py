import os
import sys
import json
import time
from datetime import datetime, timezone, timedelta
from playwright.sync_api import sync_playwright

# Ensure UTF-8 console output
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CALENDAR_JSON = os.path.join(BASE_DIR, "marketing_calendar_30_days.json")
STATE_FILE = os.path.join(BASE_DIR, "state.json")

# Map calendar board names to exact boards existing in the user's Pinterest account
BOARD_MAPPING = {
    "Printable Coloring & Art Books": "Paint by number",
    "Cold Case & Murder Mystery Files": "True Crime & Cold Case Files",
    "Interactive Mystery Books": "Interactive Mystery Books",
    "Gifts for Mystery Lovers & True Crime Fans": "Gifts for Mystery Lovers & True Crime Fans",
    "Dark Academia & Detective Aesthetics": "Dark Academia & Detective Aesthetics",
    "Escape Room in a Book": "Escape Room in a Book"
}

def load_calendar():
    with open(CALENDAR_JSON, "r", encoding="utf-8") as f:
        return json.load(f)

def save_calendar(data):
    with open(CALENDAR_JSON, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def post_due_pins():
    # BST timezone (UTC+6)
    BST = timezone(timedelta(hours=6))
    now_bst = datetime.now(BST)
    today_str = now_bst.strftime("%Y-%m-%d")
    now_time_str = now_bst.strftime("%H:%M")
    
    calendar = load_calendar()
    
    # Check for overdue or currently due pins
    due_pins = []
    for item in calendar:
        if item.get("posted", False):
            continue
        s_date = item.get("scheduled_date", "")
        s_time = item.get("scheduled_time_24h", "23:59")
        if s_date < today_str:
            due_pins.append(item)
        elif s_date == today_str and s_time <= now_time_str:
            due_pins.append(item)

    # Fallback: if runner triggered manually or slightly before scheduled minute, pick next pending for today
    if not due_pins:
        pending_today = [x for x in calendar if not x.get("posted") and x.get("scheduled_date") == today_str]
        if pending_today:
            print(f"📌 Runner triggered at {now_time_str} BST: selecting next upcoming pin for today ({today_str})...")
            due_pins = [pending_today[0]]

    if not due_pins:
        print(f"✅ No pending scheduled pins due right now (Current time: {today_str} {now_time_str} BST)!")
        return

    # Cap to max 3 pins per execution to ensure smooth runs
    due_pins = due_pins[:3]
    print(f"🚀 Found {len(due_pins)} scheduled pin(s) ready to publish for {today_str} ({now_time_str} BST)...")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        if os.path.exists(STATE_FILE):
            context = browser.new_context(
                storage_state=STATE_FILE,
                viewport={"width": 1366, "height": 900}
            )
        else:
            profile_dir = os.path.join(BASE_DIR, ".browser_session")
            context = p.chromium.launch_persistent_context(
                user_data_dir=profile_dir,
                headless=True,
                viewport={"width": 1366, "height": 900}
            )
        page = context.pages[0] if context.pages else context.new_page()

        for idx, item in enumerate(due_pins, 1):
            pin_title = item["pin_title"]
            pin_desc = item["pin_description"]
            pin_link = item["etsy_url"]
            target_board = BOARD_MAPPING.get(item["board_name"], item["board_name"])
            img_path = item.get("image_path", "")
            tags_list = [t.strip() for t in item.get("tagged_topics", "").split(",") if t.strip()][:4]

            # Cross-platform path resolution (Windows & Linux / GitHub Actions)
            filename = item.get("image_filename") or img_path.replace("\\", "/").split("/")[-1]
            repo_img_path = os.path.join(BASE_DIR, "output_pins", "variations", filename)
            if os.path.exists(repo_img_path):
                img_path = repo_img_path
            elif os.path.exists(img_path):
                pass
            else:
                print(f"⚠️ Image missing: {img_path} (also checked {repo_img_path}). Skipping.")
                continue

            print(f"\n[{idx}/{len(due_pins)}] 📌 Publishing: {pin_title[:55]}...")
            print(f"    Scheduled for: {item['scheduled_date']} {item['scheduled_time']} | Board: {target_board}")

            page.goto("https://www.pinterest.com/pin-creation-tool/", wait_until="domcontentloaded", timeout=30000)
            time.sleep(4)
            page.keyboard.press("Escape")
            time.sleep(1)

            if "/login" in page.url:
                print("❌ Error: Not logged in. Please check .browser_session.")
                break

            # Count existing drafts before upload
            before_count = page.locator('[data-test-id^="pinDraft-"]').count()

            # 1. Upload image
            upload_input = page.wait_for_selector('input[data-test-id="storyboard-upload-input"], input[type="file"]', timeout=20000)
            upload_input.set_input_files(img_path)
            print("  🖼️ Image uploaded. Waiting for new draft...")

            # Wait for draft count to increase and select the newly added draft (last child)
            for _ in range(15):
                time.sleep(1)
                if page.locator('[data-test-id^="pinDraft-"]').count() > before_count:
                    break
            time.sleep(2)

            new_draft = page.locator('[data-test-id^="pinDraft-"]').last
            if new_draft.count() > 0:
                new_draft.click()
                time.sleep(2)

            # 2. Title
            title_el = page.wait_for_selector('#storyboard-selector-title, input[placeholder*="Tell everyone" i]', timeout=15000)
            title_el.click()
            title_el.fill(pin_title[:100])
            print("  ✍️ Title filled.")

            # 3. Description (Draft.js editor)
            desc_el = page.wait_for_selector('div.public-DraftEditor-content[aria-label="Describe your Pin"]', timeout=15000)
            desc_el.click()
            time.sleep(0.5)
            page.keyboard.insert_text(pin_desc[:500])
            print("  ✍️ Description filled.")

            # 4. Link
            link_el = page.wait_for_selector('#WebsiteField, input[placeholder="Add a link"]', timeout=15000)
            link_el.click()
            link_el.fill(pin_link)
            print("  🔗 Etsy Link filled.")

            # 5. Select Board using exact data-test-id
            try:
                board_btn = page.locator('[data-test-id="board-dropdown-select-button"]').first
                board_btn.click()
                time.sleep(2)
                board_row = page.locator(f'[data-test-id="board-row-{target_board}"]').first
                if board_row.count() > 0:
                    board_row.click()
                    print(f"  📁 Selected Board: {target_board}")
                else:
                    print(f"  ⚠️ Board '{target_board}' not found cleanly.")
                time.sleep(1.5)
            except Exception as e:
                print(f"  ⚠️ Board selection note: {e}")

            # 6. Add Tagged Topics
            try:
                tag_input = page.locator('#combobox-storyboard-interest-tags').first
                if tag_input.count() > 0:
                    added_tags = []
                    for tag in tags_list:
                        tag_input.click()
                        tag_input.fill("")
                        tag_input.type(tag, delay=40)
                        time.sleep(1.5)
                        option = page.locator('[role="option"], [data-test-id*="interest-tag-option"]').first
                        if option.count() > 0:
                            opt_text = option.inner_text(timeout=3000)
                            if "No results" not in opt_text:
                                option.click()
                                added_tags.append(tag)
                                time.sleep(0.5)
                    page.keyboard.press("Escape")
                    print(f"  🏷️ Added {len(added_tags)} Tagged Topics: {added_tags}")
            except Exception as e:
                print(f"  ⚠️ Tagging note: {e}")

            time.sleep(2)
            page.evaluate("window.scrollTo(0, 0)")
            time.sleep(1)

            publish_btn = page.locator('[data-test-id="storyboard-creation-nav-done"] button, button:has-text("Publish")').first
            if publish_btn.count() > 0 and publish_btn.is_enabled():
                publish_btn.click()
                print("  🚀 Clicked Publish! Waiting for confirmation...")
                time.sleep(7)
                page.keyboard.press("Escape")
                
                item["posted"] = True
                item["posted_at"] = datetime.now().isoformat()
                save_calendar(calendar)
                print("  ✅ Pin PUBLISHED successfully and saved to calendar!")
            else:
                print("  ❌ Publish button not clickable/enabled.")

            time.sleep(4)

        context.close()
    print("\n🎉 Done processing today's scheduled pins!")

if __name__ == "__main__":
    post_due_pins()
