#!/usr/bin/env python3
"""Разовый вход в Telegram: печатает строку сессии для секрета TG_SESSION.

Запускается один раз на своём компьютере:
    pip install telethon
    python robot/tg_login.py

Спросит api_id и api_hash (их выдаёт https://my.telegram.org → API development tools),
номер телефона и код из Telegram. В конце напечатает длинную строку — её нужно
положить в секреты репозитория под именем TG_SESSION.

Строка сессии даёт полный доступ к аккаунту Telegram: никому её не показывайте
и не сохраняйте в файлы. Отозвать её можно в Telegram: Настройки → Устройства.
"""
from getpass import getpass

from telethon.sessions import StringSession
from telethon.sync import TelegramClient

api_id = int(input("api_id: ").strip())
api_hash = getpass("api_hash (ввод скрыт): ").strip()

with TelegramClient(StringSession(), api_id, api_hash) as client:
    print("\nВход выполнен:", client.get_me().first_name)
    print("\nСекрет TG_SESSION (скопируйте целиком):\n")
    print(client.session.save())
