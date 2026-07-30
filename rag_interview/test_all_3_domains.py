#!/usr/bin/env python3
"""Test all 3 domain RAGs."""
import sys
sys.stdout.reconfigure(encoding='utf-8')
import requests
import json

BASE_URL = "http://localhost:8100"

test_cases = [
    # PD domain tests
    {
        "domain": "pd",
        "question": "How to do clock tree synthesis in ICC2?",
        "expect": "Should find PD CTS content"
    },
    # DV domain tests
    {
        "domain": "dv",
        "question": "What is binary number system?",
        "expect": "Should find DV number systems content"
    },
    {
        "domain": "dv",
        "question": "Explain AND gate and OR gate",
        "expect": "Should find DV logic gates"
    },
    {
        "domain": "dv",
        "question": "What is two's complement?",
        "expect": "Should find DV signed numbers"
    },
    # Analog domain tests
    {
        "domain": "analog",
        "question": "What is MOSFET?",
        "expect": "Should find Analog transistor content"
    },
    {
        "domain": "analog",
        "question": "Explain wafer fabrication process",
        "expect": "Should find Analog wafer process"
    },
    {
        "domain": "analog",
        "question": "What is power dissipation in CMOS?",
        "expect": "Should find Analog power content"
    },
]

print("="*70)
print("ALL 3 DOMAINS TEST - PD, DV, ANALOG")
print("="*70)

for i, test in enumerate(test_cases, 1):
    print(f"\n{'='*70}")
    print(f"Test {i}: {test['expect']}")
    print(f"Domain: {test['domain'].upper()}")
    print(f"Question: {test['question']}")
    print('='*70)

    response = requests.post(
        f"{BASE_URL}/api/ask",
        json={"question": test["question"], "domain": test["domain"], "allow_clarify": False}
    )

    if response.status_code == 200:
        data = response.json()

        answer = data.get('answer', '')[:250]
        sources = data.get('sources', [])
        domain_returned = data.get('domain', '')
        token_usage = data.get('token_usage', {})

        print(f"\n📝 Answer preview:")
        print(f"   {answer}...")

        print(f"\n📊 Results:")
        print(f"   Domain: {domain_returned}")
        print(f"   Sources: {len(sources)}")

        if sources:
            first_source = sources[0]
            if test['domain'] == 'pd':
                print(f"   Lab: {first_source.get('lab_name', 'N/A')}")
            elif test['domain'] == 'dv':
                print(f"   Module: {first_source.get('module_name', first_source.get('lab_name', 'N/A'))}")
            elif test['domain'] == 'analog':
                print(f"   Topic: {first_source.get('topic_name', first_source.get('lab_name', 'N/A'))}")

        if token_usage:
            print(f"\n💰 Token usage:")
            print(f"   Total: {token_usage.get('total_tokens', 0)} tokens")
            print(f"   Cost: ${token_usage.get('cost_usd', 0):.6f}")
            print(f"   Verify batches: {token_usage.get('verify_batches', 0)}")

        if len(sources) > 0:
            print(f"\n   ✅ PASS - Found content in {domain_returned.upper()}")
        else:
            print(f"\n   ❌ FAIL - No content found")
    else:
        print(f"❌ HTTP Error: {response.status_code}")
        print(response.text[:200])

print(f"\n{'='*70}")
print("All 3 Domains Test Complete!")
print("="*70)
