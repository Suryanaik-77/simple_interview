# LMS Integration Guide

Base URL: `https://your-domain.com`

---

## Authentication

All requests require the header:
```
X-API-Key: <shared LMS_API_KEY>
```
The same key is used for both launch requests and callback verification.

---

## 1. Launch an Interview

**POST** `/api/lms/launch`

Content-Type: `multipart/form-data`

### Request Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | Yes | Student's full name |
| `email` | string | Yes | Student's email (primary identifier for results) |
| `domain` | string | No | Interview domain. Default: `physical_design` |
| `resume` | file | Yes | Student's resume (PDF/DOCX/TXT, max 5MB) |
| `callback_url` | string | No | URL to receive results after interview completes |
| `user_voice` | file | No | Voice reference audio for speaker verification (max 10MB) |
| `user_face` | file | No | Face photo for identity verification (max 5MB, single face) |

### Supported Domains

| Value | Label |
|-------|-------|
| `physical_design` | Physical Design |
| `analog_layout` | Analog Layout |
| `design_verification` | Design Verification |

Alias: `analog_design` maps to `analog_layout`

### Response

```json
{
  "session_id": "a1b2c3d4e5f6",
  "launch_url": "https://your-domain.com/?lms=1&token=eyJ...&session_id=a1b2c3d4e5f6",
  "resume": {
    "candidate_name": "Rahul Kumar",
    "email": "rahul@university.edu",
    "domain": "physical_design",
    "level": "experienced_junior",
    "years_experience": 2,
    "expertise": ["STA", "floorplanning", "CTS"],
    "tools": ["Innovus", "PrimeTime"]
  }
}
```

Redirect the student's browser to `launch_url`. The token expires in 30 minutes.

To get JSON response, include `Accept: application/json` header. Without it, the endpoint returns a 303 redirect directly.

### Example (cURL)

```bash
curl -X POST https://your-domain.com/api/lms/launch \
  -H "X-API-Key: your-api-key" \
  -H "Accept: application/json" \
  -F "name=Rahul Kumar" \
  -F "email=rahul@university.edu" \
  -F "domain=physical_design" \
  -F "callback_url=https://your-lms.com/api/interview-results" \
  -F "resume=@resume.pdf"
```

### Error Responses

| Status | Reason |
|--------|--------|
| 401 | Invalid or missing API key |
| 400 | Unsupported domain / cannot extract resume text / no face detected / multiple faces |
| 413 | File too large |
| 500 | Server error |

---

## 2. Callback (Interview Results)

After the interview ends and evaluation completes, a **single POST** request is sent to your `callback_url`.

### Callback Behavior

- Fires **once** per interview, after AI evaluation is complete
- Retry: **3 attempts** with exponential backoff (2s, 4s) on failure
- Timeout: 15 seconds per attempt
- Stops retrying on any response with status < 500

### Callback Headers

```
X-API-Key: <shared LMS_API_KEY>
Content-Type: application/json
```

Verify the `X-API-Key` header matches your shared secret to authenticate the callback.

### Callback Payload

```json
{
  "event": "interview_completed",
  "session_id": "a1b2c3d4e5f6",

  "student": {
    "email": "rahul@university.edu",
    "name": "Rahul Kumar",
    "domain": "physical_design",
    "level": "experienced_junior"
  },

  "result": {
    "status": "done",
    "overall_score": 7.2,
    "communication_score": 8.0,
    "recommendation": "hire",
    "verdict": "Strong candidate with solid PD fundamentals",
    "level_fit": "experienced_junior",
    "grade": "B+",
    "trajectory": "rising",
    "summary": "Candidate demonstrated strong understanding of STA and floorplanning with minor gaps in CTS optimization",
    "strengths": [
      "Strong STA knowledge",
      "Clear communication",
      "Good tool awareness"
    ],
    "weaknesses": [
      "Weak on clock tree synthesis",
      "Limited power analysis experience"
    ],
    "topic_scores": {
      "STA": 8.5,
      "floorplanning": 7.0,
      "CTS": 5.5,
      "placement": 7.5
    },
    "questions_answered": 28
  },

  "questions": [
    {
      "turn": 1,
      "question": "Can you walk me through the physical design flow?",
      "answer": "The physical design flow starts with netlist import, then floorplanning where we define die size and place macros...",
      "topic": "PD_flow",
      "difficulty": "basic",
      "is_followup": false,
      "score": 8.5,
      "feedback": "Comprehensive overview covering all major stages"
    },
    {
      "turn": 2,
      "question": "You mentioned macro placement - what factors do you consider when placing macros?",
      "answer": "For macro placement I consider data flow, pin accessibility, channel spacing for routing...",
      "topic": "floorplanning",
      "difficulty": "intermediate",
      "is_followup": true,
      "score": 7.5,
      "feedback": "Good points but missed flyline analysis and blockage planning"
    },
    {
      "turn": 3,
      "question": "Explain setup and hold time violations and how you fix them",
      "answer": "Setup violation means data arrives too late before the clock edge. We fix by reducing combinational delay...",
      "topic": "STA",
      "difficulty": "intermediate",
      "is_followup": false,
      "score": 9.0,
      "feedback": "Excellent explanation with correct fix strategies for both"
    }
  ],

  "integrity": {
    "ai_detection_flags": 0,
    "face_mismatch_count": 0,
    "tab_switch_count": 1,
    "trust_score": 95
  },

  "timestamps": {
    "started_at": 1751193600,
    "completed_at": 1751197200,
    "duration_sec": 3600
  }
}
```

---

## 3. Field Reference

### student

| Field | Type | Description |
|-------|------|-------------|
| `email` | string | Student's email (use this as primary identifier) |
| `name` | string | Student's full name |
| `domain` | string | Interview domain |
| `level` | string | Detected experience level from resume |

### result

| Field | Type | Description |
|-------|------|-------------|
| `status` | string | `done`, `skipped`, or `error` |
| `overall_score` | float | Overall score (0-10 scale) |
| `communication_score` | float | Communication quality score (0-10) |
| `recommendation` | string | `strong_hire`, `hire`, `maybe`, `no_hire` |
| `verdict` | string | One-line summary verdict |
| `level_fit` | string | Assessed level fit (may differ from resume level) |
| `grade` | string | Letter grade (A+, A, B+, B, C, D, F) |
| `trajectory` | string | Score trend: `rising`, `stable`, or `falling` |
| `summary` | string | Detailed evaluation summary paragraph |
| `strengths` | string[] | List of identified strengths |
| `weaknesses` | string[] | List of identified weaknesses |
| `topic_scores` | object | Score per topic, e.g. `{"STA": 8.5, "CTS": 5.5}` |
| `questions_answered` | int | Total questions the student answered |

### questions[]

| Field | Type | Description |
|-------|------|-------------|
| `turn` | int | Question number (1-based) |
| `question` | string | The interview question asked |
| `answer` | string | Student's verbatim answer (from speech-to-text) |
| `topic` | string | Technical topic category |
| `difficulty` | string | `basic`, `intermediate`, or `advanced` |
| `is_followup` | bool | Whether this was a follow-up to a previous question |
| `score` | float/null | Per-question score (0-10), null if not scored |
| `feedback` | string | Per-question evaluator feedback |

### integrity

| Field | Type | Description |
|-------|------|-------------|
| `ai_detection_flags` | int | Number of answers flagged as AI-generated |
| `face_mismatch_count` | int | Number of face verification failures during session |
| `tab_switch_count` | int | Number of times student switched browser tabs |
| `trust_score` | int/null | Overall integrity score (0-100), null if not computed |

### timestamps

| Field | Type | Description |
|-------|------|-------------|
| `started_at` | float | Interview start time (Unix timestamp) |
| `completed_at` | float | Evaluation completion time (Unix timestamp) |
| `duration_sec` | int | Total interview duration in seconds |

---

## 4. Your Callback Endpoint

Your endpoint should:

1. Verify the `X-API-Key` header
2. Accept `POST` with `Content-Type: application/json`
3. Return `200 OK` on success
4. Use `student.email` + `session_id` to match results to the student record

Example (Python/Flask):
```python
@app.post("/api/interview-results")
def interview_results():
    if request.headers.get("X-API-Key") != YOUR_API_KEY:
        return {"error": "unauthorized"}, 401

    data = request.json
    email = data["student"]["email"]
    score = data["result"]["overall_score"]
    status = data["result"]["status"]

    # Save to your database
    save_interview_result(email, data)

    return {"ok": True}, 200
```
