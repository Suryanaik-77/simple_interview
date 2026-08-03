#!/usr/bin/env python3
"""Test script for user quota functions."""

import database

# Test get_user_quota for a new user
print("=" * 60)
print("Testing get_user_quota for new user...")
quota = database.get_user_quota('test@example.com')
print(f'New user quota: {quota}')

# Test update_user_quota
print("\n" + "=" * 60)
print("Testing update_user_quota (25.5 minutes)...")
database.update_user_quota('test@example.com', 25.5)
quota = database.get_user_quota('test@example.com')
print(f'After 25.5 min usage: {quota}')

# Test another update
print("\n" + "=" * 60)
print("Testing update_user_quota (15 more minutes)...")
database.update_user_quota('test@example.com', 15.0)
quota = database.get_user_quota('test@example.com')
print(f'After another 15 min: {quota}')

# Test admin_set_user_quota_limit
print("\n" + "=" * 60)
print("Testing admin_set_user_quota_limit (240 minutes)...")
database.admin_set_user_quota_limit('test@example.com', 240)
quota = database.get_user_quota('test@example.com')
print(f'After setting 240 min limit: {quota}')

# Test admin_reset_user_quota
print("\n" + "=" * 60)
print("Testing admin_reset_user_quota...")
database.admin_reset_user_quota('test@example.com')
quota = database.get_user_quota('test@example.com')
print(f'After reset: {quota}')

# Test get_all_user_quotas
print("\n" + "=" * 60)
print("Testing get_all_user_quotas...")
database.update_user_quota('user1@example.com', 100)
database.update_user_quota('user2@example.com', 50)
quotas = database.get_all_user_quotas(limit=5)
print(f'All quotas ({len(quotas)} users):')
for q in quotas:
    print(f"  {q['email']}: {q['total_minutes_used']:.1f}/{q['quota_limit_minutes']:.0f} min, remaining: {q['remaining_minutes']:.1f}")

print("\n" + "=" * 60)
print("All tests completed!")
