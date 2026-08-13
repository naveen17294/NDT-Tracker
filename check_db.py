"""
Verify NDT's database connection before deploying.

    python check_db.py

Reads the same config the bot does, so whatever this reports is what the bot will do.
With DATABASE_URL set it tests that Postgres server; without it, the local SQLite file.

It creates the schema (safe to re-run — every statement is IF NOT EXISTS), writes a
probe row, reads it back, removes it, then prints the current contents. Run it once
after setting DATABASE_URL and you will know the bot can persist before you find out
the hard way from a Render log at 2am.
"""

import asyncio
import sys
import time


async def main():
    from config import DATABASE_URL, DB_PATH
    from database import Database
    from storage import redact_dsn

    print("NDT database check")
    print("=" * 60)

    if DATABASE_URL:
        print(f"Backend  : Postgres")
        print(f"Target   : {redact_dsn(DATABASE_URL)}")
        print("Data survives restarts: YES")
    else:
        print(f"Backend  : SQLite")
        print(f"Target   : {DB_PATH}")
        print("Data survives restarts: only if this path is on a persistent disk")
        print("  -> set DATABASE_URL to use an external Postgres instead")
    print("-" * 60)

    db = Database()

    try:
        print("Connecting and creating schema...")
        await db.init()
        print(f"  OK (backend={db.backend_name})")
    except Exception as e:
        print(f"  FAILED: {type(e).__name__}: {e}")
        print()
        print("Common causes:")
        print("  * Wrong host/port, or the server is not reachable from this machine")
        print("  * Postgres not listening on its external interface")
        print("    (listen_addresses in postgresql.conf must include the interface,")
        print("     and pg_hba.conf must allow your client IP)")
        print("  * Firewall blocking 5432")
        print("  * Bad username/password, or the database does not exist yet")
        print("  * SSL required but not requested — append ?sslmode=require")
        return 1

    probe_hash = f"__check_db_probe_{int(time.time())}"
    try:
        print("Writing a probe row...")
        await db.save_deal(
            deal_hash=probe_hash,
            keyword_matched='__check__',
            product_name='connectivity probe',
            price='0',
            channel_name='__check__',
        )
        seen = await db.is_deal_seen(probe_hash)
        print(f"  write+read back: {'OK' if seen else 'FAILED (row not found)'}")
        if not seen:
            return 1

        print("Removing the probe row...")
        removed = await db.backend.execute(
            'DELETE FROM matched_deals WHERE deal_hash = ?', (probe_hash,)
        )
        print(f"  removed {removed} row(s)")
    except Exception as e:
        print(f"  FAILED: {type(e).__name__}: {e}")
        return 1

    try:
        stats = await db.get_stats()
        print("-" * 60)
        print("Current contents:")
        print(f"  keywords        : {stats['watchlist_count']}")
        print(f"  channels active : {stats['active_channels']} / {stats['total_channels']}")
        print(f"  deals (24h)     : {stats['deals_24h']}")
        print(f"  deals (total)   : {stats['total_deals']}")
    except Exception as e:
        print(f"  stats FAILED: {type(e).__name__}: {e}")
        return 1
    finally:
        await db.close()

    print("=" * 60)
    print("PASS — the bot can read and write this database.")
    if not DATABASE_URL:
        print()
        print("Reminder: this is SQLite on local disk. On a host without a mounted")
        print("disk it is wiped on every restart. Set DATABASE_URL to persist.")
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
