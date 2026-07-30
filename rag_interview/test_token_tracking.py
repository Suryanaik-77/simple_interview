#!/usr/bin/env python3
"""Test token tracking and cost calculation."""
import sys
sys.stdout.reconfigure(encoding='utf-8')
import requests
import json

BASE_URL = "http://localhost:8100"

# Test queries
test_queries = [
    "How to load SRAM libraries in ICC2?",  # Should use 1-2 chunks
    "What are all the steps in PnR flow?",  # Broader, might use more chunks
    "How to do CTS?",  # Should trigger clarify OR use few chunks
]

print("="*70)
print("TOKEN TRACKING TEST")
print("="*70)

for i, question in enumerate(test_queries, 1):
    print(f"\n{'='*70}")
    print(f"Query {i}: {question}")
    print('='*70)

    response = requests.post(
        f"{BASE_URL}/api/ask",
        json={"question": question, "allow_clarify": False, "k": 6}
    )

    if response.status_code == 200:
        data = response.json()

        # Check if clarify triggered
        if data.get('clarify'):
            print(f"\n✅ CLARIFY TRIGGERED - No token tracking for clarify flow")
            continue

        token_usage = data.get('token_usage', {})

        print(f"\n📝 Answer preview:")
        print(f"   {data.get('answer', '')[:150]}...")

        print(f"\n📊 TOKEN USAGE:")
        print(f"   Embedding tokens: {token_usage.get('embedding_tokens', 0)}")
        print(f"   Verify batches: {token_usage.get('verify_batches', 0)}")
        print(f"   Verify tokens: {token_usage.get('verify_tokens', 0)}")
        print(f"   Answer tokens: {token_usage.get('answer_tokens', 0)}")
        print(f"   Total tokens: {token_usage.get('total_tokens', 0)}")

        print(f"\n💰 COST:")
        print(f"   ${token_usage.get('cost_usd', 0):.6f} USD")

        print(f"\n🔍 BATCH DETAILS:")
        for batch in token_usage.get('batches_detail', []):
            print(f"   Batch {batch['batch_num']}: {batch['input']} input, {batch['output']} output")

        sources = data.get('sources', [])
        print(f"\n📚 Sources used: {len(sources)} sections")

# Get overall statistics
print(f"\n\n{'='*70}")
print("OVERALL TOKEN STATISTICS")
print('='*70)

stats_response = requests.get(f"{BASE_URL}/api/token-stats")
if stats_response.status_code == 200:
    stats = stats_response.json()
    summary = stats.get('summary', {})

    print(f"\n📊 Summary ({stats['total_queries']} queries):")
    print(f"   Total cost: ${summary.get('total_cost_usd', 0):.6f}")
    print(f"   Avg cost/query: ${summary.get('avg_cost_per_query_usd', 0):.6f}")
    print(f"   Total tokens: {summary.get('total_tokens', 0):,}")
    print(f"   Avg tokens/query: {summary.get('avg_tokens_per_query', 0):.0f}")

    print(f"\n💰 OPTIMIZATION METRICS:")
    print(f"   {summary.get('optimization_rate', '0%')}")

    batch_dist = summary.get('batch_distribution', {})
    if batch_dist:
        print(f"\n   Batch distribution:")
        for batches, count in sorted(batch_dist.items()):
            print(f"      {batches} batch(es): {count} queries ({count/stats['total_queries']*100:.1f}%)")

print(f"\n{'='*70}")
print("Token Tracking Test Complete!")
print("="*70)
