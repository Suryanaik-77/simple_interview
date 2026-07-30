#!/usr/bin/env python3
"""Test domain selection feature."""
import sys
sys.stdout.reconfigure(encoding='utf-8')
import requests
import json

BASE_URL = "http://localhost:8100"

test_cases = [
    {
        "question": "What is binary number system?",
        "domain": "dv",
        "expect": "Should find DV content about binary"
    },
    {
        "question": "What is binary number system?",
        "domain": "pd",
        "expect": "Should NOT find in PD (no such content)"
    },
    {
        "question": "How to do clock tree synthesis in ICC2?",
        "domain": "pd",
        "expect": "Should find PD CTS content"
    },
    {
        "question": "How to do clock tree synthesis in ICC2?",
        "domain": "dv",
        "expect": "Should NOT find in DV (no CTS content)"
    },
    {
        "question": "Explain AND gate and OR gate",
        "domain": "dv",
        "expect": "Should find DV logic gates content"
    },
    {
        "question": "What is two's complement?",
        "domain": "dv",
        "expect": "Should find DV signed numbers content"
    },
]

print("="*70)
print("DOMAIN SELECTION TEST")
print("="*70)

for i, test in enumerate(test_cases, 1):
    print(f"\n{'='*70}")
    print(f"Test {i}: {test['expect']}")
    print(f"Question: {test['question']}")
    print(f"Domain: {test['domain'].upper()}")
    print('='*70)

    response = requests.post(
        f"{BASE_URL}/api/ask",
        json={"question": test["question"], "domain": test["domain"], "allow_clarify": False}
    )

    if response.status_code == 200:
        data = response.json()

        answer = data.get('answer', '')[:200]
        sources = data.get('sources', [])
        domain_returned = data.get('domain', '')

        print(f"\n📝 Answer preview:")
        print(f"   {answer}...")

        print(f"\n📊 Results:")
        print(f"   Domain returned: {domain_returned}")
        print(f"   Sources found: {len(sources)}")

        if sources:
            print(f"   First source: {sources[0].get('lab_name' if test['domain']=='pd' else 'module_name', 'N/A')}")

        token_usage = data.get('token_usage', {})
        if token_usage:
            print(f"\n💰 Token usage:")
            print(f"   Total: {token_usage.get('total_tokens', 0)} tokens")
            print(f"   Cost: ${token_usage.get('cost_usd', 0):.6f}")

        # Validate expectations
        if test['domain'] == 'dv' and 'binary' in test['question'].lower():
            if len(sources) > 0:
                print(f"\n   ✅ PASS - Found DV content as expected")
            else:
                print(f"\n   ❌ FAIL - Should have found DV content")
        elif test['domain'] == 'pd' and 'binary' in test['question'].lower():
            if len(sources) == 0 or 'not' in answer.lower():
                print(f"\n   ✅ PASS - Correctly found no PD content for binary")
            else:
                print(f"\n   ⚠️  UNEXPECTED - Found sources in PD for binary")
    else:
        print(f"❌ HTTP Error: {response.status_code}")

print(f"\n{'='*70}")
print("Domain Selection Test Complete!")
print("="*70)
