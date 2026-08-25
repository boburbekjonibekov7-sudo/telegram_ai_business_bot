# Telegram AI Chat Automation Bot

Bu loyiha Telegram profiliga ulangan Chat Automation bot orqali shaxsiy chatlarga OpenAI yordamida avtomatik javob beradi. Qwen/DashScope ham konfiguratsiyada qo‘llab-quvvatlanadi. Bot Python tilida yozilgan va **Vercel Python serverless webhook** sifatida ishlashga moslangan.

GitHub’dagi `manhwa_bot` repositoriyasi bu loyiha uchun faqat reference sifatida ko‘rilgan. Unga hech qanday commit, push yoki boshqa o‘zgartirish yuborilmaydi.

## Arxitektura

Vercel `api/index.py` faylini ASGI endpoint sifatida ishga tushiradi. Telegram webhook orqali kelgan `business_message` update’lari secret path va `X-Telegram-Bot-Api-Secret-Token` header orqali tekshiriladi. Dastur `business_connection_id`, `chat_id` va `can_reply` huquqini tekshiradi, OpenAI yoki Qwen’ga xabar yuboradi va javobni Telegram `sendMessage` metodi orqali profil nomidan jo‘natadi.

Vercel serverless instance’lari doimiy disk sifatida ishlatilmaydi. Shu sababli `memory_store.py` tarixni faqat faol instance xotirasida saqlaydi; instance almashtirilsa suhbat konteksti yo‘qolishi mumkin. Doimiy suhbat tarixi zarur bo‘lsa, keyingi bosqichda Redis yoki Postgres adapterini ulash kerak. Credentiallar va secretlar hech qachon repositoryga yozilmaydi.

## Telegram talabi

Telegram’ning 2026-yilgi rasmiy hujjatlariga ko‘ra connected bots Premiumsiz foydalanuvchilarga ham mavjud. Botni profilga ulash uchun Telegram ilovasida `Settings → Chat Automation` bo‘limiga kirib, botni tanlang va unga **reply/send messages** huquqini bering. Faqat yangi chatlar, kontaktlar, kontakt bo‘lmaganlar yoki tanlangan chatlarni ulash mumkin.

Bu loyiha Bot API orqali ishlaydi. `api_id` va `api_hash` kerak emas. Kerakli Telegram credential — faqat `BOT_TOKEN`. `api_id/api_hash` faqat MTProto user-client varianti uchun kerak bo‘ladi; ushbu loyiha shaxsiy akkaunt sessiyasidan foydalanmaydi.

Bot @BotFather’da Business Mode yoki profile Chat Automation bilan ishlashga ruxsat berilgan bot sifatida sozlangan bo‘lishi kerak. Dastur Telegram API’ning `getMe` javobidagi `can_connect_to_business` maydonini long-polling rejimida tekshiradi; Vercel rejimida webhook kelishi uchun BotFather sozlamasi va profil ulanishi yetarli bo‘ladi.

## Fayllar

| Fayl | Vazifasi |
|---|---|
| `api/index.py` | Vercel ASGI webhook endpointi, path/header tekshiruvi |
| `app.py` | Telegram Business update’larini qayta ishlash va AI javob oqimi |
| `telegram_api.py` | Telegram Bot API HTTP klienti |
| `ai_providers.py` | OpenAI/Qwen OpenAI-compatible klienti va fallback |
| `memory_store.py` | Vercel uchun vaqtinchalik xotira storage’i |
| `storage.py` | Lokal ishlashda JSON suhbat storage’i |
| `config.py` | Environment variable konfiguratsiyasi |
| `vercel.json` | Vercel Python build va route sozlamalari |
| `.env.example` | Lokal namuna konfiguratsiyasi |

## Vercel Environment Variables

Vercel project’ning **Settings → Environment Variables** bo‘limida Production uchun quyidagi qiymatlarni kiriting. Ochiq chatga yuborilgan eski tokenlarni ishlatmang; BotFather va OpenAI panelidan yangilangan credentiallarni kiriting.

| O‘zgaruvchi | Majburiyligi | Qiymat |
|---|---:|---|
| `BOT_TOKEN` | Ha | BotFather’dan olingan yangi token |
| `WEBHOOK_SECRET` | Ha | Uzun random secret, masalan password manager yaratgan qiymat |
| `OPENAI_API_KEY` | Ha, OpenAI uchun | Yangilangan OpenAI API key |
| `AI_PROVIDER` | Ha | `openai`, `qwen` yoki `auto`; hozir `openai` yetarli |
| `OPENAI_MODEL` | Yo‘q | Masalan `gpt-4o-mini` yoki hisobingizdagi boshqa model |
| `QWEN_API_KEY` | Yo‘q | Qwen ishlatilganda qo‘shiladi |
| `QWEN_MODEL` | Yo‘q | Masalan `qwen-plus` |
| `QWEN_BASE_URL` | Yo‘q | `https://dashscope-intl.aliyuncs.com/compatible-mode/v1` |
| `MAX_HISTORY_MESSAGES` | Yo‘q | Default `12` |
| `SEND_ERROR_MESSAGE` | Yo‘q | Default `false` |

Vercel’ning Production URL’i avtomatik ravishda `VERCEL_PROJECT_PRODUCTION_URL` orqali olinadi. Zarur bo‘lsa `PUBLIC_BASE_URL` ni `https://your-project.vercel.app` ko‘rinishida qo‘shish mumkin. URL oxirida `/` bo‘lmasin.

## Deploy va webhook

Yangi private repository Vercel project’ga ulangach, Production deployment qiling. Production URL tayyor bo‘lgandan keyin Telegram webhook’ni yangi `WEBHOOK_SECRET` bilan quyidagicha o‘rnating:

```bash
curl -sS -X POST "https://api.telegram.org/bot<NEW_BOT_TOKEN>/setWebhook" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://<YOUR_PRODUCTION_DOMAIN>/webhook/<WEBHOOK_SECRET>",
    "secret_token": "<WEBHOOK_SECRET>",
    "allowed_updates": ["business_connection", "business_message", "edited_business_message", "deleted_business_messages"],
    "drop_pending_updates": false
  }'
```

`<NEW_BOT_TOKEN>` va `<WEBHOOK_SECRET>` shell history’ga tushmasligi uchun bu buyruqni password manager yoki xavfsiz terminal sessiyasida bajaring. Vercel URL’ni Preview deployment’dan emas, Production deployment’dan oling.

Health check uchun Production URL’ni brauzerda oching:

```text
https://<YOUR_PRODUCTION_DOMAIN>/
```

`Telegram AI bot webhook is running` javobi ko‘rinsa, function ishlayapti. Webhook holatini tekshirish:

```bash
curl -sS "https://api.telegram.org/bot<NEW_BOT_TOKEN>/getWebhookInfo"
```

## Mahalliy sinov

```bash
cd telegram_ai_business_bot
cp .env.example .env
chmod 600 .env
python3 app.py
```

Mahalliy polling testida `WEBHOOK_SECRET` kerak emas. Vercel webhook testida esa alohida public HTTPS URL va Telegram `setWebhook` kerak bo‘ladi. Kod credentiallarsiz kompilyatsiya va unit testlardan o‘tkazilgan.

## Admin buyruqlari va AI roli

Buyruqlarni mijoz chatiga emas, botning o‘z shaxsiy chatiga yuboring. `/id` Telegram user ID’ingizni ko‘rsatadi. `/rol Siz muloyim, qisqa va faqat o‘zbek tilida javob beradigan yordamchisiz.` buyrug‘i keyingi Business xabarlarga qo‘llanadigan AI uslubini saqlaydi. `/rol` joriy rolni ko‘rsatadi, `/rol reset` esa standart rolga qaytaradi. `/role` inglizcha alias sifatida ham ishlaydi.

Xavfsizlik uchun `/rol` faqat `ADMIN_USER_ID` ga mos user yoki faol Business ulanishining akkaunt egasi tomonidan bajariladi. Agar bot “faqat akkaunt egasi” desa, avval `/id` ni yuboring va Vercel Environment Variables’da `ADMIN_USER_ID` sifatida shu ID’ni kiriting, keyin yangi deployment qiling. Vercel serverless xotirasida rol hot instance davomida saqlanadi; doimiy saqlash kerak bo‘lsa, Redis yoki Postgres adapteri kerak bo‘ladi.

## Provider tanlash

Hozircha faqat OpenAI ishlatish uchun `AI_PROVIDER=openai` va yangilangan `OPENAI_API_KEY` yetarli. Keyinchalik Qwen’ni qo‘shish uchun `QWEN_API_KEY` ni Vercel Environment Variables’ga kiriting va `AI_PROVIDER=qwen` yoki `AI_PROVIDER=auto` deb o‘zgartiring. `auto` rejimida OpenAI birinchi, Qwen esa fallback sifatida ishlaydi.

API key autentifikatsiya satridir; “API key tokeni ko‘p yoki kam” degan taqqoslash qilinmaydi. Xarajat modelning input/output tokenlari bo‘yicha hisoblanadi. OpenAI tariflari modelga, Qwen/DashScope tariflari esa model va regionga bog‘liq.

## Xavfsizlik

Bot tokeni va AI key’larini `.env`, Git history, README yoki source code’ga yozmang. Ushbu chatga yuborilgan qiymatlar komprometatsiya qilingan hisoblanadi va production’da ishlatilmasligi kerak. Vercel’da secretlarni faqat Environment Variables orqali saqlang. `WEBHOOK_SECRET` Telegram webhook URL path’i va secret header uchun bir xil, uzun va taxmin qilib bo‘lmaydigan qiymat bo‘lsin.

## Rasmiy manbalar

1. [Telegram Bot API](https://core.telegram.org/bots/api) — `business_message`, `business_connection`, `business_connection_id`, `can_connect_to_business`, `BusinessBotRights` va `sendMessage`.
2. [Connected business bots](https://core.telegram.org/api/bots/connected-business-bots) — botni profilga ulash va foydalanuvchi nomidan javob berish modeli.
3. [Telegram Business](https://core.telegram.org/api/business) — connected bots Premiumsiz foydalanuvchilar uchun ham mavjudligi.
4. [Telegram Bot API changelog](https://core.telegram.org/bots/api-changelog) — Business Bots’ning Premiumsiz user accountlarni boshqarishi.
5. [Telegram Chat Automation e’loni](https://telegram.org/blog/ai-bot-revolution-11-new-features) — “Chat Automation in Profiles”.
6. [OpenAI API pricing](https://developers.openai.com/api/docs/pricing) — model va token tariflari.
7. [Alibaba Cloud Model Studio pricing](https://www.alibabacloud.com/help/en/model-studio/model-pricing) — Qwen/DashScope model tariflari.
