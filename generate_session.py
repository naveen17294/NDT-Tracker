import asyncio
from telethon.sync import TelegramClient
from telethon.sessions import StringSession
from config import API_ID, API_HASH, SESSION_NAME

async def main():
    print("Reading local session file...")
    client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
    await client.start()
    session_string = StringSession.save(client.session)
    print("\n" + "="*50)
    print("YOUR TELEGRAM SESSION STRING:")
    print("="*50 + "\n")
    print(session_string)
    print("\n" + "="*50)
    print("Copy the long string above and add it to your Railway Variables as:")
    print("SESSION_STRING")

if __name__ == '__main__':
    asyncio.run(main())
