"""
Naukri Auto-Apply Bot - Microsoft Edge Version (Updated for Naukri.com 2026)
================================================================================
Automates job applications on Naukri.com using Selenium with Edge browser.
Opens each job keyword search in a separate tab for faster collection.

Usage:
    1. Copy .env.example to .env and fill in your details
    2. pip install -r requirements.txt
    3. python Naukri-Edge.py

Requirements:
    - Edge browser installed
    - Dependencies from requirements.txt
"""

import os
import time
import csv
import difflib
import re
import logging
import pandas as pd
from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.edge.service import Service as EdgeService
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support.ui import Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException, WebDriverException
from bs4 import BeautifulSoup

# Try to use webdriver-manager for automatic driver management
try:
    from webdriver_manager.microsoft import EdgeChromiumDriverManager
    WEBDRIVER_MANAGER_AVAILABLE = True
except ImportError:
    WEBDRIVER_MANAGER_AVAILABLE = False

# ------------------------------------------------------------
# Configuration
# ------------------------------------------------------------
load_dotenv()

# --- Naukri Credentials ---
NAUKRI_EMAIL = os.getenv('NAUKRI_EMAIL', '')
NAUKRI_PASSWORD = os.getenv('NAUKRI_PASSWORD', '')

# --- Personal Details ---
FIRSTNAME = os.getenv('FIRSTNAME', '')
LASTNAME = os.getenv('LASTNAME', '')

# --- Job Search ---
KEYWORDS = [kw.strip() for kw in os.getenv('KEYWORDS', '').split(',') if kw.strip()]
LOCATION = os.getenv('LOCATION', '').strip()
ROLE_FILTERS = [item.strip().lower() for item in os.getenv('ROLE_FILTERS', '').split(',') if item.strip()]
EXPERIENCE_FILTERS = [item.strip().lower() for item in os.getenv('EXPERIENCE_FILTERS', '').split(',') if item.strip()]
EXCLUDE_FILTERS = [item.strip().lower() for item in os.getenv('EXCLUDE_FILTERS', '').split(',') if item.strip()]
REQUIRE_DIRECT_APPLY = os.getenv('REQUIRE_DIRECT_APPLY', 'true').strip().lower() not in ('0', 'false', 'no')
APPLY_DETECTION_TIMEOUT = max(0.5, float(os.getenv('APPLY_DETECTION_TIMEOUT', '2')))

# --- Limits ---
MAX_APPLICATIONS = int(os.getenv('MAX_APPLICATIONS', '50'))
PAGES_PER_KEYWORD = int(os.getenv('PAGES_PER_KEYWORD', '2'))
ANSWERS_CSV = os.getenv('ANSWERS_CSV', 'application_answers.csv')
RESUME_FILE = os.getenv('RESUME_FILE', 'resume.txt')
POST_TIMEOUT = max(30, int(os.getenv('POST_TIMEOUT', os.getenv('JOB_TIMEOUT', '90'))))
MANUAL_ANSWER_TIMEOUT = max(
    10, int(os.getenv('MANUAL_ANSWER_TIMEOUT', os.getenv('QUESTION_TIMEOUT', '30')))
)
QUESTION_POLL_INTERVAL = max(0.25, float(os.getenv('QUESTION_POLL_INTERVAL', '0.5')))

# --- Edge Driver Path (optional, only if NOT using webdriver-manager) ---
EDGE_DRIVER_PATH = os.getenv('EDGE_DRIVER_PATH', '')

# ------------------------------------------------------------
# Logging Setup
# ------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)


def load_answer_data():
    """Load saved answers and resume text used for application questions."""
    answers = {}
    if os.path.exists(ANSWERS_CSV):
        try:
            with open(ANSWERS_CSV, 'r', encoding='utf-8', newline='') as file:
                for row in csv.DictReader(file):
                    question = (row.get('question_text') or '').strip()
                    answer = (row.get('answer') or '').strip()
                    if question and answer:
                        answers[question] = answer
        except (OSError, csv.Error) as error:
            logger.warning(f"Could not read {ANSWERS_CSV}: {error}")

    resume = ''
    if os.path.exists(RESUME_FILE):
        try:
            with open(RESUME_FILE, 'r', encoding='utf-8') as file:
                resume = file.read()
        except OSError as error:
            logger.warning(f"Could not read {RESUME_FILE}: {error}")

    logger.info(f"Loaded {len(answers)} saved answers and {len(resume)} resume characters.")
    return answers, resume


def find_saved_answer(question, answers):
    """Return an exact or close saved answer for a question."""
    def normalized(value):
        value = value.lower().replace('lacs', 'lakhs').replace('lac', 'lakh')
        value = re.sub(r'[^a-z0-9]+', ' ', value)
        return set(value.split())

    if question in answers:
        return answers[question]

    question_tokens = normalized(question)
    best_key = None
    best_score = 0.0
    for answer_question in answers:
        answer_tokens = normalized(answer_question)
        if not answer_tokens:
            continue
        overlap = len(question_tokens & answer_tokens) / len(question_tokens | answer_tokens)
        sequence_score = difflib.SequenceMatcher(None, question.lower(), answer_question.lower()).ratio()
        score = max(overlap, sequence_score)
        if score > best_score:
            best_key, best_score = answer_question, score
    return answers[best_key] if best_key and best_score >= 0.45 else None


def infer_resume_answer(question, resume):
    """Answer only questions supported by configured profile data or resume text."""
    normalized = question.lower()
    if any(word in normalized for word in ('first name', 'given name')):
        return FIRSTNAME
    if 'last name' in normalized or 'surname' in normalized:
        return LASTNAME
    profile_values = {
        'email': NAUKRI_EMAIL,
        'phone': os.getenv('PHONE', ''),
        'mobile': os.getenv('PHONE', ''),
        'location': LOCATION,
    }
    for field, value in profile_values.items():
        if field in normalized and value:
            return value

    if resume:
        experience = re.search(r'(\d+(?:\.\d+)?)\+?\s*(?:years?|yrs?)\s*(?:of)?\s*experience', resume, re.I)
        if experience and ('experience' in normalized or 'years' in normalized):
            return experience.group(1)
        if 'skill' in normalized or 'technology' in normalized or 'tech stack' in normalized:
            skills = re.findall(r'(?im)^\s*(?:skills?|technologies?)\s*[:\-]\s*(.+)$', resume)
            if skills:
                return skills[0].strip()

    if re.search(r'how many years|years of experience|experience in', normalized):
        return '0'

    return None


def get_question_answer(question, options, answers, resume):
    """Resolve a question from saved answers, profile/resume facts, or return None."""
    answer = find_saved_answer(question, answers)
    normalized = question.lower()
    numeric_question = any(term in normalized for term in (
        'how many years', 'years of experience', 'current ctc', 'current salary',
        'salary per month', 'ctc/month',
    ))
    if answer and not (numeric_question and answer.strip().lower() in ('yes', 'no', 'fresher')):
        return answer
    return infer_resume_answer(question, resume)


def save_answer(question, answer, field_type):
    """Persist a confirmed answer for reuse on later applications."""
    exists = os.path.exists(ANSWERS_CSV) and os.path.getsize(ANSWERS_CSV) > 0
    try:
        if exists:
            with open(ANSWERS_CSV, 'r', encoding='utf-8', newline='') as existing_file:
                for row in csv.DictReader(existing_file):
                    if ((row.get('question_text') or '').strip().lower() == question.strip().lower()
                            and (row.get('answer') or '').strip() == answer.strip()):
                        return
        with open(ANSWERS_CSV, 'a', encoding='utf-8', newline='') as file:
            writer = csv.writer(file)
            if not exists:
                writer.writerow(['question_text', 'answer', 'field_type', 'created_at'])
            writer.writerow([question, answer, field_type, time.strftime('%Y-%m-%dT%H:%M:%S')])
    except OSError as error:
        logger.warning(f"Could not save answer: {error}")


def validate_config():
    """Check that required configuration values are set."""
    errors = []
    if not NAUKRI_EMAIL:
        errors.append("NAUKRI_EMAIL is not set in .env")
    if not NAUKRI_PASSWORD:
        errors.append("NAUKRI_PASSWORD is not set in .env")
    if not KEYWORDS:
        errors.append("KEYWORDS is not set in .env (comma-separated job roles)")
    if not FIRSTNAME:
        errors.append("FIRSTNAME is not set in .env")
    if not LASTNAME:
        errors.append("LASTNAME is not set in .env")
    if errors:
        for e in errors:
            logger.error(e)
        logger.error("Please copy .env.example to .env and fill in your details.")
        return False
    return True


def create_edge_driver():
    """Create and return an Edge WebDriver instance."""
    options = webdriver.EdgeOptions()
    options.add_argument("--start-maximized")
    options.add_argument("--disable-notifications")
    # Uncomment the line below to run in headless mode (no visible browser)
    # options.add_argument("--headless=new")

    if WEBDRIVER_MANAGER_AVAILABLE:
        logger.info("Using webdriver-manager to auto-download EdgeDriver...")
        service = EdgeService(EdgeChromiumDriverManager().install())
    elif EDGE_DRIVER_PATH:
        logger.info(f"Using EdgeDriver at: {EDGE_DRIVER_PATH}")
        service = EdgeService(executable_path=EDGE_DRIVER_PATH)
    else:
        logger.info("No driver path specified; assuming EdgeDriver is in PATH...")
        service = EdgeService()

    driver = webdriver.Edge(service=service, options=options)
    return driver


def login_naukri(driver):
    """Log in to Naukri.com."""
    logger.info("Logging in to Naukri.com...")
    driver.get('https://login.naukri.com/')
    time.sleep(3)

    # Wait for username field
    WebDriverWait(driver, 10).until(
        EC.presence_of_element_located((By.ID, 'usernameField'))
    )

    uname = driver.find_element(By.ID, 'usernameField')
    uname.send_keys(NAUKRI_EMAIL)

    passwd = driver.find_element(By.ID, 'passwordField')
    passwd.send_keys(NAUKRI_PASSWORD)
    passwd.send_keys(Keys.ENTER)

    # Wait for login to complete
    time.sleep(8)
    logger.info("Login completed.")


def build_search_urls():
    """
    Build all search URLs for every keyword and page combination.
    Naukri.com updated URL format: /{keyword}-jobs-in-{location}
    Returns a list of (keyword, url) tuples.
    """
    urls = []
    for keyword in KEYWORDS:
        keyword_slug = keyword.lower().replace(' ', '-')
        for page_num in range(1, PAGES_PER_KEYWORD + 1):
            if not LOCATION:
                if page_num == 1:
                    url = f"https://www.naukri.com/{keyword_slug}-jobs"
                else:
                    url = f"https://www.naukri.com/{keyword_slug}-jobs-{page_num}"
            else:
                location_slug = LOCATION.lower().replace(' ', '-')
                if page_num == 1:
                    url = f"https://www.naukri.com/{keyword_slug}-jobs-in-{location_slug}"
                else:
                    url = f"https://www.naukri.com/{keyword_slug}-jobs-in-{location_slug}-{page_num}"
            urls.append((keyword, url))
    return urls


def open_tabs_parallel(driver, search_urls):
    """
    Open all search URLs in parallel browser tabs.
    The first URL opens in the current tab; the rest open in new tabs.
    Returns the original window handle.
    """
    original_window = driver.current_window_handle

    if not search_urls:
        return original_window

    # Open first URL in current tab
    keyword, first_url = search_urls[0]
    logger.info(f"[Tab: Main] Opening: {first_url}")
    driver.get(first_url)
    time.sleep(3)

    # Open remaining URLs in new tabs
    for keyword, url in search_urls[1:]:
        logger.info(f"[Tab: New] Opening: {url}")
        driver.switch_to.new_window('tab')
        driver.get(url)
        time.sleep(2)

    # Switch back to the original tab
    driver.switch_to.window(original_window)
    return original_window


def collect_job_links_from_tab(driver, window_handle):
    """
    Switch to a specific tab, scrape job links from the page.
    Uses updated Naukri.com selectors (as of 2026 redesign).

    Current HTML structure for job cards:
        <div class="srp-jobtuple-wrapper" data-job-id="...">
          <div class="cust-job-tuple layout-wrapper lay-2 sjw__tuple">
            <div class="row1">
              <h2><a class="title " href="https://...">Job Title</a></h2>
            </div>
          </div>
        </div>
    """
    links = []
    try:
        driver.switch_to.window(window_handle)
        time.sleep(1)  # Allow the result cards to mount before parsing

        soup = BeautifulSoup(driver.page_source, 'html5lib')

        # New selector: find all job tuple wrappers
        job_wrappers = soup.find_all('div', class_='srp-jobtuple-wrapper')
        raw_count = len(job_wrappers)
        logger.info(f"[Tab: {driver.title[:50]}] Found {raw_count} job cards (srp-jobtuple-wrapper)")

        if not job_wrappers:
            # Fallback: try the older cust-job-tuple class
            job_wrappers = soup.find_all('div', class_='cust-job-tuple')
            raw_count = len(job_wrappers)
            logger.info(f"[Tab: {driver.title[:50]}] Found {raw_count} job cards (cust-job-tuple fallback)")

        for job_wrapper in job_wrappers:
            # Find the title link - new selector is a.title (class="title ")
            title_link = job_wrapper.find('a', class_='title')
            if title_link and title_link.get('href'):
                href = title_link.get('href')
                # Ensure full URL
                if href.startswith('/'):
                    href = 'https://www.naukri.com' + href
                job_text = job_wrapper.get_text(' ', strip=True).lower()
                if ROLE_FILTERS and not any(role in job_text for role in ROLE_FILTERS):
                    continue
                if EXPERIENCE_FILTERS and not any(experience in job_text for experience in EXPERIENCE_FILTERS):
                    continue
                if EXCLUDE_FILTERS and any(excluded in job_text for excluded in EXCLUDE_FILTERS):
                    continue
                links.append(href)

    except WebDriverException as e:
        logger.warning(f"[Tab] Error reading tab: {e}")

    return links


def collect_all_jobs_parallel(driver, search_urls):
    """
    Open all keyword search pages in parallel tabs and collect job links from each.
    Returns a deduplicated list of job URLs.
    """
    logger.info("=" * 50)
    logger.info(f"Opening {len(search_urls)} search pages in parallel tabs...")
    logger.info("=" * 50)

    # Open all search URLs in parallel tabs
    original_window = open_tabs_parallel(driver, search_urls)

    # Collect job links from each tab
    all_links = []
    window_handles = driver.window_handles
    logger.info(f"Collecting job links from {len(window_handles)} tabs...")

    for handle in window_handles:
        tab_links = collect_job_links_from_tab(driver, handle)
        all_links.extend(tab_links)

    # Close all extra tabs and return to main
    logger.info("Closing search tabs...")
    for handle in window_handles:
        if handle != original_window:
            try:
                driver.switch_to.window(handle)
                driver.close()
            except WebDriverException:
                pass
    driver.switch_to.window(original_window)

    # Remove duplicates while preserving order
    seen = set()
    unique_links = []
    for link in all_links:
        if link not in seen:
            seen.add(link)
            unique_links.append(link)

    duplicate_count = len(all_links) - len(unique_links)
    logger.info(
        f"Collected {len(all_links)} eligible links from {len(window_handles)} tabs; "
        f"removed {duplicate_count} duplicates; {len(unique_links)} unique jobs remain."
    )
    return unique_links


def click_apply_button(driver, link):
    """
    Try to click Naukri's direct Apply button on a job detail page.

    Company-site applications are intentionally skipped because they redirect
    away from Naukri and cannot be completed by this bot.

    Returns True if applied successfully, False otherwise.
    """
    # Do not sleep for a fixed page-load delay. External applications can be
    # rejected as soon as their card/button is mounted.
    time.sleep(0.2)

    current_host = driver.current_url.split('/')[2].lower() if '://' in driver.current_url else ''
    if REQUIRE_DIRECT_APPLY and not current_host.endswith('naukri.com'):
        logger.info(f"  Skipping non-Naukri page: {driver.current_url}")
        return False

    if page_has_company_site_apply(driver):
        logger.info("  Skipping 'Apply on company site' application.")
        return False

    # Match only the exact direct-application label. Do not use contains()
    # because it also matches "Apply on company site".
    apply_selectors = [
        (By.XPATH, "//button[normalize-space(.)='Apply']"),
        (By.XPATH, "//*[@role='button' and normalize-space(.)='Apply']"),
        (By.CSS_SELECTOR, "button[class*='apply'], [class*='apply-button']"),
    ]

    for by, selector in apply_selectors:
        try:
            apply_btn = WebDriverWait(driver, APPLY_DETECTION_TIMEOUT).until(
                EC.element_to_be_clickable((by, selector))
            )
            if is_company_site_apply_button(apply_btn):
                logger.info("  Skipping 'Apply on company site' application.")
                return False
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", apply_btn)
            try:
                apply_btn.click()
            except WebDriverException:
                driver.execute_script("arguments[0].click();", apply_btn)
            time.sleep(1)

            # Direct applications stay on Naukri. Treat another destination
            # as a company-site application and do not count it.
            current_host = driver.current_url.split('/')[2].lower()
            if not current_host.endswith('naukri.com'):
                logger.info(f"  Skipping external company site: {driver.current_url}")
                return False
            return True
        except (TimeoutException, NoSuchElementException, WebDriverException):
            continue

    return False


def is_company_site_apply_button(element):
    """Return True for buttons that start an external/company-site application."""
    text = ' '.join((
        element.text or '',
        element.get_attribute('aria-label') or '',
        element.get_attribute('title') or '',
        element.get_attribute('href') or '',
    )).strip().lower()
    return any(marker in text for marker in (
        'apply on company site',
        'company site',
        'external application',
        'external site',
    ))


def page_has_company_site_apply(driver):
    """Detect an external-application control without waiting through selectors."""
    try:
        controls = driver.find_elements(
            By.CSS_SELECTOR,
            "button, a, [role='button'], [class*='apply'], [class*='Apply']",
        )
        return any(
            element.is_displayed() and is_company_site_apply_button(element)
            for element in controls
        )
    except WebDriverException:
        return False


def visible_elements(driver, selector):
    """Return displayed elements matching selector, ignoring stale elements."""
    try:
        return [element for element in driver.find_elements(By.CSS_SELECTOR, selector)
                if element.is_displayed() and element.is_enabled()]
    except WebDriverException:
        return []


def question_text_for(element):
    """Get the most useful nearby label/question text for a form control."""
    for attribute in ('aria-label', 'placeholder'):
        value = (element.get_attribute(attribute) or '').strip()
        if value:
            return value
    try:
        label = element.find_element(By.XPATH, "./ancestor::label[1]").text.strip()
        if label:
            return label
    except NoSuchElementException:
        pass
    try:
        parent = element.find_element(By.XPATH, "./ancestor::*[self::div or self::li or self::fieldset][1]")
        text = parent.text.strip()
        if text:
            return text[:500]
    except NoSuchElementException:
        pass
    return 'Unlabelled application question'


def select_option(driver, select_element, answer):
    """Select an option using visible text or value."""
    select = Select(select_element)
    for option in select.options:
        if (option.text.strip().lower() == answer.strip().lower() or
                (option.get_attribute('value') or '').strip().lower() == answer.strip().lower()):
            select.select_by_visible_text(option.text)
            return True
    return False


def form_scope(driver):
    """Prefer the application drawer so unrelated job-page fields are untouched."""
    selectors = (
        "[id*='ChatbotContainer'], .chatbot_Drawer, [role='dialog'], "
        "[id^='chatList_'], ul[id*='chatList_'], .chatbot_List"
    )
    candidates = visible_elements(driver, selectors)
    control_selector = (
        "input:not([type='hidden']):not([type='submit']):not([type='button']), "
        "textarea, select, [contenteditable='true'], [role='textbox']"
    )
    best_candidate = None
    best_control_count = 0
    for element in candidates:
        if (element.get_attribute('id') or '').startswith('chatList_'):
            try:
                drawer = element.find_element(
                    By.XPATH,
                    "./ancestor::*[@role='dialog' or contains(@class, 'Drawer') or contains(@class, 'drawer')][1]",
                )
                if drawer.is_displayed():
                    element = drawer
            except (NoSuchElementException, WebDriverException):
                pass
        try:
            control_count = len(element.find_elements(By.CSS_SELECTOR, control_selector))
        except WebDriverException:
            control_count = 0
        if control_count > best_control_count:
            best_candidate = element
            best_control_count = control_count
    if best_candidate is not None:
        return best_candidate
    return candidates[0] if candidates else driver


def scope_elements(scope, selector):
    try:
        return [element for element in scope.find_elements(By.CSS_SELECTOR, selector)
                if element.is_displayed() and element.is_enabled()]
    except WebDriverException:
        return []


def questionnaire_buttons(driver, scope):
    """Find visible questionnaire controls, preferring controls inside the drawer."""
    selector = (
        "button:not([class*='save-job']):not([id*='save-job']), "
        "input[type='submit'], input[type='button'], [role='button'], "
        "[class*='sendMsg'], [class*='save'], div.sendMsg"
    )
    scoped_buttons = scope_elements(scope, selector)
    for label in ('Save', 'Send', 'Next', 'Continue', 'Submit', 'Apply'):
        try:
            scoped_buttons.extend(scope_elements(scope, f"div[aria-label='{label}']"))
            scoped_buttons.extend(scope_elements(scope, f"div[title='{label}']"))
        except WebDriverException:
            pass
    try:
        scoped_buttons.extend(
            element for element in scope.find_elements(
                By.XPATH,
                ".//*[normalize-space(.)='Save' or normalize-space(.)='Send' or "
                "normalize-space(.)='Next' or normalize-space(.)='Continue' or "
                "normalize-space(.)='Submit' or normalize-space(.)='Apply']",
            ) if element.is_displayed() and element.is_enabled()
        )
    except WebDriverException:
        pass
    if scope is not driver and scoped_buttons:
        return scoped_buttons
    buttons = list(reversed(scoped_buttons if scope is driver else visible_elements(driver, selector)))
    unique_buttons = []
    seen_ids = set()
    for button in buttons:
        try:
            element_id = button.id
            if element_id not in seen_ids:
                seen_ids.add(element_id)
                unique_buttons.append(button)
        except WebDriverException:
            continue
    return unique_buttons


def action_label(element):
    """Normalize button labels such as `Next >` and `Save`."""
    return re.sub(r'[^a-z ]', '', control_label(element).lower()).strip()


def is_job_save_button(element):
    """Exclude the job-card Save button from questionnaire actions."""
    marker = ' '.join((
        element.get_attribute('id') or '',
        element.get_attribute('class') or '',
        element.get_attribute('data-testid') or '',
    )).lower()
    return (
        'styles_save-job-button' in marker
        or marker.strip() in {'save-job', 'savejob'}
    )


def is_chatbot_element(element):
    """Return whether an element belongs to Naukri's application chatbot."""
    try:
        element.find_element(
            By.XPATH,
            "ancestor-or-self::*[contains(@id, 'ChatbotContainer') or "
            "contains(@class, 'chatbot_') or contains(@class, 'chatBot')][1]",
        )
        return True
    except (NoSuchElementException, WebDriverException):
        return False


def latest_question_text(driver, scope):
    """Read the current chatbot question before falling back to control labels."""
    selectors = (
        ".chatbot_ListItem.botItem .msg, .chatbot_ListItem .botMsg .msg, "
        "[class*='botMsg'], #chatList_ li.botItem, ul[id*='chatList_'] li.botItem"
    )
    messages = scope_elements(scope, selectors)
    for element in reversed(messages):
        text = element.text.strip()
        if text and not re.match(r'^hi\s+.*thank you for showing interest', text, re.I):
            return text
    if scope is not driver:
        messages = visible_elements(driver, selectors)
        for element in reversed(messages):
            text = element.text.strip()
            if text and not re.match(r'^hi\s+.*thank you for showing interest', text, re.I):
                return text
    return None


def radio_group_key(radio):
    """Group radios even when Naukri omits the input name attribute."""
    name = radio.get_attribute('name')
    if name:
        return f"name:{name}"
    try:
        container = radio.find_element(By.XPATH, "./ancestor::fieldset[1]")
        return f"fieldset:{container.get_attribute('outerHTML')[:300]}"
    except NoSuchElementException:
        try:
            container = radio.find_element(By.XPATH, "./parent::*")
            return f"parent:{container.get_attribute('outerHTML')[:300]}"
        except NoSuchElementException:
            return radio.get_attribute('id') or 'radio-group'


def answer_visible_question(driver, scope, answers, resume):
    """Answer one visible questionnaire control and return whether work was done."""
    radio_groups = {}
    for radio in scope_elements(scope, "input[type='radio'], .ssrc__radio"):
        radio_groups.setdefault(radio_group_key(radio), []).append(radio)
    for radios in radio_groups.values():
        if any(radio.is_selected() for radio in radios):
            continue
        question = latest_question_text(driver, scope) or question_text_for(radios[0])
        options = []
        for radio in radios:
            label = ''
            radio_id = radio.get_attribute('id')
            if radio_id:
                labels = scope.find_elements(By.CSS_SELECTOR, f"label[for='{radio_id}']")
                label = labels[0].text.strip() if labels else ''
            options.append(label or radio.get_attribute('value') or '')
        answer = get_question_answer(question, options, answers, resume)
        if not answer:
            logger.warning(f"  Missing answer for radio question: {question}")
            return False
        chosen = next((radio for radio, option in zip(radios, options)
                       if option.lower() == answer.lower() or (radio.get_attribute('value') or '').lower() == answer.lower()), None)
        if not chosen:
            logger.warning(f"  Resume answer '{answer}' is not an available option for: {question}")
            return False
        driver.execute_script("arguments[0].click();", chosen)
        save_answer(question, answer, 'radio')
        return True

    for select_element in scope_elements(scope, 'select'):
        selected = Select(select_element).first_selected_option
        if selected and selected.get_attribute('value'):
            continue
        question = latest_question_text(driver, scope) or question_text_for(select_element)
        answer = get_question_answer(question, [], answers, resume)
        if not answer or not select_option(driver, select_element, answer):
            logger.warning(f"  Missing or invalid answer for select question: {question}")
            return False
        save_answer(question, answer, 'select')
        return True

    checkboxes = scope_elements(scope, "input[type='checkbox']")
    if checkboxes:
        question = latest_question_text(driver, scope) or question_text_for(checkboxes[0])
        answer = get_question_answer(question, [], answers, resume)
        if not answer:
            logger.warning(f"  Missing answer for checkbox question: {question}")
            return False
        requested = {item.strip().lower() for item in answer.split(',')}
        numeric_answer = None
        try:
            numeric_answer = float(answer.strip())
        except ValueError:
            pass
        selected_numeric_option = False
        for checkbox in checkboxes:
            checkbox_id = checkbox.get_attribute('id')
            labels = scope.find_elements(By.CSS_SELECTOR, f"label[for='{checkbox_id}']") if checkbox_id else []
            option = (labels[0].text.strip() if labels else checkbox.get_attribute('value') or '')
            normalized_option = option.lower()
            should_be_checked = (
                normalized_option in requested
                or answer.strip().lower() in ('yes', 'true', 'on', 'checked')
            )
            if numeric_answer is not None:
                bounds = re.findall(r'\d+(?:\.\d+)?', normalized_option)
                if bounds:
                    lower_bound = float(bounds[0])
                    upper_bound = float(bounds[1]) if len(bounds) > 1 else lower_bound
                    if '>' in normalized_option:
                        should_be_checked = numeric_answer > lower_bound
                    else:
                        should_be_checked = lower_bound <= numeric_answer <= upper_bound
                    selected_numeric_option = selected_numeric_option or should_be_checked
            if checkbox.is_selected() != should_be_checked:
                driver.execute_script("arguments[0].click();", checkbox)
                if checkbox.is_selected() != should_be_checked and labels:
                    driver.execute_script("arguments[0].click();", labels[0])
                if checkbox.is_selected() != should_be_checked:
                    logger.warning(f"  Could not select checkbox option '{option}' for: {question}")
                    return False
        if numeric_answer is not None and not selected_numeric_option:
            logger.warning(f"  No checkbox range matched numeric answer '{answer}' for: {question}")
            return False
        save_answer(question, answer, 'checkbox')
        return True

    fields = scope_elements(
        scope,
        "input:not([type='hidden']):not([type='radio']):not([type='checkbox']):not([type='submit']), "
        "textarea, div[contenteditable='true'], div.textArea, [role='textbox']",
    )
    for field in fields:
        if field.get_attribute('readonly') or field.get_attribute('disabled'):
            continue
        existing_value = (
            field.get_attribute('value')
            or field.get_property('value')
            or field.get_attribute('innerText')
            or field.text
            or ''
        )
        if existing_value.strip():
            continue
        question = latest_question_text(driver, scope) or question_text_for(field)
        answer = get_question_answer(question, [], answers, resume)
        if not answer:
            logger.warning(f"  Missing answer for text question: {question}")
            return False
        if field.get_attribute('contenteditable') == 'true':
            driver.execute_script(
                "arguments[0].focus(); arguments[0].innerText = arguments[1]; "
                "arguments[0].dispatchEvent(new Event('input', {bubbles: true}));",
                field,
                answer,
            )
        else:
            field.clear()
            field.send_keys(answer)
        save_answer(question, answer, 'text')
        return True
    return False


def has_application_confirmation(driver):
    """Return True only when Naukri displays a completion confirmation."""
    page_text = driver.find_element(By.TAG_NAME, 'body').text.lower()
    markers = ('application submitted', 'successfully applied', 'applied successfully', 'your application has been sent')
    return any(marker in page_text for marker in markers)


def control_label(element):
    """Return the visible/action label from a button-like form control."""
    for attribute in ('value', 'aria-label', 'title', 'data-testid'):
        value = (element.get_attribute(attribute) or '').strip()
        if value:
            return value
    return re.sub(r'\s+', ' ', element.text or '').strip()


def question_for_scope(driver, scope):
    """Find the current question from chatbot text or the visible form control."""
    question = latest_question_text(driver, scope)
    if question:
        return question
    controls = scope_elements(
        scope,
        "input:not([type='hidden']):not([type='submit']):not([type='button']), "
        "select, textarea, div[contenteditable='true']",
    )
    return question_text_for(controls[0]) if controls else None


def complete_application_questions(driver, answers, resume, job_deadline=None):
    """Complete every visible application question; never treat partial work as success."""
    deadline = time.monotonic() + POST_TIMEOUT
    if job_deadline is not None:
        deadline = min(deadline, job_deadline)
    saw_questionnaire = False
    handled_control = False
    current_question = None
    question_started = time.monotonic()
    while time.monotonic() < deadline:
        scope = form_scope(driver)
        if scope is not driver:
            saw_questionnaire = True
        detected_question = question_for_scope(driver, scope)
        if detected_question and detected_question != current_question:
            current_question = detected_question
            question_started = time.monotonic()
        if current_question and time.monotonic() - question_started >= MANUAL_ANSWER_TIMEOUT:
            logger.warning(
                f"  Question timed out after {MANUAL_ANSWER_TIMEOUT}s: {current_question}"
            )
            return False
        if has_application_confirmation(driver):
            return True
        if answer_visible_question(driver, scope, answers, resume):
            handled_control = True
            time.sleep(QUESTION_POLL_INTERVAL)
            continue

        # Do not click Save/Next while a visible form control is still
        # unanswered. This prevents the drawer from looping on Save when
        # Naukri renders the input outside its message-list container.
        pending_controls = scope_elements(
            scope,
            "input:not([type='hidden']):not([type='radio']):not([type='checkbox']):not([type='submit']), "
            "textarea, select, div[contenteditable='true'], div.textArea, [role='textbox']",
        )
        if pending_controls:
            logger.info("  Waiting for the questionnaire input to become available.")
            time.sleep(QUESTION_POLL_INTERVAL)
            continue

        buttons = questionnaire_buttons(driver, scope)
        action_labels = {
            'next', 'continue', 'save', 'send', 'submit', 'submit and apply', 'apply',
        }
        button = next((
            item for item in buttons
            if is_chatbot_element(item)
            and not is_job_save_button(item)
            and action_label(item) in action_labels
        ), None)
        if button and (handled_control or not current_question):
            label = control_label(button)
            logger.info(f"  Clicking questionnaire action: {label}")
            try:
                button.click()
            except WebDriverException:
                driver.execute_script("arguments[0].click();", button)
            time.sleep(QUESTION_POLL_INTERVAL)
            continue

        labels = [control_label(item) for item in buttons if control_label(item)]
        if labels:
            logger.warning(f"  No recognized questionnaire action. Visible controls: {labels}")

        if saw_questionnaire:
            if not visible_elements(
                driver,
                "[id*='ChatbotContainer'], .chatbot_Drawer, [role='dialog'], "
                "[id^='chatList_'], ul[id*='chatList_'], .chatbot_List",
            ):
                return has_application_confirmation(driver)
        elif has_application_confirmation(driver):
            return True
        time.sleep(QUESTION_POLL_INTERVAL)

    logger.warning(f"  Post timeout reached after {POST_TIMEOUT}s.")
    return False


def apply_to_jobs(driver, job_links, answers, resume):
    """
    Visit each job link and attempt to apply.
    Returns (applied_count, failed_count, applied_list).
    """
    applied = 0
    failed = 0
    applied_list = {'passed': [], 'failed': []}

    logger.info("=" * 50)
    logger.info(f"Starting job applications (max: {MAX_APPLICATIONS})...")
    logger.info("=" * 50)

    for i, link in enumerate(job_links, 1):
        if applied >= MAX_APPLICATIONS:
            logger.info(f"Reached max application limit ({MAX_APPLICATIONS}). Stopping.")
            break

        job_deadline = time.monotonic() + POST_TIMEOUT
        logger.info(f"[{i}/{len(job_links)}] Visiting job: {link}")
        try:
            driver.get(link)
        except WebDriverException as e:
            logger.warning(f"  ✗ Failed to load page: {e}")
            failed += 1
            applied_list['failed'].append(link)
            continue

        if time.monotonic() >= job_deadline:
            failed += 1
            applied_list['failed'].append(link)
            logger.warning(f"  ✗ Job timed out after {POST_TIMEOUT}s before applying.")
            continue

        # --- Click the "Apply" button ---
        if click_apply_button(driver, link):
            if complete_application_questions(driver, answers, resume, job_deadline):
                applied += 1
                applied_list['passed'].append(link)
                logger.info(f"  ✓ Application confirmed. Total: {applied}")
            else:
                failed += 1
                applied_list['failed'].append(link)
                if time.monotonic() >= job_deadline:
                    logger.warning(f"  ✗ Job timed out after {POST_TIMEOUT}s; application was not confirmed.")
                else:
                    logger.warning("  ✗ Application was not confirmed; not marked as applied.")
                continue
        else:
            failed += 1
            applied_list['failed'].append(link)
            logger.warning(f"  ✗ No Apply button found. Fail count: {failed}")
            continue

        if has_application_confirmation(driver):
            logger.info("  Naukri confirmation detected.")

    return applied, failed, applied_list


def save_results(applied_list):
    """Save applied/failed links to CSV."""
    csv_file = "naukriapplied.csv"
    final_dict = {k: pd.Series(v) for k, v in applied_list.items()}
    df = pd.DataFrame.from_dict(final_dict)
    df.to_csv(csv_file, index=False)
    logger.info(f"Results saved to {csv_file}")


def main():
    """Main entry point for the bot."""
    logger.info("=" * 50)
    logger.info("Naukri Auto-Apply Bot (Edge Edition - 2026 Update)")
    logger.info("=" * 50)

    if not validate_config():
        return

    logger.info(f"Keywords: {KEYWORDS}")
    logger.info(f"Location: {LOCATION or 'Anywhere'}")
    logger.info(f"Max applications: {MAX_APPLICATIONS}")
    logger.info(f"Pages per keyword: {PAGES_PER_KEYWORD}")
    answers, resume = load_answer_data()

    driver = None
    try:
        # Initialize browser
        driver = create_edge_driver()

        # Login
        login_naukri(driver)

        # Build all search URLs
        search_urls = build_search_urls()
        logger.info(f"Total search pages to open: {len(search_urls)}")

        # Collect all job links in parallel (multi-tab)
        job_links = collect_all_jobs_parallel(driver, search_urls)

        if not job_links:
            logger.warning("No job links found. Check your keywords and location.")
            return

        # Apply to jobs
        applied, failed, applied_list = apply_to_jobs(driver, job_links, answers, resume)

        # Save results
        save_results(applied_list)

        # Summary
        logger.info("=" * 50)
        logger.info("APPLICATION SUMMARY")
        logger.info(f"  Successfully applied: {applied}")
        logger.info(f"  Failed/Skipped:      {failed}")
        logger.info(f"  Total processed:     {applied + failed}")
        logger.info(f"  Results saved to:    naukriapplied.csv")
        logger.info("=" * 50)

    except Exception as e:
        logger.exception(f"An unexpected error occurred: {e}")
    finally:
        if driver:
            try:
                driver.quit()
                logger.info("Browser closed.")
            except Exception:
                pass


if __name__ == "__main__":
    main()
