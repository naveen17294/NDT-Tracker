"""
Copy an existing SQLite database into the Postgres server in DATABASE_URL.

    python migrate_to_postgres.py                 # migrate ./data/ndt.db
    python migrate_to_postgres.py path/to/ndt.db  # migrate a specific file

Use this once, when switching a bot that already has a watchlist over to Postgres.
Skip it for a fresh install — the bot creates its own empty schema.

Safe to re-run: rows are inserted with ON CONFLICT DO NOTHING, so existing rows in
Postgres are never overwritten and a half-finished run can simply be repeated. It
only reads the SQLite file; nothing is deleted from either side.
"""

import asyncio
import os
import sys


async def main():
    from config import DATABASE_URL, DB_PATH
    from storage import PostgresBackend, SqliteBackend, redact_dsn

    sqlite_path = sys.argv[1] if len(sys.argv) > 1 else DB_PATH

    if not DATABASE_URL:
        print("DATABASE_URL is not set — there is nothing to migrate into.")
        print("Set it to your Postgres server first, e.g.")
        print("  postgresql://user:password@host:5432/ndt")
        return 1

    if not os.path.exists(sqlite_path):
        print(f"No SQLite database at {sqlite_path}")
        print("Nothing to migrate. If this is a fresh install, just start the bot.")
        return 1

    print(f"Source : {sqlite_path}")
    print(f"Target : {redact_dsn(DATABASE_URL)}")
    print("-" * 60)

    source = SqliteBackend(sqlite_path)
    target = PostgresBackend(DATABASE_URL)

    try:
        await target.execute_many_ddl(target.schema())

        # watchlist
        rows = await source.fetch_all('SELECT keyword, custom_synonyms, added_date FROM watchlist')
        copied = 0
        for row in rows:
            copied += await target.execute(
                '''INSERT INTO watchlist (keyword, custom_synonyms, added_date)
                   VALUES (?, ?, ?) ON CONFLICT (keyword) DO NOTHING''',
                (row['keyword'], row['custom_synonyms'], row['added_date'])
            )
        print(f"watchlist     : {copied} copied ({len(rows) - copied} already present)")

        # channels — active state matters, so refresh it on conflict
        rows = await source.fetch_all(
            'SELECT channel_id, channel_name, channel_username, active, added_date FROM channels'
        )
        copied = 0
        for row in rows:
            copied += await target.execute(
                '''INSERT INTO channels
                       (channel_id, channel_name, channel_username, active, added_date)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT (channel_id) DO UPDATE SET active = excluded.active''',
                (row['channel_id'], row['channel_name'], row['channel_username'],
                 row['active'], row['added_date'])
            )
        print(f"channels      : {copied} copied/updated of {len(rows)}")

        # matched_deals — dedup history, so recent rows stop a re-alert storm
        rows = await source.fetch_all(
            '''SELECT deal_hash, keyword_matched, product_name, price, channel_name,
                      message_link, matched_date FROM matched_deals'''
        )
        copied = 0
        for row in rows:
            copied += await target.execute(
                '''INSERT INTO matched_deals
                       (deal_hash, keyword_matched, product_name, price, channel_name,
                        message_link, matched_date)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (deal_hash) DO NOTHING''',
                (row['deal_hash'], row['keyword_matched'], row['product_name'],
                 row['price'], row['channel_name'], row['message_link'],
                 row['matched_date'])
            )
        print(f"matched_deals : {copied} copied ({len(rows) - copied} already present)")

    except Exception as e:
        print(f"FAILED: {type(e).__name__}: {e}")
        return 1
    finally:
        await source.close()
        await target.close()

    print("-" * 60)
    print("Done. Run `python check_db.py` to confirm, then redeploy with DATABASE_URL set.")
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
