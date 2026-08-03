#!/usr/bin/env python3
"""Check voice mismatch statistics from recent interviews."""

import database
from datetime import datetime, timedelta

def check_voice_mismatches():
    """Show voice mismatch data from recent sessions."""
    if not database._db_available:
        print("Database not available")
        return

    try:
        with database.get_conn() as conn:
            with conn.cursor() as cur:
                # Get sessions from last 7 days with voice mismatch data
                cur.execute("""
                    SELECT
                        session_id,
                        session_data->'resume'->>'email' as email,
                        session_data->'resume'->>'candidate_name' as name,
                        session_data->>'phase' as phase,
                        (session_data->>'speaker_mismatch_count')::int as voice_mismatch_count,
                        session_data->'speaker_mismatches' as mismatch_details,
                        (session_data->>'turn')::int as total_turns,
                        to_timestamp((session_data->>'started_at')::double precision) as started_at,
                        updated_at
                    FROM active_sessions
                    WHERE
                        updated_at > NOW() - INTERVAL '7 days'
                        AND session_data->>'speaker_mismatch_count' IS NOT NULL
                    ORDER BY updated_at DESC
                    LIMIT 50
                """)
                rows = cur.fetchall()

                if not rows:
                    print("No sessions with voice verification data found in the last 7 days.")
                    return

                print("\n" + "=" * 100)
                print(f"VOICE MISMATCH REPORT - Last 7 Days ({len(rows)} sessions)")
                print("=" * 100)

                total_sessions = len(rows)
                sessions_with_mismatches = 0
                total_mismatch_count = 0

                for row in rows:
                    session_id, email, name, phase, mismatch_count, details, turns, started_at, updated_at = row

                    if mismatch_count and mismatch_count > 0:
                        sessions_with_mismatches += 1
                        total_mismatch_count += mismatch_count

                        print(f"\n⚠️  {session_id[:12]}... | {name[:25]:25} | {email[:30]:30}")
                        print(f"    Mismatches: {mismatch_count} | Total Turns: {turns} | Phase: {phase}")
                        print(f"    Started: {started_at} | Updated: {updated_at}")

                        # Show details if available
                        if details:
                            import json
                            mismatch_list = json.loads(details) if isinstance(details, str) else details
                            if mismatch_list and len(mismatch_list) > 0:
                                print(f"    Details: ", end="")
                                for m in mismatch_list[:3]:  # Show first 3
                                    turn = m.get('turn', '?')
                                    score = m.get('score', '?')
                                    print(f"Turn {turn} (score={score}) ", end="")
                                if len(mismatch_list) > 3:
                                    print(f"... +{len(mismatch_list)-3} more")
                                else:
                                    print()

                print("\n" + "=" * 100)
                print(f"SUMMARY:")
                print(f"  Total sessions checked: {total_sessions}")
                print(f"  Sessions with voice mismatches: {sessions_with_mismatches} ({sessions_with_mismatches*100//total_sessions if total_sessions > 0 else 0}%)")
                print(f"  Total mismatch events: {total_mismatch_count}")
                if sessions_with_mismatches > 0:
                    print(f"  Average mismatches per flagged session: {total_mismatch_count/sessions_with_mismatches:.1f}")
                print("=" * 100)

                # Also check sessions WITHOUT any mismatches
                cur.execute("""
                    SELECT COUNT(*)
                    FROM active_sessions
                    WHERE
                        updated_at > NOW() - INTERVAL '7 days'
                        AND (session_data->>'speaker_mismatch_count' IS NULL
                             OR (session_data->>'speaker_mismatch_count')::int = 0)
                        AND session_data->>'turn' IS NOT NULL
                        AND (session_data->>'turn')::int > 0
                """)
                clean_count = cur.fetchone()[0]

                print(f"\nClean sessions (no voice issues): {clean_count}")
                print(f"Total sessions (last 7 days): {total_sessions + clean_count}")

    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    check_voice_mismatches()
