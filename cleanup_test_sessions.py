#!/usr/bin/env python3
"""Remove test/admin sessions from the database."""

import database

def cleanup_test_sessions():
    """Delete test and admin sessions from active_sessions table."""
    if not database._db_available:
        print("Database not available")
        return

    try:
        with database.get_conn() as conn:
            with conn.cursor() as cur:
                # First, show what we're about to delete
                cur.execute("""
                    SELECT
                        session_id,
                        session_data->'resume'->>'email' as email,
                        session_data->'resume'->>'candidate_name' as name,
                        session_data->>'phase' as phase,
                        to_timestamp((session_data->>'started_at')::double precision) as started_at
                    FROM active_sessions
                    WHERE
                        session_data->'resume'->>'email' LIKE '%test%'
                        OR session_data->'resume'->>'email' LIKE '%example.com%'
                        OR session_data->'resume'->>'email' LIKE '%admin%'
                        OR session_data->'resume'->>'candidate_name' ILIKE '%test%'
                    ORDER BY started_at DESC
                """)
                rows = cur.fetchall()

                if not rows:
                    print("No test/admin sessions found.")
                    return

                print(f"\nFound {len(rows)} test/admin sessions:")
                print("-" * 80)
                for row in rows:
                    session_id, email, name, phase, started_at = row
                    print(f"{session_id[:12]}... | {email:30} | {name:20} | {phase:10} | {started_at}")

                # Delete them
                print("\n" + "=" * 80)
                print("Deleting test/admin sessions...")
                cur.execute("""
                    DELETE FROM active_sessions
                    WHERE
                        session_data->'resume'->>'email' LIKE '%test%'
                        OR session_data->'resume'->>'email' LIKE '%example.com%'
                        OR session_data->'resume'->>'email' LIKE '%admin%'
                        OR session_data->'resume'->>'candidate_name' ILIKE '%test%'
                """)
                deleted_count = cur.rowcount
                conn.commit()

                print(f"✓ Deleted {deleted_count} test/admin sessions")

                # Also clean up test quota entries
                print("\n" + "=" * 80)
                print("Cleaning up test quota entries...")
                cur.execute("""
                    DELETE FROM user_interview_quota
                    WHERE
                        email LIKE '%test%'
                        OR email LIKE '%example.com%'
                        OR email LIKE '%admin%'
                """)
                quota_deleted = cur.rowcount
                conn.commit()
                print(f"✓ Deleted {quota_deleted} test quota entries")

    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    cleanup_test_sessions()
