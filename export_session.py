import asyncio
from pyrogram import Client

async def main():
    app = Client("alfred_session")
    await app.start()
    s = await app.export_session_string()
    with open(".session_string.txt", "w", encoding="utf-8") as f:
        f.write(s)
    await app.stop()

if __name__ == "__main__":
    asyncio.run(main())
