#!/usr/bin/env python3
import os, time, traceback
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By

OUT_DIR = '/home/pi/MaiconErick/Umbra/tmp/sub_search'
SCR_DIR = os.path.join(OUT_DIR, 'screenshots')
PAGE_DIR = os.path.join(OUT_DIR, 'pages')
FOUND = os.path.join(OUT_DIR, 'found_links.txt')
PROFILE = os.path.join(OUT_DIR, 'vivaldi_profile')

os.makedirs(SCR_DIR, exist_ok=True)
os.makedirs(PAGE_DIR, exist_ok=True)

# pick browser binary: prefer vivaldi (policy), fall back to system chromium
if os.path.exists('/usr/bin/vivaldi'):
    browser_bin = '/usr/bin/vivaldi'
elif os.path.exists('/usr/bin/chromium-browser'):
    browser_bin = '/usr/bin/chromium-browser'
elif os.path.exists('/snap/bin/chromium'):
    browser_bin = '/snap/bin/chromium'
else:
    raise SystemExit('No browser binary found at /usr/bin/vivaldi or /usr/bin/chromium-browser or /snap/bin/chromium')

options = webdriver.ChromeOptions()
options.binary_location = browser_bin
options.add_argument(f'--user-data-dir={PROFILE}')
options.add_argument('--no-sandbox')
options.add_argument('--disable-dev-shm-usage')
options.add_argument('--disable-extensions')
# options.add_argument('--headless=new')  # don't use headless, we want real browser for JS challenges

# Use system chromedriver (installed via apt/snap)
SERVICE_PATH = '/usr/bin/chromedriver'
service = Service(SERVICE_PATH)

try:
    driver = webdriver.Chrome(service=service, options=options)
except Exception:
    traceback.print_exc()
    raise

driver.set_page_load_timeout(60)

urls = [
    'https://subscene.com/subtitles/searchbytitle?query=Solo%20Leveling%20S02E02',
    'https://www.opensubtitles.org/en/search2/sublanguageid-jpn/moviename-Solo%20Leveling%20S02E02',
    'https://nyaa.si/?f=0&c=1_0&q=Solo+Leveling+S02E02+subtitle',
    'https://kitsunekko.net/search.php?search=Solo+Leveling',
    'https://animetosho.org/find?q=Solo+Leveling+S02E02',
    'https://duckduckgo.com/?q=Solo+Leveling+S02E02+Japanese+subtitles'
]

with open(FOUND, 'w') as fh:
    fh.write('Found links during automated search\n')

for url in urls:
    try:
        print('Opening', url)
        driver.get(url)
        time.sleep(6)
        safe = url.replace('://', '_').replace('/', '_')[:200]
        ss_path = os.path.join(SCR_DIR, f'{safe}.png')
        page_path = os.path.join(PAGE_DIR, f'{safe}.html')
        try:
            driver.save_screenshot(ss_path)
        except Exception:
            pass
        with open(page_path, 'w', encoding='utf-8') as f:
            f.write(driver.page_source)
        # try to find candidate links
        anchors = driver.find_elements(By.XPATH, "//a[contains(@href,'.srt') or contains(@href,'.zip') or contains(@href,'.ass') or contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'download') or contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '字幕')]")
        if anchors:
            with open(FOUND, 'a', encoding='utf-8') as fh:
                fh.write(f'--- {url} ---\n')
                for a in anchors:
                    href = a.get_attribute('href')
                    text = a.text
                    fh.write(f'{text} -> {href}\n')
        else:
            with open(FOUND, 'a', encoding='utf-8') as fh:
                fh.write(f'--- {url} ---\nNo obvious subtitle links found on page.\n')
    except Exception as e:
        with open(FOUND, 'a') as fh:
            fh.write(f'ERROR opening {url}: {e}\n')
        traceback.print_exc()

print('Done. Results in', OUT_DIR)
print('Screenshots:', SCR_DIR)
print('Pages:', PAGE_DIR)
print('Found links log:', FOUND)

driver.quit()
