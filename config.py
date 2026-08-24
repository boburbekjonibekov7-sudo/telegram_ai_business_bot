from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def load_dotenv(path: str = ".env") -> None:
    """Load a small .env file without overwriting existing environment variables."""
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    bot_token: str
    ai_provider: str
    openai_api_key: str
    qwen_api_key: str
    openai_base_url: str
    qwen_base_url: str
    openai_model: str
    qwen_model: str
    system_prompt: str
    data_dir: Path
    max_history_messages: int
    send_error_message: bool

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        bot_token = os.getenv("BOT_TOKEN", "").strip()
        if not bot_token:
            raise ValueError("BOT_TOKEN .env faylida ko‘rsatilmagan")

        provider = os.getenv("AI_PROVIDER", "auto").strip().lower()
        if provider not in {"auto", "openai", "qwen"}:
            raise ValueError("AI_PROVIDER faqat auto, openai yoki qwen bo‘lishi kerak")

        openai_key = os.getenv("OPENAI_API_KEY", "").strip()
        qwen_key = os.getenv("QWEN_API_KEY", os.getenv("DASHSCOPE_API_KEY", "")).strip()
        if not openai_key and not qwen_key:
            raise ValueError("OPENAI_API_KEY yoki QWEN_API_KEY dan kamida bittasi kerak")
        if provider == "openai" and not openai_key:
            raise ValueError("AI_PROVIDER=openai, ammo OPENAI_API_KEY mavjud emas")
        if provider == "qwen" and not qwen_key:
            raise ValueError("AI_PROVIDER=qwen, ammo QWEN_API_KEY/DASHSCOPE_API_KEY mavjud emas")

        data_dir = Path(os.getenv("DATA_DIR", "data")).expanduser()

        return cls(
            bot_token=bot_token,
            ai_provider=provider,
            openai_api_key=openai_key,
            qwen_api_key=qwen_key,
            openai_base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
            qwen_base_url=os.getenv(
                "QWEN_BASE_URL",
                "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
            ).rstrip("/"),
            openai_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip(),
            qwen_model=os.getenv("QWEN_MODEL", "qwen-plus").strip(),
            system_prompt=os.getenv(
                "SYSTEM_PROMPT",
                "Siz foydalanuvchining Telegram akkaunti nomidan javob beradigan yordamchisiz. "
                "Javoblarni o‘zbek tilida, tabiiy, muloyim va qisqa yozing. O‘zingizni bot deb "
                "aytmang; bilmagan ma’lumotni to‘qib chiqarmang va kerak bo‘lsa aniqlashtiruvchi savol bering.",
            ),
            data_dir=data_dir,
            max_history_messages=max(2, int(os.getenv("MAX_HISTORY_MESSAGES", "12"))),
            send_error_message=_bool_env("SEND_ERROR_MESSAGE", False),
        )
