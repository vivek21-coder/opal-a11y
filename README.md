# Opal: Voice-Controlled Web Accessibility Agent

Opal is a voice-controlled accessibility agent that helps visually impaired users browse, understand, and interact with any website through natural speech.

Say something like _"Open Wikipedia"_, _"Abrir Wikipedia"_ or _"विकिपीडिया खोलो"_ and Opal navigates to the page, shows it in a live viewport, reads the content aloud, and lets you keep interacting hands-free.

I started this as a side project to see how far a fully voice-driven browsing experience could go using current speech and LLM APIs. It has since grown into a small full-stack app with real-time streaming, browser automation, and support for ten languages.

## Features

- **Voice-first browsing**: Speak commands to navigate, read, and interact with any website. Recording stops automatically when you pause.
- **Live website viewport**: A real-time rendered preview of the page inside the app, driven by headless Chromium via Playwright.
- **AI page summarisation**: Screen-reader-style summaries of any webpage covering headings, content, links, and interactive elements.
- **Browser automation**: Click links, scroll, fill forms, search, and jump between page sections by voice, powered by [browser-use](https://github.com/browser-use/browser-use) with vision.
- **Streaming responses**: LLM output appears token by token as it is generated.
- **Neural text-to-speech**: Microsoft Neural voices via Edge TTS, streamed sentence by sentence so audio starts fast.
- **Speech-to-text**: Groq Whisper transcription with language-aware hints and noise filtering.
- **Ten-language support**: UI, voice I/O, LLM prompts, and page translation in English, Spanish, French, German, Chinese, Hindi, Arabic, Portuguese, Japanese, and Korean.
- **Page translation**: Non-English users see websites fully translated through a Google Translate proxy. Translation persists across scrolling, navigation, and browser actions.
- **Conversation memory**: Keeps context across messages for follow-up questions about a loaded page.
- **WCAG 2.2 Level AA**: Skip links, ARIA labels, keyboard navigation, focus management, 4.5:1 contrast ratios, 44px touch targets, and 400% zoom reflow.

## Architecture

```
┌─────────────────────┐         WebSocket            ┌──────────────────────────┐
│                     │  ◄───────────────────────►   │                          │
│   Frontend          │   STATUS / SCREENSHOT /      │   Backend (FastAPI)      │
│   React 19 + Vite   │   STREAM_* / TTS_CHUNK /     │                          │
│                     │   STT_RESULT / ERROR         │   ┌──────────────────┐   │
│   - Live viewport   │                              │   │  Gemini LLM      │   │
│   - Chat sidebar    │   CHAT_MSG / STT_AUDIO /     │   │  (streaming)     │   │
│   - Voice controls  │   SET_LANGUAGE / NEW_CHAT    │   └──────────────────┘   │
│   - TTS playback    │                              │   ┌──────────────────┐   │
│   - Language picker │                              │   │  Groq Whisper    │   │
│   - i18n (10 langs) │                              │   │  (STT)           │   │
│                     │                              │   └──────────────────┘   │
└─────────────────────┘                              │   ┌──────────────────┐   │
                                                     │   │  Edge TTS        │   │
                                                     │   │  (Neural voices) │   │
                                                     │   └──────────────────┘   │
                                                     │   ┌──────────────────┐   │
                                                     │   │  Playwright      │   │
                                                     │   │  (Chromium)      │   │
                                                     │   └──────────────────┘   │
                                                     │   ┌──────────────────┐   │
                                                     │   │  browser-use     │   │
                                                     │   │  (Agent + vision)│   │
                                                     │   └──────────────────┘   │
                                                     └──────────────────────────┘
```

## Quick Start

### Prerequisites

- **Python 3.11+**
- **Node.js 18+**
- **Google AI Studio API key**: free at [aistudio.google.com/apikey](https://aistudio.google.com/apikey)
- **Groq API key**: free at [console.groq.com/keys](https://console.groq.com/keys)

### Backend

```bash
cd backend
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install --with-deps chromium
cp .env.example .env           # Add your API keys
python main.py
```

The backend runs at `http://localhost:8080`. Health check: `GET /health`.

### Frontend

```bash
cd frontend
npm install
npm run dev
```

Opens at `http://localhost:5173`. The dev server reads `VITE_BACKEND_URL=http://localhost:8080` from `.env.development`.

### Docker Compose

Run the full stack locally with a single command:

```bash
# Create backend/.env with your API keys first
docker compose up --build
```

- **Backend:** `http://localhost:8080`
- **Frontend:** `http://localhost:3000`

> The backend container needs `shm_size: 1gb` for Chromium. That is already set in `docker-compose.yml`.

## Configuration

### Backend environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `GOOGLE_API_KEY` | none | Google AI Studio API key **(required)** |
| `GROQ_API_KEY` | none | Groq API key for Whisper STT **(required)** |
| `GEMINI_MODEL` | `gemini-2.5-flash` | Gemini model for text generation |
| `STT_MODEL` | `whisper-large-v3-turbo` | Groq Whisper model variant |
| `TTS_ENABLED` | `true` | Enable or disable neural TTS |
| `TTS_VOICE` | `en-US-AriaNeural` | Default Edge TTS voice |

### Frontend environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `VITE_BACKEND_URL` | `http://localhost:8080` | Backend URL (dev only) |
| `BACKEND_URL` | none | Backend URL injected at runtime in Docker |

## How It Works

### Voice conversation loop

1. You tap the mic button, or it auto-restarts after TTS finishes.
2. Audio is recorded via MediaRecorder with real-time silence detection.
3. Audio is resampled to 16 kHz WAV and sent to the backend as base64.
4. The backend transcribes it via Groq Whisper with a language hint.
5. The transcript is auto-submitted as a chat message.
6. The backend classifies intent (see below), processes it, and streams back the response.
7. TTS chunks stream back as base64 MP3 and play in order.
8. When TTS finishes, the mic restarts, which creates a hands-free voice loop.

### Intent classification

When a page is loaded, every message is classified by Gemini into one of:

| Intent | Description | Example |
|--------|-------------|---------|
| `content_request` | Read or summarise the current page | _"Read this page"_, _"इस पेज को पढ़ो"_ |
| `browser_action` | Interact with the page (click, scroll, type) | _"Click the first link"_, _"नीचे स्क्रॉल करो"_ |
| `new_url` | Navigate to a different website | _"Open BBC"_, _"विकिपीडिया खोलो"_ |
| `general` | General question or conversation | _"What is machine learning?"_ |

The classification is language-agnostic. It works across all ten supported languages because Gemini understands the intent regardless of language.

### Page translation

For non-English languages, pages are served through Google Translate's `translate.goog` proxy:

- `https://en.wikipedia.org/wiki/Python` becomes
- `https://en-wikipedia-org.translate.goog/wiki/Python?_x_tr_sl=auto&_x_tr_tl=es`

This translates the **entire page** in place (not an iframe), so scrolling works and links within the page stay translated. Switching language mid-session re-navigates the current page through the new language's proxy.

## Supported Languages

| Language | STT | TTS Voice | UI | Translation |
|----------|-----|-----------|-----|-------------|
| English | `en` | `en-US-AriaNeural` | Yes | n/a |
| Spanish | `es` | `es-ES-ElviraNeural` | Yes | Yes |
| French | `fr` | `fr-FR-DeniseNeural` | Yes | Yes |
| German | `de` | `de-DE-KatjaNeural` | Yes | Yes |
| Chinese | `zh` | `zh-CN-XiaoxiaoNeural` | Yes | Yes |
| Hindi | `hi` | `hi-IN-SwaraNeural` | Yes | Yes |
| Arabic | `ar` | `ar-SA-ZariyahNeural` | Yes | Yes |
| Portuguese | `pt` | `pt-BR-FranciscaNeural` | Yes | Yes |
| Japanese | `ja` | `ja-JP-NanamiNeural` | Yes | Yes |
| Korean | `ko` | `ko-KR-SunHiNeural` | Yes | Yes |

## Deploy to Google Cloud Run

### Prerequisites

- [gcloud CLI](https://cloud.google.com/sdk/docs/install) authenticated
- [Terraform](https://developer.hashicorp.com/terraform/install) >= 1.5
- Docker

### Option 1: One-command deploy

```bash
# Set API keys as environment variables (or the script will prompt you)
export TF_VAR_google_api_key=your-google-api-key
export TF_VAR_groq_api_key=your-groq-api-key

./deploy.sh
```

The script builds both Docker images, pushes them to Artifact Registry, and runs Terraform to deploy the Cloud Run services. It prints the frontend and backend URLs when it finishes.

### Option 2: Manual deploy

```bash
# 1. Configure Terraform
cd infra
cp terraform.tfvars.example terraform.tfvars
# Edit terraform.tfvars with your project ID, API keys, etc.
terraform init

# 2. Set variables
export PROJECT_ID=your-project-id
export REGION=europe-west2
export REPO=$REGION-docker.pkg.dev/$PROJECT_ID/opal-repo

# 3. Authenticate Docker
gcloud auth configure-docker $REGION-docker.pkg.dev

# 4. Build & push images
docker build --platform linux/amd64 -t $REPO/backend:latest ./backend
docker push $REPO/backend:latest

docker build --platform linux/amd64 -t $REPO/frontend:latest ./frontend
docker push $REPO/frontend:latest

# 5. Deploy
cd infra
terraform apply
```

### Cloud Run configuration

| Setting | Backend | Frontend |
|---------|---------|----------|
| CPU | 2 cores | 1 core |
| Memory | 4 GB | 256 MB |
| Execution env | Gen2 (for Chromium) | Gen1 |
| Max instances | 3 | 2 |
| Concurrency | 2 | 80 (default) |
| Request timeout | 3600s (1 hour) | 300s |
| CPU idle | No (always-on) | Yes |
| Session affinity | Yes (WebSocket) | No |

## Project Structure

```
opal-a11y/
├── backend/
│   ├── main.py              # FastAPI app: WebSocket, LLM, STT, TTS, Playwright
│   ├── requirements.txt     # Python dependencies
│   ├── .env.example         # Environment variable template
│   └── Dockerfile           # Python + Playwright + Chromium
├── frontend/
│   ├── src/
│   │   ├── App.jsx          # Main React component: UI, WebSocket, voice, TTS
│   │   ├── index.css        # Styles (WCAG 2.2 AA compliant)
│   │   ├── i18n.js          # UI translations for 10 languages
│   │   └── main.jsx         # Entry point
│   ├── index.html           # HTML template with BACKEND_URL placeholder
│   ├── entrypoint.sh        # Runtime URL injection for Docker
│   ├── package.json         # NPM dependencies
│   ├── vite.config.js       # Vite config
│   ├── .env.development     # Local dev backend URL
│   └── Dockerfile           # Multi-stage: Node build → nginx serve
├── infra/
│   ├── main.tf              # Cloud Run services, IAM, Artifact Registry
│   ├── variables.tf         # Terraform input variables
│   ├── outputs.tf           # Service URLs
│   └── terraform.tfvars.example
├── docker-compose.yml       # Local development orchestration
├── deploy.sh                # One-command GCP deployment
└── README.md
```

## Tech Stack

| Layer | Technology |
|-------|-----------|
| **Frontend** | React 19, Vite 7, Lucide Icons |
| **Backend** | FastAPI, Uvicorn, WebSockets |
| **LLM** | Google Gemini 2.5-Flash (streaming) |
| **Speech-to-Text** | Groq Whisper (large-v3-turbo) |
| **Text-to-Speech** | Edge TTS (Microsoft Neural voices) |
| **Browser Engine** | Playwright (headless Chromium) |
| **Browser Agent** | browser-use (AI-driven page interaction) |
| **Page Scraping** | httpx, BeautifulSoup4 |
| **Translation** | Google Translate proxy (translate.goog) |
| **Internationalisation** | Custom i18n module (10 languages) |
| **Infrastructure** | Terraform, Google Cloud Run, Artifact Registry |
| **Containers** | Docker (Python backend, nginx frontend) |

## WebSocket Protocol

All communication between frontend and backend uses a single WebSocket at `/ws/agent`.

### Client → Server

| Message | Format | Description |
|---------|--------|-------------|
| Chat message | `CHAT_MSG:{"text":"...", "viewport":{"width":N,"height":N}}` | User message with viewport dimensions |
| Voice audio | `STT_AUDIO:{"audio":"base64...", "mimeType":"audio/wav"}` | Recorded audio for transcription |
| Set language | `SET_LANGUAGE:Hindi` | Switch active language |
| Reset | `NEW_CHAT:` | Clear conversation and close browser |

### Server → Client

| Message | Format | Description |
|---------|--------|-------------|
| Status | `STATUS:Opening wikipedia.org...` | Processing status updates |
| Screenshot | `SCREENSHOT:{"url":"...","screenshot":"base64..."}` | Live page screenshot |
| Stream start | `STREAM_START:` | Begin streaming LLM response |
| Stream token | `STREAM_TOKEN: token text` | Individual LLM token |
| Stream end | `STREAM_END: full response` | Complete LLM response |
| TTS audio | `TTS_CHUNK:{"audio":"base64...","chunkIndex":0}` | Audio chunk for playback |
| TTS done | `TTS_DONE:` | All audio chunks sent |
| STT result | `STT_RESULT:transcribed text` | Transcription result |
| Error | `ERROR:message` | Error message |

## Accessibility

The frontend targets WCAG 2.2 Level AA:

- **Keyboard navigation**: Every control is reachable via Tab and operable with Enter, Space, Escape, and arrow keys.
- **Screen reader support**: ARIA labels, roles (`log`, `listbox`), and live regions (`aria-live="polite"`).
- **Focus management**: Visible `:focus-visible` outlines and a skip-to-content link.
- **Colour contrast**: 4.5:1 minimum for text, 3:1 for large text and UI components.
- **Touch targets**: 44px minimum for interactive elements.
- **Responsive reflow**: Works at 400% browser zoom without horizontal scrolling.
- **Semantic HTML**: Proper heading hierarchy and landmark regions (`main`, `aside`, `header`).

## Contributing

Issues and pull requests are welcome. If you want to add a language, the UI strings live in [frontend/src/i18n.js](frontend/src/i18n.js) and the voice, locale, and TTS mappings are in [backend/main.py](backend/main.py).

## Acknowledgements

Opal is built on top of a lot of excellent open-source and free-tier work, including Gemini, Groq Whisper, Edge TTS, Playwright, and browser-use.

## Author

Vivek Aggarwal

## License

Released under the [MIT License](LICENSE).
