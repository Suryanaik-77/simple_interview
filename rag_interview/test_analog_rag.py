#!/usr/bin/env python3
"""Comprehensive test for Analog Layout RAG."""
import sys
sys.stdout.reconfigure(encoding='utf-8')
import requests
import json

BASE_URL = "http://localhost:8100"

# Test queries covering all 5 Analog topics
test_queries = [
    # Topic2: Linux Command Line Tools
    "What are common Linux commands for file operations?",
    "How to use grep command?",

    # Topic3: Basic Electronics
    "What is Ohm's law?",
    "Explain current voltage and resistance",
    "What are passive and active components?",

    # Topic4: Wafer Process
    "What is wafer fabrication process?",
    "Explain photolithography",
    "What are the steps in IC manufacturing?",

    # Topic6: Transistors
    "What is MOSFET?",
    "Explain NMOS and PMOS transistors",
    "What is threshold voltage in transistor?",

    # Topic9: Power Dissipation
    "What is power dissipation in CMOS?",
    "Explain static and dynamic power",
    "How to reduce power consumption in circuits?",
]

print("="*70)
print("ANALOG LAYOUT RAG - COMPREHENSIVE TEST")
print("Testing all 5 topics")
print("="*70)

results_summary = {
    "total": 0,
    "success": 0,
    "failed": 0,
    "total_cost": 0.0,
    "total_tokens": 0,
    "topics_found": set()
}

for i, question in enumerate(test_queries, 1):
    print(f"\n{'='*70}")
    print(f"Query {i}/{len(test_queries)}: {question}")
    print('='*70)

    response = requests.post(
        f"{BASE_URL}/api/ask",
        json={"question": question, "domain": "analog", "allow_clarify": False, "k": 6}
    )

    results_summary["total"] += 1

    if response.status_code == 200:
        data = response.json()

        answer = data.get('answer', '')
        sources = data.get('sources', [])
        token_usage = data.get('token_usage', {})

        print(f"\n📝 Answer preview (first 200 chars):")
        print(f"   {answer[:200]}...")

        print(f"\n📊 Results:")
        print(f"   Sources found: {len(sources)}")

        if sources:
            results_summary["success"] += 1
            for j, src in enumerate(sources[:3], 1):
                topic = src.get('lab_name', 'Unknown')  # Using lab_name for compatibility
                results_summary["topics_found"].add(topic)
                print(f"   [{j}] Topic: {topic}")
                print(f"       Section: {src.get('heading', 'N/A')}")
                print(f"       Score: {src.get('score', 0):.3f}")
        else:
            results_summary["failed"] += 1
            print(f"   ⚠️  No sources found")

        if token_usage:
            cost = token_usage.get('cost_usd', 0)
            tokens = token_usage.get('total_tokens', 0)
            results_summary["total_cost"] += cost
            results_summary["total_tokens"] += tokens

            print(f"\n💰 Token usage:")
            print(f"   Tokens: {tokens}")
            print(f"   Cost: ${cost:.6f}")
            print(f"   Verify batches: {token_usage.get('verify_batches', 0)}")

        if len(sources) > 0:
            print(f"\n   ✅ SUCCESS")
        else:
            print(f"\n   ❌ NO CONTENT FOUND")
    else:
        results_summary["failed"] += 1
        print(f"❌ HTTP Error: {response.status_code}")
        print(response.text[:300])

print(f"\n\n{'='*70}")
print("ANALOG RAG TEST SUMMARY")
print('='*70)
print(f"Total queries: {results_summary['total']}")
print(f"Successful: {results_summary['success']} ({results_summary['success']/results_summary['total']*100:.1f}%)")
print(f"Failed: {results_summary['failed']}")
print(f"\nTopics covered: {len(results_summary['topics_found'])}")
for topic in sorted(results_summary['topics_found']):
    print(f"  - {topic}")
print(f"\nTotal cost: ${results_summary['total_cost']:.6f}")
print(f"Total tokens: {results_summary['total_tokens']:,}")
print(f"Avg cost/query: ${results_summary['total_cost']/results_summary['total']:.6f}")
print(f"Avg tokens/query: {results_summary['total_tokens']/results_summary['total']:.0f}")
print('='*70)
