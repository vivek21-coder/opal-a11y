import os
import re
import json
import io
import time
import asyncio
import logging
import base64
import queue
import pathlib
import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from google import genai
from google.genai import types
from dotenv import load_dotenv
from playwright.async_api import async_playwright
import edge_tts

# browser-use — advanced browser automation agent
from browser_use import Agent as BrowserUseAgent, Browser as BrowserUseBrowser, BrowserProfile as BUBrowserProfile
from browser_use import ChatGoogle as BUChatGoogle

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
STT_MODEL = os.getenv("STT_MODEL", "whisper-large-v3-turbo")
TTS_VOICE = os.getenv("TTS_VOICE", "en-US-AriaNeural")
TTS_ENABLED = os.getenv("TTS_ENABLED", "true").lower() == "true"
MAX_TASK_LENGTH = 5000
MAX_PAGE_TEXT = 50_000
MAX_HISTORY_MESSAGES = 20

client = genai.Client(api_key=GOOGLE_API_KEY) if GOOGLE_API_KEY else None

# Startup validation — warn early about missing keys
if not GOOGLE_API_KEY:
    logging.warning("GOOGLE_API_KEY is not set — LLM features will fail.")
if not GROQ_API_KEY:
    logging.warning("GROQ_API_KEY is not set — speech-to-text will fail.")

# Playwright browser singleton
_playwright_instance = None
_browser_instance = None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("opal")

app = FastAPI(title="Opal", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    return {"status": "ok", "model": GEMINI_MODEL, "stt_engine": "groq-whisper", "stt_model": STT_MODEL, "tts_engine": "edge-tts", "tts_voice": TTS_VOICE}


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    file_path = os.path.join(os.path.dirname(__file__), "../frontend/public/favicon.svg")
    if os.path.exists(file_path):
        return FileResponse(file_path, media_type="image/svg+xml")
    return {"error": "Favicon not found"}, 404


# ---------------------------------------------------------------------------
# HTML → clean text (fallback)
# ---------------------------------------------------------------------------
def extract_text_from_html(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header",
                     "aside", "form", "noscript", "iframe", "svg"]):
        tag.decompose()

    elements = soup.find_all(
        ["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "td", "th",
         "blockquote", "article", "section", "main", "figcaption", "pre"]
    )

    exclude_keywords = [
        "cookie", "privacy policy", "terms of service",
        "advertisement", "subscribe to our newsletter",
        "accept all cookies", "manage preferences",
    ]

    texts: list[str] = []
    seen: set[str] = set()
    for el in elements:
        text = el.get_text(separator=" ").strip()
        if len(text) < 20:
            continue
        if any(kw in text.lower() for kw in exclude_keywords):
            continue
        key = text[:120]
        if key in seen:
            continue
        seen.add(key)
        texts.append(text)

    return "\n\n".join(texts)


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------
SYSTEM_PROMPT_SUMMARIZE = (
    "You are an accessibility assistant and screen reader helping users understand web pages. "
    "Act like a screen reader: describe the page structure, headings, key content, links, and interactive elements. "
    "Be concise but thorough — cover everything a visually impaired user needs to know. "
    "Use bullet points or short paragraphs. Mention navigation options and actionable items. "
    "Do NOT mention that you are summarizing or that this is extracted text. "
    "IMPORTANT: You can ONLY read and describe page content. You CANNOT click links, navigate, "
    "open articles, scroll, or perform any browser actions. If the user asks you to do something "
    "interactive (click, open, navigate, scroll), do NOT claim you will do it. Instead, describe "
    "the relevant content you can see on the page."
)

SYSTEM_PROMPT_GENERAL = (
    "You are a friendly, helpful accessibility assistant. "
    "Be direct and concise but provide complete answers."
)

# browser-use Agent custom system prompt extension for accessibility focus
BROWSER_USE_EXTEND_PROMPT = (
    "You are an accessibility assistant helping a user who may be visually impaired. "
    "Provide clear, descriptive updates of what you're doing at each step. "
    "When the task is complete, give a concise summary of what was accomplished and what the user can see/do next. "
    "If you encounter cookie banners or popups, dismiss them before proceeding with the task. "
    "Be precise with form fields — fill one field at a time and verify autocomplete suggestions before selecting."
)

SYSTEM_PROMPT_URL_RESOLVER = (
    "You analyze user messages to determine if they want to visit or interact with a specific website. "
    "The user may speak in ANY language — understand their intent regardless of language.\n"
    'If yes, return JSON: {"has_url": true, "url": "full domain or URL", "intent": "what the user wants"}\n'
    'If no, return JSON: {"has_url": false}\n\n'
    "Rules:\n"
    '- Infer common websites from names in any language: '
    '"google"/"गूगल" → "google.com", "wikipedia"/"विकिपीडिया" → "wikipedia.org", '
    '"bbc" → "bbc.com", "gov.uk" → "gov.uk", "reddit" → "reddit.com", '
    '"twitter" → "x.com", "github" → "github.com", "amazon" → "amazon.com"\n'
    '- IMPORTANT: When a website name is followed by a search query, construct the SEARCH URL:\n'
    '  "google taylor swift" → "google.com/search?q=taylor+swift"\n'
    '  "google X" / "search for X" / "look up X" → "google.com/search?q=X"\n'
    '  "amazon laptop" → "amazon.com/s?k=laptop"\n'
    '  "reddit programming" → "reddit.com/search/?q=programming"\n'
    '  The intent for search queries should be "search"\n'
    '- If a specific page/topic is mentioned, construct the URL path: '
    '"wikipedia article on AI" → "wikipedia.org/wiki/Artificial_intelligence"\n'
    '- Keywords in any language that mean "open"/"खोलो", "go to"/"जाओ", "visit", "browse", '
    '"check", "look at"/"देखो", "summarize"/"सारांश" indicate website intent\n'
    '- Set intent to "visit" when the user just wants to open/go to the page\n'
    '- Set intent to "search" when the user wants to search for something on a website\n'
    '- Set intent to "summarize" only when the user explicitly asks to read, summarize, '
    'or explain the page content\n'
    '- If the message is clearly a general question with no website reference, return has_url: false\n'
    '- Always return a full domain (e.g., bbc.com, not just bbc)\n'
    '- Return ONLY valid JSON, no markdown fences, no explanation\n'
)

# Intent classifier prompt — used when a page is already loaded to determine
# what the user wants to do. Works in any language.
SYSTEM_PROMPT_INTENT_CLASSIFIER = (
    "You classify user messages into one of these intents when they have a webpage open. "
    "The user may speak in ANY language (Hindi, Spanish, French, etc.). "
    "Understand the meaning regardless of language.\n\n"
    "Intents:\n"
    '- "content_request": User wants to READ, SUMMARIZE, DESCRIBE, or EXPLAIN the current page '
    'content without navigating. '
    'Examples: "read this page", "इस पेज को पढ़ो", "summarize", "सारांश दो", '
    '"describe what\'s on this page", "explain this", "이 페이지 읽어줘", "lee esta página"\n'
    '- "browser_action": User wants to INTERACT with or NAVIGATE WITHIN the current page — '
    'click a link, open an article/link on the page, scroll, type, search, navigate to a section/heading, '
    'go to a part of the page, play media, select something, fill a form, go back, go forward, etc. '
    'This includes ANY request to open, click, or go to a specific link, article, story, section, '
    'heading, anchor, or element on the current page. '
    'Examples: "open the news article about Trump", "open the first link", '
    '"click the first link", "click on Society and Culture", "navigate to the introduction", '
    '"go to the references section", "scroll to the bottom", "scroll down", '
    '"navigate to society and culture", "go to section 3", "click on history", '
    '"take me to the conclusion", "find the search box and type hello", '
    '"open that article", "open the link about climate change", '
    '"यहाँ पर क्लिक करो", "नीचे स्क्रॉल करो", "go back", "वापस जाओ", "haz clic aquí", '
    '"press the submit button", "select the first option"\n'
    '- "new_url": User wants to navigate to a COMPLETELY DIFFERENT website or domain. '
    'Must mention a specific website name, domain, or URL. '
    'Examples: "open BBC", "विकिपीडिया खोलो", "go to gov.uk", "गूगल पर जाओ"\n'
    '- "general": A general knowledge question or conversation NOT about interacting with the page.\n\n'
    "Rules:\n"
    "- CRITICAL: If the user says 'navigate to', 'go to', 'click on', 'scroll to', 'open the', or similar "
    "action words followed by a section name, heading, article, link, or page element (NOT a website), choose browser_action\n"
    "- 'open the news article about X' = browser_action (clicking a link on the current page)\n"
    "- 'navigate to Society and Culture' = browser_action (it's a section on the current page)\n"
    "- 'go to BBC' = new_url (BBC is a different website)\n"
    "- 'open BBC' = new_url (BBC is a different website)\n"
    "- Only choose new_url if the user mentions a recognisable website name or domain\n"
    "- If ambiguous between content_request and browser_action, prefer browser_action\n"
    "- If ambiguous between general and browser_action, prefer browser_action\n"
    '- Return ONLY valid JSON: {"intent": "content_request|browser_action|new_url|general"}\n'
    "- No markdown fences, no explanation\n"
)


def build_system_prompt(is_page: bool, page_context: dict | None = None, language: str = "English") -> str:
    system = SYSTEM_PROMPT_SUMMARIZE if is_page else SYSTEM_PROMPT_GENERAL
    if language and language != "English":
        system += f"\n\nYou MUST respond entirely in {language}. Do not use English."
    else:
        system += "\n\nRespond in clear, plain English."
    if page_context and not is_page:
        system += (
            f"\n\nThe user previously visited {page_context['url']}. "
            f"Page content is provided in the conversation for reference."
        )
    return system


def build_browser_use_prompt(language: str = "English") -> str:
    prompt = BROWSER_USE_EXTEND_PROMPT
    if language and language != "English":
        prompt += f"\n\nIMPORTANT: Communicate all updates and the final summary entirely in {language}. Do not use English."
    return prompt


def build_contents(user_text: str, is_page: bool, conversation_history: list, page_context: dict | None = None, language: str = "English") -> list:
    if is_page:
        if language != "English":
            user_content = f"Here is the webpage content:\n\n{user_text[:MAX_PAGE_TEXT]}\n\nSummarize and translate the content into {language}:"
        else:
            user_content = f"Here is the webpage content:\n\n{user_text[:MAX_PAGE_TEXT]}\n\nSummary:"
    else:
        user_content = user_text

    contents = []
    recent = conversation_history[-MAX_HISTORY_MESSAGES:]
    for msg in recent:
        contents.append(types.Content(
            role=msg["role"],
            parts=[types.Part.from_text(text=msg["text"])],
        ))
    # For follow-up questions about a page, inject page content as context
    # in the conversation (not the system prompt) to keep system instruction concise
    if page_context and not is_page:
        contents.append(types.Content(
            role="user",
            parts=[types.Part.from_text(
                text=f"[For reference, here is the content of {page_context['url']}:]\n{page_context['text'][:MAX_PAGE_TEXT]}"
            )],
        ))
        contents.append(types.Content(
            role="model",
            parts=[types.Part.from_text(text="I have the page content. What would you like to know?")],
        ))
    contents.append(types.Content(
        role="user",
        parts=[types.Part.from_text(text=user_content)],
    ))
    return contents


# ---------------------------------------------------------------------------
# LLM via Gemini (text streaming)
# ---------------------------------------------------------------------------
def llm_stream(system_instruction: str, contents):
    if not client:
        raise RuntimeError("GOOGLE_API_KEY not configured")

    t0 = time.perf_counter()
    first_token_time = None
    response = client.models.generate_content_stream(
        model=GEMINI_MODEL,
        contents=contents,
        config=types.GenerateContentConfig(
            system_instruction=system_instruction,
            temperature=0.3,
        ),
    )
    for chunk in response:
        if chunk.text:
            if first_token_time is None:
                first_token_time = time.perf_counter()
                log.info("[Gemini LLM stream] first token in %.0fms", (first_token_time - t0) * 1000)
            yield chunk.text
    log.info("[Gemini LLM stream] completed in %.0fms", (time.perf_counter() - t0) * 1000)


# ---------------------------------------------------------------------------
# Gemini JSON call (non-streaming, for action parsing)
# ---------------------------------------------------------------------------
async def gemini_json_call(system_prompt: str, user_prompt: str, max_tokens: int = 512) -> str:
    if not client:
        raise RuntimeError("GOOGLE_API_KEY not configured")

    loop = asyncio.get_event_loop()

    def _call():
        return client.models.generate_content(
            model=GEMINI_MODEL,
            contents=user_prompt,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                temperature=0.1,
                max_output_tokens=max_tokens,
            ),
        )

    t0 = time.perf_counter()
    response = await loop.run_in_executor(None, _call)
    log.info("[Gemini JSON call] completed in %.0fms", (time.perf_counter() - t0) * 1000)
    text = response.text.strip() if response.text else ""

    # Strip markdown code fences if present
    fence_match = re.search(r'```(?:json)?\s*\n?(.*?)\n?\s*```', text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1).strip()
    elif text.startswith("```"):
        text = re.sub(r'^```(?:json)?\s*', '', text)
        text = re.sub(r'\s*```$', '', text)

    return text


# ---------------------------------------------------------------------------
# Long-response condensation
# ---------------------------------------------------------------------------
MAX_RESPONSE_WORDS = 150  # Word threshold for auto-condensation

async def condense_long_response(text: str, language: str = "English") -> str:
    """If *text* exceeds MAX_RESPONSE_WORDS, ask the LLM for a shorter version."""
    word_count = len(text.split())
    if word_count <= MAX_RESPONSE_WORDS:
        return text
    if not client:
        return text

    log.info("Response too long (%d words) — condensing", word_count)
    lang_instruction = f" Respond in {language}." if language != "English" else ""
    system = (
        "You are a concise accessibility assistant. The user received a very long response. "
        "Rewrite it to be shorter and easier to listen to via text-to-speech. "
        "Keep all important facts, numbers, and key points. Remove repetition and filler. "
        "Use short sentences. Target around 80 words."
        f"{lang_instruction}"
    )

    try:
        loop = asyncio.get_event_loop()
        def _call():
            return client.models.generate_content(
                model=GEMINI_MODEL,
                contents=text,
                config=types.GenerateContentConfig(
                    system_instruction=system,
                    temperature=0.1,
                    max_output_tokens=512,
                ),
            )
        response = await loop.run_in_executor(None, _call)
        condensed = (response.text or "").strip()
        if condensed and len(condensed.split()) < word_count:
            log.info("Condensed from %d to %d words", word_count, len(condensed.split()))
            return condensed
        log.warning("Condensation produced no improvement (%d words)", len(condensed.split()) if condensed else 0)
    except Exception as e:
        log.warning("Condensation failed, using original: %s", e)

    return text





# ---------------------------------------------------------------------------
# URL resolution via Gemini
# ---------------------------------------------------------------------------
async def resolve_url_from_prompt(user_text: str) -> dict:
    """Use Gemini to detect if user wants to visit a website and resolve the URL."""
    if is_url(user_text):
        clean_url = user_text.strip()
        if not clean_url.startswith(("http://", "https://")):
            clean_url = "https://" + clean_url
        return {"has_url": True, "url": clean_url, "intent": "summarize this page"}

    text = await gemini_json_call(
        SYSTEM_PROMPT_URL_RESOLVER,
        f'User message: "{user_text}"',
        max_tokens=256,
    )

    try:
        result = json.loads(text)
        if result.get("has_url") and result.get("url"):
            url = result["url"]
            if not url.startswith(("http://", "https://")):
                url = "https://" + url
            result["url"] = url
        return result
    except json.JSONDecodeError:
        log.warning("Failed to parse URL resolver JSON: %s", text[:200])
        return {"has_url": False}


# Keywords that strongly indicate a browser action (in-page interaction)
_BROWSER_ACTION_PATTERNS = re.compile(
    r'\b('
    r'click|tap|press|select|check|uncheck|toggle|expand|collapse|'
    r'open\s+(the\s+)?(article|link|story|news|video|image|post|item|result|page|menu|dropdown|modal|tab|first|second|third|last|next|that|this|an?\s)|'
    r'scroll\s*(up|down|to|left|right)|'
    r'navigate\s+to\s+(?!https?://|www\.|\S+\.(com|org|net|uk|io|gov))(?=\S)|'
    r'go\s+to\s+(the\s+)?(section|heading|part|area|tab|paragraph|chapter|top|bottom|beginning|end)|'
    r'find\s+(the\s+)?\S.*(button|link|field|box|input|menu|section)|'
    r'type\b|fill\s+in|enter\s+text|submit|go\s+back|go\s+forward|refresh|reload'
    r')\b',
    re.IGNORECASE,
)


async def classify_page_intent(user_text: str) -> str:
    """Classify user intent when a page is already loaded. Returns one of:
    content_request, browser_action, new_url, general."""
    # Quick check: if the message is a plain URL, it's definitely a new_url
    if is_url(user_text):
        return "new_url"

    # Quick check: strong browser-action keywords bypass LLM classification
    if _BROWSER_ACTION_PATTERNS.search(user_text):
        log.info("Keyword match → browser_action (task: %s)", user_text[:60])
        return "browser_action"

    text = await gemini_json_call(
        SYSTEM_PROMPT_INTENT_CLASSIFIER,
        f'User message: "{user_text}"',
        max_tokens=64,
    )
    try:
        result = json.loads(text)
        intent = result.get("intent", "general")
        if intent in ("content_request", "browser_action", "new_url", "general"):
            return intent
        return "general"
    except json.JSONDecodeError:
        log.warning("Failed to parse intent classifier JSON: %s", text[:200])
        return "general"


# ---------------------------------------------------------------------------
# Playwright browser management
# ---------------------------------------------------------------------------
async def get_browser():
    global _playwright_instance, _browser_instance
    if _browser_instance is not None and _browser_instance.is_connected():
        return _browser_instance

    # Browser is dead or not started — clean up and relaunch
    _browser_instance = None
    for attempt in range(2):
        try:
            if _playwright_instance is None:
                _playwright_instance = await async_playwright().start()
            _browser_instance = await _playwright_instance.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                ],
            )
            return _browser_instance
        except Exception as e:
            log.warning("Browser launch attempt %d failed: %s", attempt + 1, e)
            # Playwright subprocess likely died — tear it down fully
            try:
                if _playwright_instance:
                    await _playwright_instance.stop()
            except Exception:
                pass
            _playwright_instance = None
            _browser_instance = None
            if attempt == 1:
                raise
    return _browser_instance


class BrowserSession:
    """Persistent Playwright browser context + page for a single WebSocket connection."""

    def __init__(self):
        self.context = None
        self.page = None
        self.current_url = None
        self.viewport_width = 1280
        self.viewport_height = 900
        self.locale = "en-US"

    def set_viewport(self, width: int, height: int):
        self.viewport_width = width
        self.viewport_height = height

    async def set_locale(self, locale: str):
        """Set browser locale. Recreates context since Playwright doesn't allow changing locale."""
        if locale == self.locale:
            return
        self.locale = locale
        # Must recreate context for locale to take effect
        if self.context:
            try:
                await self.context.close()
            except Exception:
                pass
            self.context = None
            self.page = None

    async def ensure_page(self):
        if self.page is not None and not self.page.is_closed():
            # Update viewport size on existing page if it changed
            current = self.page.viewport_size
            if current and (current["width"] != self.viewport_width or current["height"] != self.viewport_height):
                await self.page.set_viewport_size({"width": self.viewport_width, "height": self.viewport_height})
            return
        # Clean up stale context before creating a new one
        if self.context:
            try:
                await self.context.close()
            except Exception:
                pass
            self.context = None
            self.page = None
        browser = await get_browser()
        # Build Accept-Language header from locale (e.g. "hi" → "hi,en;q=0.5")
        lang_short = self.locale.split("-")[0]
        accept_lang = f"{self.locale},{lang_short};q=0.9,en;q=0.5" if lang_short != "en" else "en-US,en;q=0.9"
        self.context = await browser.new_context(
            viewport={"width": self.viewport_width, "height": self.viewport_height},
            locale=self.locale,
            extra_http_headers={"Accept-Language": accept_lang},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )

        self.page = await self.context.new_page()

    @staticmethod
    def _to_translate_goog_url(url: str, lang_code: str) -> str:
        """Rewrite a URL to go through Google Translate's translate.goog proxy.

        Example: https://en.wikipedia.org/wiki/Python →
                 https://en-wikipedia-org.translate.goog/wiki/Python?_x_tr_sl=auto&_x_tr_tl=es&_x_tr_hl=es

        This serves the page directly with translated content — no iframes,
        full-page translation, and links within the page stay translated.
        """
        from urllib.parse import urlparse, urlencode, urlunparse, parse_qs, urljoin
        parsed = urlparse(url)
        # Transform domain: en.wikipedia.org → en-wikipedia-org.translate.goog
        translated_host = parsed.hostname.replace(".", "-") + ".translate.goog"
        # Preserve existing query params and add translation params
        params = parse_qs(parsed.query, keep_blank_values=True)
        params["_x_tr_sl"] = ["auto"]
        params["_x_tr_tl"] = [lang_code]
        params["_x_tr_hl"] = [lang_code]
        new_query = urlencode(params, doseq=True)
        translated = parsed._replace(scheme="https", netloc=translated_host, query=new_query)
        return urlunparse(translated)

    async def navigate(self, url: str, language: str = "English") -> dict:
        """Navigate to a URL, auto-dismiss cookies, return screenshot + content."""
        if not url.startswith(("http://", "https://")):
            url = "https://" + url

        await self.ensure_page()

        # For non-English languages, route through Google Translate's translate.goog proxy.
        # This serves the full translated page directly (not an iframe), so:
        # - The entire scrollable page is translated
        # - Links within the page stay translated (rewritten to translate.goog)
        # - Browser-use actions that click links remain in translated mode
        lang_code = _LANG_CODES.get(language, "en")
        if language != "English" and lang_code != "en":
            nav_url = self._to_translate_goog_url(url, lang_code)
            log.info("Navigating via translate.goog: %s", nav_url)
        else:
            nav_url = url

        await self.page.goto(nav_url, wait_until="domcontentloaded", timeout=30000)

        # Wait for page to settle
        try:
            await self.page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass  # Some pages never reach networkidle
        await self.page.wait_for_timeout(1500)

        # Auto-dismiss cookie banners
        await self._dismiss_cookies()

        self.current_url = url  # Store the original URL, not the translate.goog one
        return await self._capture_state()

    async def _dismiss_cookies(self):
        """Try to dismiss common cookie consent banners."""
        cookie_selectors = [
            # Common button texts
            'button:has-text("Accept")',
            'button:has-text("Accept all")',
            'button:has-text("Accept All")',
            'button:has-text("I agree")',
            'button:has-text("Got it")',
            'button:has-text("OK")',
            'button:has-text("Allow all")',
            'button:has-text("Allow All")',
            'button:has-text("Agree")',
            'button:has-text("Continue")',
            # Common IDs/classes
            '#onetrust-accept-btn-handler',
            '#accept-cookies',
            '.cookie-accept',
            '[data-testid="cookie-accept"]',
            '#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll',
            '.cc-accept',
            '#sp-cc-accept',  # Amazon
        ]

        for selector in cookie_selectors:
            try:
                btn = self.page.locator(selector).first
                if await btn.is_visible(timeout=500):
                    await btn.click(timeout=2000)
                    log.info("Dismissed cookie banner via: %s", selector)
                    await self.page.wait_for_timeout(800)
                    break
            except Exception:
                continue

    async def _capture_state(self) -> dict:
        """Capture screenshot and extract page content."""
        # Clip to a max height to avoid timeouts on very long pages (e.g. Wikipedia)
        try:
            max_screenshot_height = self.viewport_height * 5
            page_height = await self.page.evaluate("() => document.body.scrollHeight")
            if page_height > max_screenshot_height:
                screenshot_bytes = await self.page.screenshot(
                    type="png",
                    clip={"x": 0, "y": 0, "width": self.viewport_width, "height": max_screenshot_height},
                    timeout=15000,
                )
            else:
                screenshot_bytes = await self.page.screenshot(type="png", full_page=True, timeout=15000)
        except Exception:
            # Fallback to viewport-only screenshot
            screenshot_bytes = await self.page.screenshot(type="png", timeout=10000)
        screenshot_b64 = base64.b64encode(screenshot_bytes).decode("ascii")

        content = await self.page.evaluate("""() => {
            const body = document.body;
            const text = body ? body.innerText : '';

            const imgs = document.querySelectorAll('img');
            const altTexts = [];
            imgs.forEach(img => {
                const alt = img.alt || img.title || '';
                if (alt.trim()) altTexts.push(alt.trim());
            });

            return {
                text: text.substring(0, 50000),
                altTexts: altTexts.slice(0, 100),
                title: document.title || ''
            };
        }""")

        return {
            "screenshot": screenshot_b64,
            "text": content.get("text", ""),
            "altTexts": content.get("altTexts", []),
            "title": content.get("title", ""),
            "url": self.current_url or self.page.url,
        }

    async def close(self):
        if self.context:
            try:
                await self.context.close()
            except Exception:
                pass
            self.context = None
            self.page = None


def build_page_content(text: str, alt_texts: list[str], title: str = "") -> str:
    parts = []
    if title:
        parts.append(f"Page Title: {title}")
    if text:
        parts.append(f"Page Content:\n{text[:MAX_PAGE_TEXT]}")
    if alt_texts:
        alt_section = "\n".join(f"- {alt}" for alt in alt_texts[:50])
        parts.append(f"Images on the page:\n{alt_section}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# STT via Groq Whisper (fast, reliable, high quality)
# ---------------------------------------------------------------------------
# Map language names to ISO-639-1 codes for Groq Whisper
_LANG_CODES = {
    "English": "en", "Spanish": "es", "French": "fr", "German": "de",
    "Chinese": "zh", "Hindi": "hi", "Arabic": "ar", "Portuguese": "pt",
    "Japanese": "ja", "Korean": "ko", "Italian": "it", "Dutch": "nl",
    "Russian": "ru", "Turkish": "tr", "Polish": "pl", "Swedish": "sv",
}

# Map language names to browser locale codes (used for Playwright context)
_LOCALE_CODES = {
    "English": "en-US", "Spanish": "es-ES", "French": "fr-FR", "German": "de-DE",
    "Chinese": "zh-CN", "Hindi": "hi-IN", "Arabic": "ar-SA", "Portuguese": "pt-BR",
    "Japanese": "ja-JP", "Korean": "ko-KR", "Italian": "it-IT", "Dutch": "nl-NL",
    "Russian": "ru-RU", "Turkish": "tr-TR", "Polish": "pl-PL", "Swedish": "sv-SE",
}


def _is_noise_transcription(text: str) -> bool:
    """Detect transcriptions that are likely noise/silence/gibberish."""
    t = text.strip()
    if not t:
        return True
    # Common silence/noise hallucinations from Whisper
    noise_patterns = [
        r'^\s*\.+\s*$',                    # just dots
        r'^\s*\*+\s*$',                    # just asterisks
        r'^\s*,+\s*$',                     # just commas
        r'^\s*-+\s*$',                     # just dashes
        r'^(Subtitles by|Translated by|Transcribed by|Copyright)',  # Whisper hallucination
        r'^\s*\[.*\]\s*$',                 # [Music], [Silence], etc.
        r'^\s*\(.*\)\s*$',                 # (music), (silence), etc.
        r'^(Thank you|Thanks)\.?\s*$',     # common noise hallucination
        r'^(Bye|Goodbye)\.?\s*$',          # common noise hallucination
        r'^(You|you)$',                    # single word hallucination
        r'^(Hmm|Uh|Um|Ah|Oh)\.?\s*$',     # filler sounds
        r'^\W+$',                          # only punctuation/symbols
    ]
    for pat in noise_patterns:
        if re.match(pat, t, re.IGNORECASE):
            return True
    # Very short transcriptions (1-2 chars) are almost always noise
    if len(t) <= 2:
        return True
    # Repeated single word/syllable (e.g. "na na na", "the the the")
    words = t.lower().split()
    if len(words) >= 2 and len(set(words)) == 1:
        return True
    return False


async def transcribe_audio(audio_bytes: bytes, mime_type: str = "audio/wav", language: str = "English") -> str:
    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY not configured")

    # Map mime types to file extensions for the multipart upload
    ext_map = {
        "audio/wav": "audio.wav",
        "audio/webm": "audio.webm",
        "audio/ogg": "audio.ogg",
        "audio/mp3": "audio.mp3",
        "audio/mpeg": "audio.mp3",
        "audio/mp4": "audio.mp4",
        "audio/x-m4a": "audio.m4a",
    }
    filename = ext_map.get(mime_type, "audio.wav")

    # Pass language hint to Groq for better accuracy
    data: dict[str, str] = {"model": STT_MODEL, "response_format": "text"}
    lang_code = _LANG_CODES.get(language)
    if lang_code:
        data["language"] = lang_code

    t0 = time.perf_counter()
    async with httpx.AsyncClient(timeout=30) as http:
        response = await http.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            files={"file": (filename, audio_bytes, mime_type)},
            data=data,
        )
        response.raise_for_status()
        log.info("[Groq STT] completed in %.0fms (%d bytes audio)", (time.perf_counter() - t0) * 1000, len(audio_bytes))
        return response.text.strip()


# ---------------------------------------------------------------------------
# TTS via Edge TTS (Microsoft neural voices — free, fast, no rate limits)
# ---------------------------------------------------------------------------
# Language → Edge TTS voice mapping
EDGE_TTS_VOICES = {
    "English": "en-US-AriaNeural",
    "Spanish": "es-ES-ElviraNeural",
    "French": "fr-FR-DeniseNeural",
    "German": "de-DE-KatjaNeural",
    "Chinese": "zh-CN-XiaoxiaoNeural",
    "Hindi": "hi-IN-SwaraNeural",
    "Arabic": "ar-SA-ZariyahNeural",
    "Portuguese": "pt-BR-FranciscaNeural",
    "Japanese": "ja-JP-NanamiNeural",
    "Korean": "ko-KR-SunHiNeural",
}


async def synthesize_speech_chunk(text: str, chunk_index: int, language: str = "English") -> dict | None:
    if not TTS_ENABLED or not text.strip():
        return None
    try:
        # Strip markdown formatting before sending to TTS
        clean = re.sub(r'\*{1,3}([^*]+)\*{1,3}', r'\1', text)  # ***bold italic*** / **bold** / *italic*
        clean = re.sub(r'`{1,3}[^`]*`{1,3}', '', clean)  # `code` and ```code blocks```
        clean = re.sub(r'#{1,6}\s+', '', clean)  # ### headings
        clean = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', clean)  # [links](url)
        clean = re.sub(r'^\s*[\*\-•]\s+', '', clean, flags=re.MULTILINE)  # bullet points: * item, - item, • item
        clean = re.sub(r'^\s*\d+\.\s+', '', clean, flags=re.MULTILINE)  # numbered lists: 1. item
        clean = re.sub(r'\*+', '', clean)  # any remaining stray asterisks

        voice = EDGE_TTS_VOICES.get(language, TTS_VOICE)
        communicate = edge_tts.Communicate(clean, voice)

        # Collect all audio bytes
        t0 = time.perf_counter()
        audio_bytes = io.BytesIO()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_bytes.write(chunk["data"])

        audio_data = audio_bytes.getvalue()
        log.info("[Edge TTS] chunk %d synthesized in %.0fms (%d chars → %d bytes)", chunk_index, (time.perf_counter() - t0) * 1000, len(text), len(audio_data))
        if not audio_data:
            return None

        return {
            "audio": base64.b64encode(audio_data).decode("ascii"),
            "mimeType": "audio/mp3",
            "chunkIndex": chunk_index,
            "text": text,
        }
    except Exception as e:
        log.warning("TTS chunk %d failed: %s", chunk_index, e)
        return None


def split_into_tts_chunks(text: str) -> list[str]:
    """Split text into sentence-level chunks for faster streaming."""
    # First split by paragraphs
    paragraphs = [p.strip() for p in re.split(r'\n{2,}', text) if p.strip()]
    chunks = []
    for para in paragraphs:
        # Split every paragraph into sentences for faster first-chunk delivery
        sentences = re.split(r'(?<=[.!?])\s+', para)
        current = ""
        for sent in sentences:
            if len(current) + len(sent) > 200 and current:
                chunks.append(current.strip())
                current = sent
            else:
                current = (current + " " + sent).strip()
        if current:
            chunks.append(current)
    return chunks if chunks else [text]


# ---------------------------------------------------------------------------
# Page fetching (fallback when Playwright fails)
# ---------------------------------------------------------------------------
async def fetch_page_text(url: str) -> str:
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
    }

    t0 = time.perf_counter()
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as c:
        response = await c.get(url, headers=headers)
        response.raise_for_status()
        log.info("[HTTP fetch] %s completed in %.0fms", url, (time.perf_counter() - t0) * 1000)
        return response.text


# ---------------------------------------------------------------------------
# URL detection
# ---------------------------------------------------------------------------
def is_url(text: str) -> bool:
    t = text.strip()
    return (
        t.startswith("http://")
        or t.startswith("https://")
        or re.match(r'^[\w\-]+\.[a-zA-Z]{2,}(/\S*)?$', t) is not None
    )


# ---------------------------------------------------------------------------
# WebSocket agent endpoint
# ---------------------------------------------------------------------------
@app.websocket("/ws/agent")
async def agent_socket(websocket: WebSocket):
    await websocket.accept()
    log.info("Client connected")

    conversation_history: list[dict] = []
    page_context: dict | None = None
    browser_session = BrowserSession()
    language = "English"
    bu_browser = BrowserUseBrowser(browser_profile=BUBrowserProfile(
        headless=True,
        keep_alive=True,
    ))
    bu_agent_initialized = False

    def _append_history(role: str, text: str):
        """Append to conversation_history and prune to prevent unbounded growth."""
        conversation_history.append({"role": role, "text": text})
        # Keep only the most recent messages to prevent memory growth
        if len(conversation_history) > MAX_HISTORY_MESSAGES * 2:
            del conversation_history[:-MAX_HISTORY_MESSAGES]

    try:
        while True:
            try:
                raw_msg = await websocket.receive_text()
            except WebSocketDisconnect:
                break

            # --- STT ---
            if raw_msg.startswith("STT_AUDIO:"):
                try:
                    payload = json.loads(raw_msg[10:])
                    audio_bytes = base64.b64decode(payload.get("audio", ""))
                    mime_type = payload.get("mimeType", "audio/wav")
                    log.info("STT request (%d bytes)", len(audio_bytes))
                    await websocket.send_text("STATUS: Transcribing audio...")
                    transcript = await transcribe_audio(audio_bytes, mime_type, language=language)
                    # Filter out gibberish / noise transcriptions
                    if transcript and _is_noise_transcription(transcript):
                        log.info("Filtered noise transcription: %s", transcript[:80])
                        transcript = ""
                    await websocket.send_text(f"STT_RESULT:{transcript}" if transcript else "STT_RESULT:")
                except Exception as e:
                    log.warning("STT error: %s", e)
                    await websocket.send_text(f"ERROR: Transcription failed — {e}")
                continue

            # --- Cancel ---
            if raw_msg.strip() == "CANCEL:":
                log.info("Client requested cancel")
                await websocket.send_text("CANCELLED:")
                continue

            # --- Language setting ---
            if raw_msg.startswith("SET_LANGUAGE:"):
                new_language = raw_msg[13:].strip() or "English"
                if new_language != language:
                    language = new_language
                    locale = _LOCALE_CODES.get(language, "en-US")
                    await browser_session.set_locale(locale)
                    log.info("Language set to: %s (locale: %s)", language, locale)

                    # Re-navigate current page through translate.goog if a page is loaded
                    current_page_url = browser_session.current_url
                    if current_page_url:
                        try:
                            await websocket.send_text("STATUS: Translating page...")
                            page_data = await browser_session.navigate(current_page_url, language=language)
                            if page_data.get("screenshot"):
                                await websocket.send_text("SCREENSHOT:" + json.dumps({
                                    "url": page_data["url"],
                                    "screenshot": page_data["screenshot"],
                                }))
                            await websocket.send_text("STATUS: ")
                        except Exception as e:
                            log.warning("Failed to re-navigate for language change: %s", e)
                            await websocket.send_text("STATUS: ")
                continue

            # --- Conversation reset ---
            if raw_msg.strip() == "NEW_CHAT:":
                conversation_history.clear()
                page_context = None
                await browser_session.close()
                bu_agent_initialized = False
                try:
                    await bu_browser.close()
                except Exception:
                    pass
                bu_browser = BrowserUseBrowser(browser_profile=BUBrowserProfile(
                    headless=True,
                    keep_alive=True,
                ))
                log.info("Conversation reset")
                continue

            # --- Parse message ---
            viewport = None
            if raw_msg.startswith("CHAT_MSG:"):
                try:
                    payload = json.loads(raw_msg[9:])
                    task = payload.get("text", "").strip()
                    viewport = payload.get("viewport")
                except (json.JSONDecodeError, KeyError):
                    task = raw_msg[9:].strip()
            else:
                task = raw_msg.strip()

            # Update browser viewport if frontend sent dimensions.
            # Enforce a minimum desktop-width viewport (1280px) so websites
            # always serve their full desktop layout, never mobile/tablet views.
            if viewport:
                w = max(1280, min(3840, int(viewport.get("width", 1280))))
                h = max(720, min(2160, int(viewport.get("height", 900))))
                browser_session.set_viewport(w, h)

            if not task:
                await websocket.send_text("ERROR: Please enter a task or URL.")
                continue

            if len(task) > MAX_TASK_LENGTH:
                await websocket.send_text(
                    f"ERROR: Input too long ({len(task)} chars). Max {MAX_TASK_LENGTH}."
                )
                continue

            log.info("Task: %s", task[:80])

            # --- If we have an active page, classify intent using LLM ---
            if browser_session.page and not browser_session.page.is_closed():
                await websocket.send_text("STATUS: Analyzing your request...")
                page_intent = await classify_page_intent(task)
                log.info("Page intent: %s (task: %s)", page_intent, task[:60])

                if page_intent == "content_request":
                    await websocket.send_text("STATUS: Reading the current page...")
                    try:
                        # Wait for dynamically loaded content (SPAs, lazy loading)
                        try:
                            await browser_session.page.wait_for_load_state("networkidle", timeout=8000)
                        except Exception:
                            pass  # Best-effort; some pages never reach networkidle
                        state = await browser_session._capture_state()
                        # Send fresh screenshot
                        await websocket.send_text("SCREENSHOT:" + json.dumps({
                            "url": state["url"],
                            "screenshot": state["screenshot"],
                        }))
                        text = build_page_content(
                            state["text"],
                            state.get("altTexts", []),
                            state.get("title", ""),
                        )
                        if not text or len(text.split()) < 20:
                            await websocket.send_text(
                                "AGENT_RESULT: This page doesn't have enough readable text."
                            )
                            if TTS_ENABLED:
                                await websocket.send_text("TTS_DONE:")
                            continue

                        page_context = {"url": state["url"], "text": text}
                        # Use the page-summarization prompt but include user's
                        # original request so the LLM knows what they want
                        await websocket.send_text("STATUS: Summarizing content...")
                        system_instruction = build_system_prompt(is_page=True, language=language)
                        if language != "English":
                            user_content = (
                                f"The user asked: \"{task}\"\n\n"
                                f"Here is the webpage content:\n\n{text[:MAX_PAGE_TEXT]}\n\n"
                                f"Summarize and translate the content into {language}:"
                            )
                        else:
                            user_content = (
                                f"The user asked: \"{task}\"\n\n"
                                f"Here is the webpage content:\n\n{text[:MAX_PAGE_TEXT]}"
                            )
                        contents = []
                        recent = conversation_history[-MAX_HISTORY_MESSAGES:]
                        for msg in recent:
                            contents.append(types.Content(
                                role=msg["role"],
                                parts=[types.Part.from_text(text=msg["text"])],
                            ))
                        contents.append(types.Content(
                            role="user",
                            parts=[types.Part.from_text(text=user_content)],
                        ))

                        # Stream LLM response (reuse the same streaming logic)
                        loop = asyncio.get_event_loop()
                        token_queue: queue.Queue[str | None] = queue.Queue()

                        def _content_stream_worker():
                            try:
                                for token in llm_stream(system_instruction, contents):
                                    token_queue.put(token)
                            except Exception as e:
                                token_queue.put(f"__ERROR__:{e}")
                            finally:
                                token_queue.put(None)

                        loop.run_in_executor(None, _content_stream_worker)

                        full_response = ""
                        await websocket.send_text("STREAM_START:")
                        while True:
                            try:
                                token = await asyncio.wait_for(
                                    loop.run_in_executor(None, token_queue.get), timeout=120,
                                )
                            except asyncio.TimeoutError:
                                break
                            if token is None:
                                break
                            if isinstance(token, str) and token.startswith("__ERROR__:"):
                                if not full_response:
                                    await websocket.send_text(f"ERROR: LLM error — {token[10:]}")
                                break
                            full_response += token
                            await websocket.send_text(f"STREAM_TOKEN: {token}")

                        if full_response:
                            full_response = await condense_long_response(full_response, language)
                            await websocket.send_text(f"STREAM_END: {full_response}")
                            _append_history("user", task)
                            _append_history("model", full_response)

                            if TTS_ENABLED:
                                chunks = split_into_tts_chunks(full_response)
                                first_result = await synthesize_speech_chunk(chunks[0], 0, language)
                                if first_result:
                                    await websocket.send_text("TTS_CHUNK:" + json.dumps(first_result))
                                if len(chunks) > 1:
                                    results = await asyncio.gather(*[
                                        synthesize_speech_chunk(c, i, language) for i, c in enumerate(chunks[1:], 1)
                                    ])
                                    for r in results:
                                        if r:
                                            await websocket.send_text("TTS_CHUNK:" + json.dumps(r))
                                await websocket.send_text("TTS_DONE:")
                        else:
                            # No content produced — send error instead of empty bubble
                            await websocket.send_text("ERROR: Could not generate a response. Please try again.")
                            if TTS_ENABLED:
                                await websocket.send_text("TTS_DONE:")

                    except Exception as e:
                        log.error("Content read error: %s", e, exc_info=True)
                        await websocket.send_text(f"ERROR: Could not read the page — {e}")
                        if TTS_ENABLED:
                            await websocket.send_text("TTS_DONE:")
                    continue

                if page_intent == "browser_action":
                    await websocket.send_text("STATUS: Working on it...")
                    try:
                        # --- browser-use Agent integration ---
                        bu_llm = BUChatGoogle(model=GEMINI_MODEL, api_key=GOOGLE_API_KEY)

                        # Prepare initial navigation if user has a page open
                        # but browser-use hasn't been to it yet
                        bu_initial_actions = None
                        if not bu_agent_initialized and browser_session.current_url:
                            # Use translate.goog URL for non-English so translation persists
                            bu_nav_url = browser_session.current_url
                            bu_lang_code = _LANG_CODES.get(language, "en")
                            if language != "English" and bu_lang_code != "en":
                                bu_nav_url = BrowserSession._to_translate_goog_url(bu_nav_url, bu_lang_code)
                            bu_initial_actions = [{'navigate': {'url': bu_nav_url, 'new_tab': False}}]

                        # Step callback — streams live progress to frontend
                        async def _bu_step_callback(state_summary, agent_output, step_num):
                            try:
                                next_goal = ""
                                if agent_output and hasattr(agent_output, 'current_state'):
                                    cs = agent_output.current_state
                                    next_goal = getattr(cs, 'next_goal', '') or ''
                                status = f"Step {step_num + 1}: {next_goal}" if next_goal else f"Step {step_num + 1}..."
                                await websocket.send_text(f"STATUS: {status}")

                                # Send screenshot to frontend
                                if state_summary and getattr(state_summary, 'screenshot', None):
                                    url = getattr(state_summary, 'url', '') or ''
                                    await websocket.send_text("SCREENSHOT:" + json.dumps({
                                        "url": url,
                                        "screenshot": state_summary.screenshot,
                                    }))
                            except Exception as cb_err:
                                log.warning("browser-use step callback error: %s", cb_err)

                        # Build the task — include current URL for context
                        agent_task = task
                        if browser_session.current_url and not bu_agent_initialized:
                            agent_task = f"You are on {browser_session.current_url}. {task}"

                        agent = BrowserUseAgent(
                            task=agent_task,
                            llm=bu_llm,
                            browser=bu_browser,
                            register_new_step_callback=_bu_step_callback,
                            extend_system_message=build_browser_use_prompt(language),
                            use_vision=True,
                            max_actions_per_step=3,
                            initial_actions=bu_initial_actions,
                        )

                        MAX_AGENT_STEPS = 150
                        log.info("Running browser-use agent (max %d steps): %s", MAX_AGENT_STEPS, agent_task[:100])
                        history = await agent.run(max_steps=MAX_AGENT_STEPS)
                        bu_agent_initialized = True

                        # Extract result
                        final_text = history.final_result() or "Task completed."
                        is_success = history.is_successful()
                        log.info("browser-use agent done (success=%s): %s", is_success, final_text[:200])

                        # Try to get the final URL from history
                        last_url = ""
                        if history.history:
                            last_state = history.history[-1].state
                            if last_state:
                                last_url = getattr(last_state, 'url', '') or ''

                        # Send final screenshot from browser-use
                        try:
                            if history.history and history.history[-1].state:
                                final_screenshot_path = getattr(history.history[-1].state, 'screenshot_path', None)
                                if final_screenshot_path:
                                    ss_path = pathlib.Path(final_screenshot_path)
                                    if ss_path.exists():
                                        ss_b64 = base64.b64encode(ss_path.read_bytes()).decode('ascii')
                                        await websocket.send_text("SCREENSHOT:" + json.dumps({
                                            "url": last_url,
                                            "screenshot": ss_b64,
                                        }))
                        except Exception as ss_err:
                            log.warning("Failed to send final browser-use screenshot: %s", ss_err)

                        # Update page context for follow-up questions
                        if last_url:
                            page_context = {"url": last_url, "text": final_text}

                        # Condense if too long, then send to frontend
                        final_text = await condense_long_response(final_text, language)
                        await websocket.send_text("STREAM_START:")
                        await websocket.send_text(f"STREAM_TOKEN: {final_text}")
                        await websocket.send_text(f"STREAM_END: {final_text}")

                        _append_history("user", f"[Browser action] {task}")
                        _append_history("model", final_text)

                        if TTS_ENABLED:
                            tts_result = await synthesize_speech_chunk(final_text, 0, language)
                            if tts_result:
                                await websocket.send_text("TTS_CHUNK:" + json.dumps(tts_result))
                            await websocket.send_text("TTS_DONE:")

                    except Exception as e:
                        log.error("browser-use agent error: %s", e, exc_info=True)
                        await websocket.send_text(f"ERROR: Could not perform action — {e}")
                        if TTS_ENABLED:
                            await websocket.send_text("TTS_DONE:")
                    continue

            # --- Resolve URL from natural language ---
            await websocket.send_text("STATUS: Analyzing your request...")
            try:
                url_result = await resolve_url_from_prompt(task)
                url_mode = url_result.get("has_url", False)
                resolved_url = url_result.get("url", "") if url_mode else ""
                user_intent = url_result.get("intent", "summarize") if url_mode else ""
            except Exception as e:
                log.warning("URL resolution failed, treating as general question: %s", e)
                url_mode = False
                resolved_url = ""
                user_intent = ""

            if url_mode:
                log.info("Resolved URL: %s (intent: %s)", resolved_url, user_intent)

            # --- URL mode or general question ---
            if url_mode:
                await websocket.send_text(f"STATUS: Opening {resolved_url}...")
            else:
                await websocket.send_text("STATUS: Thinking...")

            try:
                if url_mode:
                    try:
                        page_data = await browser_session.navigate(resolved_url, language=language)
                    except Exception as e:
                        log.warning("Playwright capture failed, falling back to httpx: %s", e)
                        html = await fetch_page_text(resolved_url)
                        text = extract_text_from_html(html)
                        page_data = {
                            "screenshot": None,
                            "text": text,
                            "altTexts": [],
                            "title": "",
                            "url": resolved_url,
                        }

                    # Send screenshot to frontend
                    if page_data.get("screenshot"):
                        await websocket.send_text("SCREENSHOT:" + json.dumps({
                            "url": page_data["url"],
                            "screenshot": page_data["screenshot"],
                        }))

                    text = build_page_content(
                        page_data["text"],
                        page_data.get("altTexts", []),
                        page_data.get("title", ""),
                    )

                    if not text or len(text.split()) < 20:
                        await websocket.send_text(
                            "AGENT_RESULT: This page doesn't have enough readable text "
                            "to summarize."
                        )
                        if TTS_ENABLED:
                            await websocket.send_text("TTS_DONE:")
                        continue

                    page_context = {"url": resolved_url, "text": text}

                    # If user just wants to open/visit (not summarize), send a
                    # short confirmation instead of dumping the full page content
                    _summarize_intents = {"summarize", "read", "explain", "describe", "tell me about"}
                    if not any(kw in (user_intent or "").lower() for kw in _summarize_intents):
                        title = page_data.get("title", "") or resolved_url
                        # Translate visit confirmation for non-English languages
                        if language != "English":
                            brief = await gemini_json_call(
                                f"Translate the following message to {language}. Return ONLY the translated text, nothing else.",
                                f"I've opened **{title}**. Let me know what you'd like to do — I can read the page, navigate to a section, click a link, or answer questions about it.",
                                max_tokens=256,
                            )
                        else:
                            brief = f"I've opened **{title}**. Let me know what you'd like to do — I can read the page, navigate to a section, click a link, or answer questions about it."
                        await websocket.send_text("STREAM_START:")
                        await websocket.send_text(f"STREAM_TOKEN: {brief}")
                        await websocket.send_text(f"STREAM_END: {brief}")
                        _append_history("user", f"[User visited {resolved_url}]")
                        _append_history("model", brief)
                        if TTS_ENABLED:
                            tts_result = await synthesize_speech_chunk(brief, 0, language)
                            if tts_result:
                                await websocket.send_text("TTS_CHUNK:" + json.dumps(tts_result))
                            await websocket.send_text("TTS_DONE:")
                        continue

                    await websocket.send_text("STATUS: Summarizing content...")
                    system_instruction = build_system_prompt(is_page=True, language=language)
                    contents = build_contents(text, is_page=True, conversation_history=conversation_history, language=language)
                else:
                    system_instruction = build_system_prompt(
                        is_page=False, page_context=page_context, language=language
                    )
                    contents = build_contents(task, is_page=False, conversation_history=conversation_history, page_context=page_context, language=language)

                # Stream LLM response
                loop = asyncio.get_event_loop()
                token_queue: queue.Queue[str | None] = queue.Queue()

                def _stream_worker():
                    try:
                        for token in llm_stream(system_instruction, contents):
                            token_queue.put(token)
                    except Exception as e:
                        token_queue.put(f"__ERROR__:{e}")
                    finally:
                        token_queue.put(None)

                loop.run_in_executor(None, _stream_worker)

                full_response = ""
                await websocket.send_text("STREAM_START:")
                while True:
                    try:
                        token = await asyncio.wait_for(
                            loop.run_in_executor(None, token_queue.get), timeout=120,
                        )
                    except asyncio.TimeoutError:
                        log.warning("Stream timeout after 120s")
                        break

                    if token is None:
                        break
                    if isinstance(token, str) and token.startswith("__ERROR__:"):
                        error_msg = token[10:]
                        log.error("LLM stream error: %s", error_msg)
                        if not full_response:
                            await websocket.send_text(f"ERROR: LLM error — {error_msg}")
                        break

                    full_response += token
                    await websocket.send_text(f"STREAM_TOKEN: {token}")

                if full_response:
                    full_response = await condense_long_response(full_response, language)
                    await websocket.send_text(f"STREAM_END: {full_response}")
                    log.info("Response sent (%d chars)", len(full_response))

                    user_text = f"[User visited {resolved_url}] {user_intent}" if url_mode else task
                    _append_history("user", user_text)
                    _append_history("model", full_response)

                    if TTS_ENABLED:
                        chunks = split_into_tts_chunks(full_response)
                        # Send first chunk ASAP, then synthesize rest in parallel
                        first_result = await synthesize_speech_chunk(chunks[0], 0, language)
                        if first_result:
                            await websocket.send_text("TTS_CHUNK:" + json.dumps(first_result))
                        if len(chunks) > 1:
                            results = await asyncio.gather(*[
                                synthesize_speech_chunk(c, i, language) for i, c in enumerate(chunks[1:], 1)
                            ])
                            for r in results:
                                if r:
                                    await websocket.send_text("TTS_CHUNK:" + json.dumps(r))
                        await websocket.send_text("TTS_DONE:")
                else:
                    # No content produced — send error instead of empty bubble
                    await websocket.send_text("ERROR: Could not generate a response. Please try again.")
                    if TTS_ENABLED:
                        await websocket.send_text("TTS_DONE:")

            except httpx.HTTPStatusError as e:
                await websocket.send_text(
                    f"ERROR: Could not fetch the page — HTTP {e.response.status_code}."
                )
                if TTS_ENABLED:
                    await websocket.send_text("TTS_DONE:")
            except httpx.RequestError as e:
                await websocket.send_text(f"ERROR: Could not reach the page — {e}.")
                if TTS_ENABLED:
                    await websocket.send_text("TTS_DONE:")
            except Exception as e:
                log.error("Unexpected error: %s", e, exc_info=True)
                await websocket.send_text(f"ERROR: {e}")
                if TTS_ENABLED:
                    await websocket.send_text("TTS_DONE:")

    except Exception:
        pass
    finally:
        await browser_session.close()
        try:
            await bu_browser.close()
        except Exception:
            pass
        log.info("Client disconnected")


# ---------------------------------------------------------------------------
# Lifespan (replaces deprecated on_event)
# ---------------------------------------------------------------------------
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app_instance):
    # Startup — nothing to do
    yield
    # Shutdown — close browser
    global _browser_instance, _playwright_instance
    try:
        if _browser_instance:
            await _browser_instance.close()
    except Exception:
        pass  # Transport may already be closed on force-quit
    try:
        if _playwright_instance:
            await _playwright_instance.stop()
    except Exception:
        pass


app.router.lifespan_context = lifespan


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
