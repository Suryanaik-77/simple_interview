# VLSI.AI - AI-Powered Interview Platform

## Product Documentation

**Version:** 1.0
**Last Updated:** June 2025

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Product Overview](#2-product-overview)
3. [System Architecture](#3-system-architecture)
4. [Features](#4-features)
5. [Interview Flow](#5-interview-flow)
6. [AI & LLM Integration](#6-ai--llm-integration)
7. [Anti-Cheat & Proctoring System](#7-anti-cheat--proctoring-system)
8. [Admin Panel](#8-admin-panel)
9. [LMS Integration](#9-lms-integration)
10. [Security](#10-security)
11. [Infrastructure & Deployment](#11-infrastructure--deployment)
12. [API Reference](#12-api-reference)
13. [Cost & Observability](#13-cost--observability)
14. [Supported Domains](#14-supported-domains)

---

## 1. Executive Summary

VLSI.AI is a fully automated, voice-based AI interview platform purpose-built for the VLSI semiconductor industry. It conducts real-time technical interviews with candidates using an AI interviewer persona, evaluates responses against calibrated rubrics, and generates detailed scoring reports -- eliminating the need for human interviewers in the screening round.

**Key value propositions:**
- Replaces 30-45 minute human screening interviews with a consistent AI interviewer
- Covers 3 VLSI domains across 2 experience levels (6 interview profiles)
- Real-time voice interaction with sub-second latency (STT + TTS pipeline)
- Multi-layered anti-cheat: face verification, speaker verification, gaze tracking, AI-answer detection, tab-switch monitoring
- LMS-ready with API-based session launch and score callback
- Full observability: per-call cost tracking, latency metrics, and admin review tools

---

## 2. Product Overview

### What It Does

The platform conducts a live, voice-based technical interview where:
1. A candidate uploads their resume and enters the interview room
2. An AI interviewer (with a distinct persona and voice) greets them and begins asking domain-specific questions
3. The AI adapts questions based on the candidate's resume, prior answers, and experience level
4. After 12-18 turns, the AI ends the interview and generates a structured evaluation
5. Admins and hiring managers review scores, transcripts, and anti-cheat signals via the admin panel

### Who Uses It

| User | Interface | Purpose |
|------|-----------|---------|
| **Candidates** | Web browser (desktop) | Take the voice interview |
| **Hiring Managers / Admins** | Admin panel | Review sessions, scores, transcripts, anti-cheat flags |
| **LMS Systems** | REST API | Launch interviews, receive score callbacks |

---

## 3. System Architecture

### High-Level Architecture

```
                        +--------------------+
                        |   Candidate Browser |
                        |  (voice_agent_ui)   |
                        +--------+-----------+
                                 |
                         HTTPS / WSS
                                 |
                        +--------v-----------+
                        |   FastAPI Server    |
                        |   (uvicorn, multi-  |
                        |    worker capable)  |
                        +--------+-----------+
                                 |
          +----------+-----------+-----------+----------+
          |          |           |           |          |
    +-----v----+ +--v---+ +----v----+ +----v----+ +---v----+
    |PostgreSQL | |Redis | |LLM APIs | |STT APIs | |TTS APIs|
    | (primary  | |(cache| |         | |         | |        |
    |  storage) | | +cfg)| |         | |         | |        |
    +----------+ +------+ +---------+ +---------+ +--------+
                                 |
                          +------v-------+
                          | AWS Services |
                          | Rekognition  |
                          | Bedrock      |
                          | Textract     |
                          | SSM / SM     |
                          +--------------+
```

### Tech Stack

| Layer | Technology |
|-------|------------|
| **Backend** | Python 3.11+, FastAPI, Uvicorn (ASGI) |
| **Database** | PostgreSQL (primary), Redis (cache + cross-worker config) |
| **Frontend** | Vanilla HTML/JS, Tailwind CSS, MediaPipe, TensorFlow.js |
| **LLM** | OpenAI GPT-4o-mini, AWS Bedrock (Claude, Llama, Nova), xAI Grok, Cerebras |
| **STT** | OpenAI Whisper, Deepgram Nova, Inworld STT |
| **TTS** | Deepgram Aura, Kugel, Inworld, OpenAI TTS |
| **Cloud** | AWS EC2, Rekognition, Textract, SSM Parameter Store, Secrets Manager |
| **Auth** | JWT (HS256), cookie + bearer token |

### Data Flow

```
Resume Upload -> Parse (Cerebras/LLM) -> Create Session -> Store in PostgreSQL
                                                              v
Interview Start -> LLM Greeting -> TTS Synthesis -> Audio to Browser
                                                              v
Candidate Speaks -> Browser VAD (1s pause) -> Audio Chunk -> STT API
                       v                                      v
              (30s auto-chunk mid-speech)              Transcript
                                                              v
                                              Submit Answer -> LLM generates
                                              next question -> TTS -> Browser
                                                              v
                                              (Background: AI detection,
                                               speaker verify, expected
                                               points generation)
                                                              v
                                              Interview Ends -> Evaluate
                                              via LLM -> Store scores ->
                                              LMS callback (if applicable)
```

---

## 4. Features

### 4.1 Voice-Based Interview

- Real-time voice interaction using browser microphone
- Browser-side Voice Activity Detection (VAD) with configurable silence thresholds
- 1-second pause: commits audio chunk to STT in background (non-blocking)
- 2-second total silence: finalizes the turn and submits answer
- 30-second auto-chunking for long answers (sends mid-speech, zero-gap restart)
- Accumulated transcript from multiple chunks combined before submission
- Streaming LLM response with real-time TTS synthesis (sentence-level chunking)

### 4.2 Adaptive AI Interviewer

- 6 distinct interviewer personas across domains and levels
- Each persona has a defined name, personality, speech style, and word limits
- Questions adapt to candidate's resume content (only asks about listed skills/projects)
- Three question types enforced: Project (40%), Scenario/Debug (40%), Concept (20%)
- Follow-up probing when expected answer points are missed
- Deliberate incorrect statements to test candidate's ability to push back
- Dynamic pacing: end at 12-15 turns, extend to 18 for strong candidates, cut at 10 for weak
- Returning candidate awareness: fresh questions for repeat interviews

### 4.3 Resume Parsing

- Supports PDF, DOCX, and TXT formats
- PDF extraction via pdfplumber with AWS Textract fallback for scanned documents
- Fast parsing via Cerebras (Llama 3.1-8b) with fallback to primary LLM
- Extracts: name, email, experience years, domain, skills, tools, projects, education
- 3-attempt retry with validation for robust extraction

### 4.4 Evaluation & Scoring

- Post-interview evaluation via LLM against calibrated rubrics
- Separate rubrics for junior (1-5 years) and senior (5+ years) candidates
- Per-question scoring with quality, accuracy, and quadrant classification
- Expected points comparison (generated in background during interview)
- Follow-up questions grouped with parent for combined scoring
- Output: overall score, grade, per-topic breakdown, strengths, weaknesses
- Minimum 8 answered questions required for evaluation

### 4.5 Face Verification (AWS Rekognition)

- Reference face registration during lobby (webcam capture)
- Glasses detection with user notification
- Periodic face comparison during interview against stored reference
- Configurable similarity threshold (default: 90%)
- Stored per-email for returning candidates

### 4.6 Speaker Verification (Resemblyzer)

- Voice embedding computed from first answer (256-dimensional vector)
- Subsequent answers compared against reference embedding
- Two-strike system: first mismatch is a warning, second triggers session termination
- Cosine similarity threshold: 0.75
- Minimum 3 seconds of audio required for reliable embedding

### 4.7 Gaze Tracking

- Client-side: MediaPipe Face Mesh extracts 90 facial landmark features
- Server-side: ExtraTrees classifier (sklearn) classifies gaze as left/right/straight
- Logs gaze deviations as anti-cheat signals

### 4.8 AI Answer Detection

- Dual detection: Sapling AI API + LLM cross-verification
- Runs in background thread per answer (non-blocking)
- Results stored in session for admin review

### 4.9 Browser Anti-Cheat

- Tab switch / window blur detection
- Copy/paste monitoring
- DevTools open detection
- Right-click context menu blocking
- Object detection via TensorFlow.js COCO-SSD (detects phones, books, second screens)
- Anti-cheat canary element (invisible text to detect AI page scrapers)
- All events logged with timestamps for admin review

### 4.10 Shared Review Links

- Admins can generate time-limited read-only links (default: 72 hours)
- Separate JWT with type: share -- no admin access
- Accessible without login at `/review/{token}`

---

## 5. Interview Flow

### 5.1 Candidate Journey

```
1. LOBBY (index.html)
   +-- Enter name, email, years of experience
   +-- Select domain (Analog Layout / Physical Design / Design Verification)
   +-- Upload resume (PDF/DOCX/TXT)
   +-- Resume parsed and validated
   +-- Face registration (webcam capture, if enabled)
   +-- Click "Start Interview"

2. INTERVIEW (voice_agent_ui.html)
   +-- AI generates personalized greeting (time-of-day aware)
   +-- WARM_OPENING phase (turns 1-3): Light questions to build comfort
   +-- DISCOVERY phase (turns 4-7): Explore breadth of knowledge
   +-- ADAPTIVE_DEPTH phase (turns 8+): Deep dive based on responses
   +-- Each turn:
   |   +-- Candidate speaks -> audio captured -> STT -> transcript
   |   +-- Background: speaker verification, AI detection
   |   +-- LLM generates next question (streaming)
   |   +-- TTS synthesizes response (streaming, sentence-level)
   |   +-- Background: expected points generated for scoring
   +-- Interview ends (LLM decision, time limit, or turn limit)

3. EVALUATION (automatic)
   +-- Full transcript built with expected points
   +-- LLM evaluates against level-calibrated rubric
   +-- Per-question scores + overall score + grade computed
   +-- Results persisted to PostgreSQL
   +-- LMS callback triggered (if LMS-launched session)

4. REPORT
   +-- Candidate can view their evaluation summary
```

### 5.2 Interview Ending Conditions

| Condition | Trigger | Source |
|-----------|---------|--------|
| LLM decision | AI determines interview is complete (12-18 turns) | LLM via `[END_INTERVIEW]` tag |
| Turn limit | 25 turns reached | Backend hard limit |
| Time limit | 1 hour elapsed | Backend hard limit |
| Stale session | No activity for 1 hour | Background sweeper |
| Abuse detected | Candidate uses abusive language | LLM via `[ABUSIVE]` tag |
| Speaker mismatch | Voice doesn't match reference (2 strikes) | Speaker verification |
| Manual end | Candidate clicks "End Interview" | Frontend |

---

## 6. AI & LLM Integration

### 6.1 LLM Providers

| Provider | Models | Use Case |
|----------|--------|----------|
| **OpenAI** | GPT-4o-mini (default) | Question generation, evaluation |
| **AWS Bedrock** | Claude Haiku/Sonnet/Opus, Llama 4, Amazon Nova, DeepSeek, Mistral | Alternative question generation and evaluation |
| **xAI** | Grok 4.1 (fast reasoning / non-reasoning) | Alternative LLM provider |
| **Cerebras** | Llama 3.1-8b | Fast resume parsing (low latency) |

All LLM providers are hot-swappable via the admin panel without restart.

### 6.2 STT Providers

| Provider | Models | Features |
|----------|--------|----------|
| **OpenAI** | gpt-4o-mini-transcribe, whisper-1 | Default provider |
| **Deepgram** | nova-3, nova-2 | VLSI keyword boosting (50 domain terms, boost=5) |
| **Inworld** | inworld-stt-1 | Alternative provider |

All providers pass through a centralized hallucination filter that catches common STT artifacts (e.g., "Thank you for watching", "Please subscribe").

### 6.3 TTS Providers

| Provider | Voices | Features |
|----------|--------|----------|
| **Deepgram** | aura-asteria-en (default) | Primary, low latency |
| **Kugel** | kugel-2.5 | Raw PCM -> WAV conversion |
| **Inworld** | Configurable | Alternative |
| **OpenAI** | tts-1 | Fallback |

### 6.4 Domain-Specific STT Prompts

Each domain has a custom STT prompt file (`stt_prompts/`) containing domain-specific vocabulary to improve transcription accuracy for technical VLSI terms.

---

## 7. Anti-Cheat & Proctoring System

### Feature Matrix

| Feature | Client-Side | Server-Side | Toggleable |
|---------|-------------|-------------|------------|
| Face verification | Webcam capture | AWS Rekognition comparison | Yes |
| Speaker verification | Audio capture | Resemblyzer embedding comparison | Yes |
| Gaze tracking | MediaPipe Face Mesh (90 features) | ExtraTrees classifier | Yes |
| AI answer detection | -- | Sapling API + LLM cross-check | Yes |
| Tab switch detection | Visibility API | Event logging | Yes |
| Copy/paste detection | Clipboard events | Event logging | Yes |
| Object detection | TensorFlow.js COCO-SSD | -- | Yes |
| DevTools detection | Window size heuristics | -- | Yes |
| Content canary | Hidden DOM element | -- | Always on |

All anti-cheat features are individually toggleable via the admin panel.

---

## 8. Admin Panel

### Capabilities

| Section | Features |
|---------|----------|
| **Dashboard** | Live session count, total interviews, aggregate stats |
| **Sessions** | List all sessions with scores, grades, anti-cheat flags; search/filter |
| **Session Detail** | Full transcript, per-question scores, evaluation breakdown, anti-cheat timeline, observability metrics |
| **LLM Config** | Switch question-generation and evaluation models on the fly |
| **Prompt Editor** | View and edit evaluation prompts per level; reset to defaults |
| **STT Config** | Switch STT provider and model |
| **TTS Config** | Switch TTS provider and voice; test TTS with custom text |
| **STT Playground** | Upload audio and test transcription with any provider/model |
| **Prompt Playground** | Test any prompt against any model with adjustable temperature |
| **Anti-Cheat Config** | Toggle individual anti-cheat features |
| **Voice Verification** | Enable/disable speaker verification globally |
| **Observability** | Aggregated LLM/STT/TTS costs, latency stats, per-call logs |
| **Share Links** | Generate time-limited read-only session review links |
| **Re-run Evaluation** | Re-evaluate any session with current model/prompts |

---

## 9. LMS Integration

### Launch Flow

```
LMS System                          VLSI.AI Platform
    |                                      |
    |  POST /api/lms/launch                |
    |  X-API-Key: <lms_api_key>            |
    |  {name, email, domain,               |
    |   resume_url/resume_base64,          |
    |   voice, user_face}                  |
    |------------------------------------->|
    |                                      |  Parse resume
    |                                      |  Create session
    |                                      |  Store face reference
    |  {session_id, interview_url}         |
    |<-------------------------------------|
    |                                      |
    |  Candidate opens interview_url       |
    |  (signed JWT, 30-min expiry)         |
    |------------------------------------->|
    |                                      |
    |        ... interview happens ...     |
    |                                      |
    |  POST <lms_callback_url>             |
    |  {session_id, candidate_name,        |
    |   candidate_email, domain, level,    |
    |   overall_score, grade, status,      |
    |   per_question scores, strengths,    |
    |   weaknesses, topics}               |
    |<-------------------------------------|
```

### LMS Launch Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `name` | string | Yes | Candidate full name |
| `email` | string | Yes | Candidate email |
| `domain` | string | Yes | Interview domain |
| `resume_url` or `resume_base64` | string | Yes (one of) | Resume file |
| `voice` | string | No | TTS voice override |
| `callback_url` | string | No | Score delivery URL |
| `user_face` | string | No | Base64 face reference image |
| `level` | string | No | Override auto-detected level |
| `years_experience` | number | No | Override years |

---

## 10. Security

### Authentication & Authorization

| Mechanism | Scope | Details |
|-----------|-------|---------|
| JWT (HS256) | Admin panel | 8-hour expiry, bearer + cookie support |
| JWT (HS256) | LMS launch | 30-minute expiry, single-use signed URL |
| JWT (HS256) | Share links | 72-hour expiry, read-only access |
| API Key | LMS API | `X-API-Key` header validation |
| Admin credentials | Login | Username/password from secrets store |

### Secrets Management

Secrets are never hardcoded. The `secrets_proxy` module abstracts secret retrieval with three backends:

| Backend | Environment | Source |
|---------|-------------|--------|
| `env` | Development | `.env` file / environment variables |
| `ssm` | Production | AWS SSM Parameter Store (SecureString) |
| `secretsmanager` | Production | AWS Secrets Manager |

Backend selection is automatic via the `SECRETS_BACKEND` environment variable.

### Data Protection

- Session data stored as JSONB in PostgreSQL (encrypted at rest via AWS)
- Face reference images stored as BYTEA in PostgreSQL
- Redis cache has 2-hour TTL; invalidated on sensitive updates
- No candidate PII in application logs
- Share links are time-limited and read-only

---

## 11. Infrastructure & Deployment

### Current Deployment

| Component | Details |
|-----------|---------|
| **Server** | AWS EC2 instance |
| **Application** | Uvicorn (multi-worker capable) |
| **Database** | PostgreSQL (connection pool: 1-10) |
| **Cache** | Redis |
| **Deployment method** | Git push -> git pull on EC2 -> restart |

### Database Schema

12 tables managing the complete interview lifecycle:

| Table | Purpose |
|-------|---------|
| `candidates` | Candidate profiles (name, domain, level, skills, tools) |
| `sessions` | Completed session metadata and scores |
| `turns` | Per-turn question, answer, duration, word count |
| `evaluations` | Per-turn scores, quality, accuracy, expected/missing points |
| `behavioral_signals` | Per-turn filler rate, pause times, behavioral flags |
| `reports` | Aggregate session scores by category |
| `expert_reviews` | Human reviewer overlays (for calibration) |
| `llm_calls` | Per-call LLM/STT/TTS cost and latency logging |
| `active_sessions` | Live session state (JSONB blob) |
| `app_config` | Runtime configuration (shared across workers) |
| `candidate_history` | Per-email session history for returning candidates |
| `face_references` | Stored face images for verification |

### Background Processes

| Process | Interval | Purpose |
|---------|----------|---------|
| Evaluation sweeper | Every 5 minutes | Catch sessions that missed foreground evaluation |
| Stale session sweeper | Continuous | End sessions inactive > 1 hour |
| Runtime config sync | On startup | Sync admin config across workers |

---

## 12. API Reference

### Candidate-Facing Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/` | Interview lobby page |
| `GET` | `/interview` | Interview room page |
| `GET` | `/health` | Health check |
| `GET` | `/api/lobby-config` | Get lobby feature flags |
| `GET` | `/api/domains` | List available interview domains |
| `GET` | `/api/tts-status` | Check if TTS is enabled |
| `POST` | `/api/parse-resume` | Upload and parse resume |
| `POST` | `/api/create-session` | Create new interview session |
| `POST` | `/api/start-interview` | Start interview, get greeting |
| `POST` | `/api/transcribe` | Transcribe audio chunk |
| `POST` | `/api/submit-answer` | Submit answer (non-streaming) |
| `POST` | `/api/stream-answer` | Submit answer (streaming SSE) |
| `POST` | `/api/end-session` | End session manually |
| `GET` | `/api/get-session` | Get session metadata |
| `POST` | `/api/generate-report` | Get evaluation report |
| `POST` | `/api/anticheat-event` | Log anti-cheat event |
| `GET` | `/api/anticheat-settings` | Get anti-cheat feature states |
| `POST` | `/api/anticheat/gaze` | Classify gaze direction |
| `GET` | `/api/face/check` | Check face reference exists |
| `POST` | `/api/face/register` | Register face reference |
| `POST` | `/api/face/detect-glasses` | Detect glasses |
| `POST` | `/api/face/compare` | Compare face against reference |
| `WS` | `/ws/audio` | WebSocket audio stream |

### Admin Endpoints (JWT Required)

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/admin` | Admin panel page |
| `POST` | `/api/auth/login` | Admin login |
| `POST` | `/api/auth/logout` | Admin logout |
| `GET` | `/api/auth/me` | Validate current session |
| `GET` | `/api/admin/sessions` | List all sessions |
| `GET` | `/api/admin/session/{sid}` | Session detail |
| `POST` | `/api/admin/rerun-eval/{sid}` | Re-run evaluation |
| `GET/POST` | `/api/admin/llm-config` | Get/set LLM models |
| `GET/POST` | `/api/admin/llm-prompts` | Get/set evaluation prompts |
| `GET` | `/api/admin/qgen-prompt` | Get interviewer prompt |
| `GET` | `/api/admin/interview-prompt` | Get raw prompt file |
| `GET/POST` | `/api/admin/stt-config` | Get/set STT config |
| `POST` | `/api/admin/stt-test` | STT playground |
| `GET/POST` | `/api/admin/voice-verification` | Get/toggle speaker verification |
| `POST` | `/api/admin/set-interview-voice` | Set TTS voice |
| `POST` | `/api/admin/test-tts` | TTS playground |
| `POST` | `/api/admin/prompt-playground` | Prompt playground |
| `GET/POST` | `/api/admin/anticheat-config` | Get/toggle anti-cheat features |
| `POST` | `/api/toggle-tts` | Toggle TTS globally |
| `POST` | `/api/admin/share-link` | Generate share link |
| `GET` | `/api/shared/session/{token}` | Get shared session |
| `GET` | `/api/observability/summary` | Cost/latency summary |
| `GET` | `/api/observability/logs` | Raw observability logs |

### LMS Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/lms/launch` | Pre-create session from LMS |

---

## 13. Cost & Observability

### Per-Call Tracking

Every LLM, STT, and TTS call is logged with:
- Model/provider used
- Input/output token counts (LLM) or audio duration (STT) or character count (TTS)
- Latency in milliseconds
- Computed USD cost
- Success/error status

### Cost Estimation Models

| Service | Pricing Basis |
|---------|--------------|
| LLM | Per-token (input + output), model-specific rates |
| STT | Per-minute of audio, model-specific rates |
| TTS | Per-character, provider-specific rates |

### Admin Observability Views

- **Summary**: Aggregate cost by step (question generation, evaluation, STT, TTS), average latency, total calls
- **Logs**: Raw per-call breakdown filterable by session, step, model, and time range

---

## 14. Supported Domains

### Interview Domains

| Domain | Junior Prompt | Senior Prompt | STT Prompt |
|--------|--------------|---------------|------------|
| **Analog Layout** | Yes | Yes | Yes |
| **Physical Design** | Yes | Yes | Yes |
| **Design Verification** | Yes | Yes | Yes |

### Experience Levels

| Level | Years | Calibration |
|-------|-------|-------------|
| Experienced Junior | 1-3 years | Expects hands-on tool usage, real debugging stories, ownership of block/flow |
| Experienced Senior | 3+ years | Expects tradeoff reasoning, architecture decisions, cross-domain awareness, war stories |

Each level has distinct interviewer personas, question depth expectations, evaluation rubrics, and scoring calibration.

---

## File Structure

```
simple_interview/
+-- main.py                    # Backend: FastAPI app, all endpoints (4,100 lines)
+-- config.py                  # Configuration, AI client init, domain constants
+-- database.py                # PostgreSQL operations, connection pool
+-- redis_cache.py             # Redis session cache, cross-worker config
+-- secrets_proxy.py           # Secrets abstraction (env / SSM / Secrets Manager)
+-- schema.sql                 # PostgreSQL schema (12 tables)
+-- requirements.txt           # Python dependencies
+-- loadtest.py                # Load testing utility
+-- .env                       # Local environment variables
|
+-- prompts/                   # Live interviewer system prompts
|   +-- experienced_junior_analog_layout.md
|   +-- experienced_junior_design_verification.md
|   +-- experienced_junior_physical_design.md
|   +-- experienced_senior_analog_layout.md
|   +-- experienced_senior_design_verification.md
|   +-- experienced_senior_physical_design.md
|
+-- eval_prompts/              # Post-interview evaluation rubrics
|   +-- experienced_junior.md
|   +-- experienced_senior.md
|
+-- stt_prompts/               # Domain-specific STT vocabulary
|   +-- analog_layout.txt
|   +-- design_verification.txt
|   +-- physical_design.txt
|
+-- models/                    # ML models
|   +-- gaze_ensemble_model.pkl    # Gaze direction classifier
|   +-- spkrec-ecapa/              # Speaker recognition model
|
+-- templates/                 # Frontend HTML
    +-- index.html             # Candidate lobby (1,364 lines)
    +-- voice_agent_ui.html    # Interview room (3,022 lines)
    +-- admin.html             # Admin panel (3,396 lines)
    +-- shared_review.html     # Read-only review page
    +-- lms_test.html          # LMS integration test page
    +-- speaker_test.html      # Speaker verification test page
```
