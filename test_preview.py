import asyncio
import os
from dotenv import load_dotenv
load_dotenv()
from telethon import TelegramClient
from telethon.tl.functions.messages import GetWebPagePreviewRequest
from config import API_ID, API_HASH, SESSION_PATH, PHONE_NUMBER

async def main():
    session_file = os.path.join(SESSION_PATH, f'{PHONE_NUMBER}.session')
    client = TelegramClient(session_file, API_ID, API_HASH)
    await client.start()
    
    url = "https://amzn.to/4qi3DSS"
    print("Trying GetWebPagePreviewRequest...")
    try:
        preview = await client(GetWebPagePreviewRequest(message=url))
        print("Result:", preview)
    except Exception as e:
        print("Error:", e)
        
    print("Trying send_message to 'me'...")
    try:
        msg = await client.send_message('me', url, link_preview=True)
        print("Initial media:", msg.media)
        await asyncio.sleep(2)
        msg2 = (await client.get_messages('me', ids=[msg.id]))[0]
        print("Media after 2s:", msg2.media)
        await client.delete_messages('me', [msg.id])
    except Exception as e:
        print("Error:", e)
        
    await client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
