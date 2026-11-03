"""調査用：投票サイトにログインして、画面の部品を探す。購入はしない（「投票」系のボタンは押さない）。

  docker compose exec -T app python -m tools.explore spat4                       # ログイン後の画面で「映像」などを探す
  docker compose exec -T app python -m tools.explore spat4 --click "レース映像" --click "盛岡"
  docker compose exec -T app python -m tools.explore spat4 --all                 # 部品を全部表示

各画面のURL、見つかった部品、動画らしい通信（m3u8・mp4 など）を表示する。画面の画像は data/shots/ に残る。
入力欄の値（暗証番号など）は表示しない。
"""
from __future__ import annotations

import argparse
import asyncio
import re

from app.bots.engine import SiteBot
from app.config import config
from app.scheduler import credentials

FIND = re.compile(r"映像|動画|LIVE|ライブ|リプレイ|レプレイ|ムービー|movie|video|stream", re.I)
MEDIA = re.compile(r"\.m3u8|\.mp4|\.flv|/hls/|/live/|stream|movie|video", re.I)
NEVER = re.compile(r"投票|購入|入金|出金|精算")  # 押さない


async def show(bot: SiteBot, tag: str, all_parts: bool) -> None:
    print(f"==== {tag}")
    for p in bot.page.context.pages:
        print("ページ:", p.url)
    parts = await bot.elements(limit=1500)
    hits = [x for x in parts.splitlines() if all_parts or FIND.search(x)]
    print("\n".join(hits) or "（該当なし）")
    await bot.screenshot(f"explore-{tag}")


async def click_text(bot: SiteBot, text: str) -> bool:
    """どのページ・どの枠（frame）にあっても、その文字の部品を押す。"""
    for page in reversed(bot.page.context.pages):
        for frame in page.frames:
            loc = frame.get_by_text(text, exact=False)
            try:
                if await loc.count():
                    await loc.first.click(timeout=8000)
                    bot.page = bot.page.context.pages[-1]
                    return True
            except Exception:
                continue
    return False


async def main_async(a) -> None:
    (config.data_dir / "shots").mkdir(parents=True, exist_ok=True)
    media: list[str] = []
    async with SiteBot(a.site, credentials(a.site)) as bot:
        bot.page.context.on("request", lambda r: media.append(r.url) if MEDIA.search(r.url) else None)
        await bot.login()
        await show(bot, "login", a.all)
        for i, text in enumerate(a.click):
            if NEVER.search(text):
                print(f"「{text}」は購入などにつながるので押しません")
                continue
            if not await click_text(bot, text):
                print(f"「{text}」を押せませんでした")
                continue
            await asyncio.sleep(3)
            await show(bot, f"click{i + 1}", a.all)
    print("==== 動画らしい通信")
    print("\n".join(dict.fromkeys(media)) or "（なし）")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("site", choices=["ipat", "spat4"])
    ap.add_argument("--click", action="append", default=[], help="押す文字（順番に）")
    ap.add_argument("--all", action="store_true", help="部品を全部表示する")
    asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    main()
