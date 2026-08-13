#!/usr/bin/env python3
"""Delete ended interview sessions with fewer than 6 questions answered.

Only touches sessions with phase == "ended" -- in-progress sessions are left
alone even if they currently have fewer than 6 questions.
"""

import database

MIN_QUESTIONS = 6


def cleanup_short_sessions():
    if not database._db_available:
        print("Database not available")
        return

    try:
        with database.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        session_id,
                        session_data->'resume'->>'candidate_name' as name,
                        session_data->>'phase' as phase,
                        COALESCE((session_data->>'turn')::int, 0) as turn,
                        to_timestamp((session_data->>'started_at')::double precision) as started_at
                    FROM active_sessions
                    WHERE session_data->>'phase' = 'ended'
                      AND COALESCE((session_data->>'turn')::int, 0) < %s
                    ORDER BY started_at DESC
                    """,
                    (MIN_QUESTIONS,),
                )
                rows = cur.fetchall()

                if not rows:
                    print("No ended sessions with fewer than "
                          f"{MIN_QUESTIONS} questions found.")
                    return

                print(f"\nFound {len(rows)} ended session(s) with < {MIN_QUESTIONS} questions:")
                print("-" * 80)
                for row in rows:
                    session_id, name, phase, turn, started_at = row
                    print(f"{session_id[:12]}... | {name or '?':20} | "
                          f"{phase:10} | turn={turn} | {started_at}")

                print("\n" + "=" * 80)
                print("Deleting...")
                cur.execute(
                    """
                    DELETE FROM active_sessions
                    WHERE session_data->>'phase' = 'ended'
                      AND COALESCE((session_data->>'turn')::int, 0) < %s
                    """,
                    (MIN_QUESTIONS,),
                )
                deleted_count = cur.rowcount
                conn.commit()
                print(f"Deleted {deleted_count} session(s).")

    except Exception as e:
        print(f"Error: {e}")


if __name__ == "__main__":
    database.init_db()
    cleanup_short_sessions()
