#!/usr/bin/env python3
"""Test the question classifier"""
import os
from dotenv import load_dotenv
from openai import OpenAI
import json

load_dotenv()

DEEPSEEK_BASE_URL = "https://api.deepseek.com"
CHAT_MODEL = "deepseek-v4-flash"

_client = OpenAI(
    api_key=os.getenv("DEEPSEEK_API_KEY"),
    base_url=DEEPSEEK_BASE_URL,
    timeout=60.0,
)

def _classify_question(question: str) -> dict:
    """
    Classify if question is platform-related, domain-related, or both.
    """
    prompt = f"""You are a question classifier for SemiconLabs platform.

Classify this question into one of three categories:

1. **PLATFORM** - Questions about the SemiconLabs platform itself:
   - Dashboard, navigation, UI (Home, Skills, Journey, Profile)
   - Plans & subscriptions (Basic vs Pro, upgrades, renewals)
   - Account management (password reset, profile updates)
   - Certificates (download, LinkedIn sharing)
   - Labs platform features (provisioning, DCV login, data backup)
   - Enrollment, competencies, quizzes

2. **DOMAIN** - Technical questions about VLSI/semiconductor content:
   - Physical Design (PD): synthesis, clock tree, placement, routing, STA, PV, LEC
   - Design Verification (DV): binary numbers, gates, boolean logic, Karnaugh maps
   - Analog Layout: MOSFET, transistors, wafer fabrication, power dissipation
   - EDA tools usage: ICC2, Innovus, PrimeTime, Calibre commands and flows

3. **BOTH** - Questions that could involve both platform AND domain knowledge

Question: "{question}"

Respond in JSON format:
{{
    "category": "PLATFORM" | "DOMAIN" | "BOTH",
    "reasoning": "brief explanation"
}}"""

    try:
        response = _client.chat.completions.create(
            model=CHAT_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=150
        )

        result_text = response.choices[0].message.content.strip()
        result = json.loads(result_text)

        category = result.get("category", "DOMAIN")
        reasoning = result.get("reasoning", "")

        return {
            "search_platform": category in ("PLATFORM", "BOTH"),
            "search_domain": category in ("DOMAIN", "BOTH"),
            "reasoning": reasoning,
            "category": category
        }
    except Exception as e:
        return {
            "search_platform": False,
            "search_domain": True,
            "reasoning": f"Classifier error: {e}, defaulting to domain search",
            "category": "DOMAIN"
        }


# Test cases
test_questions = [
    # PLATFORM questions
    "How do I download my certificate?",
    "What is the difference between Basic and Pro plans?",
    "How do I reset my password?",
    "How long does lab provisioning take?",
    "Can I upgrade from Basic to Pro?",

    # DOMAIN questions
    "How to do clock tree synthesis in ICC2?",
    "What is binary number system?",
    "Explain MOSFET operation",
    "What command loads technology libraries?",

    # BOTH questions (if any)
    "What labs are available in the PD skill?",
]

print("="*70)
print("QUESTION CLASSIFIER TEST")
print("="*70)

for question in test_questions:
    result = _classify_question(question)
    print(f"\nQuestion: {question}")
    print(f"Category: {result['category']}")
    print(f"Search Platform: {result['search_platform']}")
    print(f"Search Domain: {result['search_domain']}")
    print(f"Reasoning: {result['reasoning']}")
