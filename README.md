# Telegram AI Chat Automation Bot

Bu loyiha Telegram profiliga ulangan Chat Automation bot orqali shaxsiy chatlarga Manus AI yordamida avtomatik javob beradi. OpenAI va Qwen/DashScope ham konfiguratsiyada qo‘llab-quvvatlanadi. Bot Python tilida yozilgan va **Vercel Python serverless webhook** sifatida ishlashga moslangan.

GitHub’dagi `manhwa_bot` repositoriyasi bu loyiha uchun faqat reference sifatida ko‘rilgan. Unga hech qanday commit, push yoki boshqa o‘zgartirish yuborilmaydi.

## Arxitektura

Vercel `api/index.py` faylini ASGI endpoint sifatida ishga tushiradi. Telegram webhook orqali kelgan `business_message` update’lari secret path va `X-Telegram-Bot-Api-Secret-Token` header orqali tekshiriladi. Dastur `business_connection_id`, `chat_id` va `can_reply` huquqini tekshiradi, Manus v2 task yaratib statusni polling orqali kutadi yoki OpenAI/Qwen’ga xabar yuboradi va javobni Telegram `sendMessage` metodi orqali profil nomidan jo‘natadi.

Vercel serverless instance’lari doimiy disk sifatida ishlatilmaydi. `DATABASE_URL` mavjud bo‘lsa, `postgres_store.py` Neon PostgreSQL’da `/rol`, suhbat tarixi va owner pause holatini saqlaydi; shu sababli instance almashtirilishi yoki yangi deployment suhbat ma’lumotlarini o‘chirmaydi. DATABASE_URL bo‘lmagan lokal rejimda JSON yoki memory fallback ishlaydi. Credentiallar va secretlar hech qachon repositoryga yozilmaydi.

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
| `ai_providers.py` | Manus v2 task adapteri, OpenAI/Qwen klientlari va fallback |
| `memory_store.py` | Fallback sifatida vaqtinchalik xotira storage’i |
| `storage.py` | Lokal ishlashda JSON suhbat storage’i |
| `pause_store.py` | Optional Upstash Redis REST orqali durable owner-pause storage’i |
| `postgres_pause_store.py` | Neon PostgreSQL orqali durable owner-pause storage’i |
| `config.py` | Environment variable konfiguratsiyasi |
| `vercel.json` | Vercel Python build va route sozlamalari |
| `.env.example` | Lokal namuna konfiguratsiyasi |

## Vercel Environment Variables

Vercel project’ning **Settings → Environment Variables** bo‘limida Production uchun quyidagi qiymatlarni kiriting. Ochiq chatga yuborilgan eski tokenlarni ishlatmang; BotFather va OpenAI panelidan yangilangan credentiallarni kiriting.

| O‘zgaruvchi | Majburiyligi | Qiymat |
|---|---:|---|
| `BOT_TOKEN` | Ha | BotFather’dan olingan yangi token |
| `WEBHOOK_SECRET` | Ha | Uzun random secret, masalan password manager yaratgan qiymat |
| `OPENAI_API_KEY` | OpenAI uchun | OpenAI API key |
| `MANUS_API_KEY` | Manus uchun | Manus API Integration’dan olingan key |
| `AI_PROVIDER` | Ha | `manus`, `openai`, `qwen` yoki `auto`; hozir `manus` |
| `MANUS_BASE_URL` | Yo‘q | `https://api.manus.ai` |
| `MANUS_AGENT_PROFILE` | Yo‘q | `manus-1.6-lite` |
| `MANUS_MAX_WAIT_SECONDS` | Yo‘q | Default `45` |
| `OPENAI_MODEL` | Yo‘q | Masalan `gpt-4o-mini` yoki hisobingizdagi boshqa model |
| `QWEN_API_KEY` | Yo‘q | Qwen ishlatilganda qo‘shiladi |
| `QWEN_MODEL` | Yo‘q | Masalan `qwen-plus` |
| `QWEN_BASE_URL` | Yo‘q | `https://dashscope-intl.aliyuncs.com/compatible-mode/v1` |
| `MAX_HISTORY_MESSAGES` | Yo‘q | Default `12` |
| `SEND_ERROR_MESSAGE` | Yo‘q | Default `true` |
| `MANUAL_REPLY_PAUSE_SECONDS` | Yo‘q | Default `1800` (30 daqiqa) |
| `DATABASE_URL` | Tavsiya qilinadi | Neon PostgreSQL connection string; Vercel cold startlarida pause holatini saqlaydi |
| `UPSTASH_REDIS_REST_URL` | Muqobil | Neon ishlatilmasa Upstash Redis REST endpointi |
| `UPSTASH_REDIS_REST_TOKEN` | Muqobil | Neon ishlatilmasa Upstash Redis REST tokeni |

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

## Admin panel

Botning shaxsiy chatida `/admin` buyrug‘i owner va faol premium userlar uchun ochiladi. Owner ID `8645314130` to‘liq panel, jumladan statistika, AI roli va 30 daqiqalik manual pause boshqaruviga ega. Premium userlar panelida statistika tugmasi bo‘lmaydi; ular shaxsiy rol va pause boshqaruvidan foydalanadi. `⏱ Pause` bo‘limidagi tugma bilan bu funksiyani istalgan payt yoqing yoki o‘chiring; tanlangan holat Neon’dagi `telegram_settings` jadvalida saqlanadi. Oddiy premium bo‘lmagan user `/admin` yoki `/rol` yuborsa, bot `Siz admin emassiz.` deb javob beradi. Admin panel tugmalari ham har bir callback’da qayta tekshiriladi.

## APK fayllarini o‘chirish

Business chatga mijoz `.apk` fayl yuborsa, bot AI javobi bermasdan `deleteBusinessMessages` orqali shu xabarni chatning ikki tomonida o‘chirishga urinadi. Buning uchun Telegram Business ulanishida botga **delete all messages** (`can_delete_all_messages`) huquqini bering. Huquq berilmagan bo‘lsa, Telegram o‘chirishni rad etadi va xato Vercel logiga yoziladi.

## 30 daqiqalik qo‘lda yozish pauzasi

Bot har bir Business chatni alohida kuzatadi. Agar admin panelda manual pause yoqilgan bo‘lsa va akkaunt egasi userga qo‘lda xabar yuborsa, shu chatda avtomatik javoblar 30 daqiqaga pauzalanadi. Funksiya admin paneldan o‘chirilsa, egasining qo‘lda yozgan xabari AI javoblarini pauzalamaydi. User 30 daqiqa ichida yana yozsa, bot javob bermaydi.
 30 daqiqa o‘tgach user qayta yozsa, bot yana avtomatik javob beradi. Egasi shu chatga yana yozsa, taymer qaytadan 30 daqiqaga boshlanadi. Botning o‘zi yuborgan xabar taymerni qayta boshlamaydi.

## Admin buyruqlari va AI roli

Buyruqlarni mijoz chatiga emas, botning o‘z shaxsiy chatiga yuboring. `/id` Telegram user ID’ingizni ko‘rsatadi. `/rol Siz muloyim, qisqa va faqat o‘zbek tilida javob beradigan yordamchisiz.` buyrug‘i keyingi Business xabarlarga qo‘llanadigan AI uslubini saqlaydi. `/rol` joriy rolni ko‘rsatadi, `/rol reset` esa standart rolga qaytaradi. `/role` inglizcha alias sifatida ham ishlaydi.

Ownerning global `/rol` huquqi faqat hardcoded `8645314130` ID’ga tegishli. Premium user `/rol` orqali faqat o‘zining shaxsiy rolini boshqaradi. `DATABASE_URL` orqali Neon PostgreSQL ulangani sababli rol va suhbat tarixi yangi deploymentdan keyin ham saqlanadi. `telegram_settings` jadvalida global AI roli, `telegram_conversations` jadvalida har bir Business chat tarixi, `telegram_owner_pauses` jadvalida esa `business_connection_id + chat_id` bo‘yicha owner pause vaqti saqlanadi. Neon vaqtincha ishlamasa, bot xatoni logga yozib, javob oqimini xavfsiz fallback bilan davom ettiradi.

## Premium subscription

Botning shaxsiy chatida `/premium` yuborilganda premium user paneli ochiladi. Oylik subscription 100 Telegram Stars turadi va `XTR` invoice orqali 30 kunlik recurring access beradi. Premium funksiyalar faqat Telegram `successful_payment` update’idan keyin ochiladi; pre-checkout bosqichi o‘zi access bermaydi. To‘lovlar Neon’dagi `telegram_star_payments` va `telegram_premium_access` jadvallarida saqlanadi.

Global `/rol` va statistika bo‘limi faqat hardcoded owner ID `8645314130` uchun ochiq. Premium user `/admin` panelidan statistikasiz foydalanadi va faqat `/rol` orqali shaxsiy AI uslubini sozlaydi.

## Provider tanlash

Manus ishlatish uchun `AI_PROVIDER=manus` va `MANUS_API_KEY` yetarli. Manus v2 tasklari asinxron bo‘lgani sabab adapter task yaratadi, statusni tekshiradi va `assistant_message` natijasini oladi. OpenAI uchun `AI_PROVIDER=openai`, Qwen uchun `AI_PROVIDER=qwen` tanlang. `auto` rejimida OpenAI, Qwen va Manus shu tartibda fallback sifatida ishlaydi.

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
