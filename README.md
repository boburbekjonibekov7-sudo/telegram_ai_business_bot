# Telegram AI Business Bot — clean rebuild

Bu repository eski asosiy menyu va funksiyalardan tozalangan, yangi botni bosqichma-bosqich qurish uchun minimal skeletdir.

## Hozirgi holat

Bot Telegram Bot API orqali ishlaydi va Vercel webhook sifatida deploy qilinadi. Hozircha `/start` va `/help` buyruqlariga faqat quyidagi vaqtinchalik javob qaytariladi:

> Bot qayta qurilmoqda. Yangi funksiyalar tez orada qo‘shiladi.

Inline menyu, eski `.settings`, `.add`, `.ai`, `.send`, `.down`, `.music`, VIP to‘lovlar, admin panel, avtomatik javoblar va boshqa eski biznes funksiyalari olib tashlangan.

## Muhim fayllar

| Fayl | Vazifasi |
|---|---|
| `app.py` | Minimal update handler va qayta qurish nuqtasi |
| `api/index.py` | Vercel ASGI webhook endpointi |
| `telegram_api.py` | Telegram Bot API klienti |
| `config.py` | Minimal environment konfiguratsiyasi |
| `vercel.json` | Vercel route va function sozlamalari |

## Environment Variables

- `BOT_TOKEN` — BotFather tokeni.
- `WEBHOOK_SECRET` — Vercel webhook yo‘lini va Telegram secret header’ini himoyalash uchun uzun random qiymat.
- Ixtiyoriy `WEBHOOK_PATH` — maxsus webhook yo‘li kerak bo‘lsa.

## Vercel webhook

Production deploy’dan keyin webhook’ni quyidagicha o‘rnating:

```bash
curl -sS -X POST "https://api.telegram.org/bot< BOT_TOKEN >/setWebhook" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://<YOUR_DOMAIN>/webhook/<WEBHOOK_SECRET>",
    "secret_token": "<WEBHOOK_SECRET>",
    "allowed_updates": ["message", "business_connection", "business_message"]
  }'
```

`< BOT_TOKEN >` orasidagi bo‘shliqni haqiqiy token bilan almashtiring.

## Lokal test

```bash
export BOT_TOKEN="..."
python3 app.py
```

Testlar:

```bash
python3 -m unittest discover -s tests -v
```
