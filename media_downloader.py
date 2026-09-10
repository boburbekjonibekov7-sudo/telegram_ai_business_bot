"""
.down va .music buyruqlari uchun yt-dlp asosidagi yuklab olish moduli.

MUHIM: bu modul faqat doimiy ishlaydigan muhitda (masalan run.sh / VPS / server,
uzoq muddatli process) ishonchli ishlaydi. Vercel kabi serverless muhitda
funksiya bajarilish vaqti va vaqtinchalik disk hajmi cheklangani sabab katta
yoki uzun video/audio fayllar yuklanmasligi mumkin. Shu sabab bu funksiya
frontendda VIP’ga bog‘langan va xatolik holatida foydalanuvchiga aniq xabar
qaytaradi (funksiya “soxta” ishlamaydi — signal beradi).
"""
from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from dataclasses import dataclass

try:
    import yt_dlp
except ImportError:  # pragma: no cover - agar requirements o'rnatilmagan bo'lsa
    yt_dlp = None

MAX_FILE_SIZE_BYTES = 45 * 1024 * 1024  # Telegram bot API oddiy fayl yuborish limiti ~50MB


class DownloadError(RuntimeError):
    pass


@dataclass
class DownloadResult:
    path: str
    title: str
    _tmp_dir: str

    def cleanup(self) -> None:
        shutil.rmtree(self._tmp_dir, ignore_errors=True)


def _run_download(query_or_url: str, tmp_dir: str, audio_only: bool) -> tuple[str, str]:
    if yt_dlp is None:
        raise DownloadError("yt-dlp o‘rnatilmagan. `pip install yt-dlp` ni bajaring.")

    outtmpl = os.path.join(tmp_dir, "%(title).80s.%(ext)s")
    is_url = query_or_url.startswith("http://") or query_or_url.startswith("https://")
    target = query_or_url if is_url else f"ytsearch1:{query_or_url}"

    ydl_opts: dict = {
        "outtmpl": outtmpl,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "max_filesize": MAX_FILE_SIZE_BYTES,
        "format": "bestaudio/best" if audio_only else "best[filesize<45M]/best",
    }
    if audio_only:
        ydl_opts["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
        ]

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(target, download=True)
        if "entries" in info:  # ytsearch natijasi ro'yxat qaytaradi
            info = info["entries"][0]
        filename = ydl.prepare_filename(info)
        if audio_only:
            base, _ = os.path.splitext(filename)
            mp3_path = base + ".mp3"
            if os.path.exists(mp3_path):
                filename = mp3_path
        if not os.path.exists(filename):
            raise DownloadError("Fayl yuklab olingandan so‘ng topilmadi.")
        return filename, str(info.get("title") or "Yuklangan fayl")


async def download_media(query_or_url: str, audio_only: bool = False) -> DownloadResult:
    tmp_dir = tempfile.mkdtemp(prefix="tgbot_dl_")
    try:
        path, title = await asyncio.to_thread(_run_download, query_or_url, tmp_dir, audio_only)
    except DownloadError:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    except Exception as exc:  # yt-dlp ko'p turdagi xatoliklar tashlashi mumkin
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise DownloadError(str(exc)) from exc

    size = os.path.getsize(path)
    if size > MAX_FILE_SIZE_BYTES:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise DownloadError("Fayl juda katta (Telegram bot API orqali yuborib bo‘lmaydi, limit ~45MB).")

    return DownloadResult(path=path, title=title, _tmp_dir=tmp_dir)
