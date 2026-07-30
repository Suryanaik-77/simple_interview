#!/usr/bin/env python3
"""
Test progressive chunk evaluation with token optimization.

Tests:
1. Complete answer in first 3 chunks (50% token savings)
2. Partial answer in first 3, complete in next 3
3. Partial answer overall → navigation hint
4. No answer found → navigation hint only
"""
import sys
sys.stdout.reconfigure(encoding='utf-8')

import requests
import json

BASE_URL = "http://localhost:8100"

def test_query(question, description):
    print(f"\n{'='*70}")
    print(f"Test: {description}")
    print(f"Question: {question}")
    print('='*70)

    response = requests.post(
        f"{BASE_URL}/api/ask",
        json={"question": question, "k": 6, "allow_clarify": False}
    )

    if response.status_code == 200:
        data = response.json()
        print(f"\n✅ Answer received:")
        print(f"{data.get('answer', 'No answer')}\n")

        if data.get('partial_answer'):
            print(f"⚠️  PARTIAL ANSWER DETECTED")

        if data.get('navigate_hint'):
            print(f"💡 Navigation Hint: {data['navigate_hint']}")

        sources = data.get('sources', [])
        print(f"\n📚 Sources used: {len(sources)} sections")
        for src in sources[:3]:  # Show first 3
            print(f"  - [{src['n']}] {src['lab_name']} :: {src['heading']}")

        print(f"\n💰 Token optimization:")
        if len(sources) <= 3:
            print(f"   ✅ Used only {len(sources)} sections (first batch) - 50% token savings!")
        elif len(sources) <= 6:
            print(f"   ✅ Used {len(sources)} sections - progressive evaluation worked!")
        else:
            print(f"   ⚠️  Used {len(sources)} sections")
    else:
        print(f"❌ Error: {response.status_code}")
        print(response.text)

if __name__ == "__main__":
    # Test 1: Should find complete answer in first 3 chunks
    test_query(
        "How to run synthesis in Design Compiler?",
        "Complete answer expected (first batch)"
    )

    # Test 2: Common question that should have full answer
    test_query(
        "What is setup time and hold time?",
        "STA basics - should be in first batch"
    )

    # Test 3: Specific tool question
    test_query(
        "How to do floorplanning in ICC2?",
        "PnR Synopsys - should find in early chunks"
    )

    # Test 4: Question that might need navigation
    test_query(
        "What are all the DRC checks in Calibre?",
        "Broad question - might need navigation hint"
    )

    print("\n" + "="*70)
    print("Progressive Evaluation Test Complete!")
    print("="*70)
