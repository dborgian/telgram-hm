from telethon.sync import TelegramClient
from telethon.sessions import StringSession

with TelegramClient(StringSession(), 33298523, "5bc8bc4d639ec900ee70adc016470132") as c:
    print(c.session.save())
