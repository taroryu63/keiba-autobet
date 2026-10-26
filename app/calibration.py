"""接続テスト。投票サイトにログインして、購入直前まで1段階ずつ試し、各段階の画面を残す。購入はしない。"""
from __future__ import annotations

from .bots.engine import SiteBot

STAGES = [
    ("login", "ログイン"),
    ("time", "発走時刻の読み取り"),
    ("race", "レースを開く"),
    ("odds", "単勝オッズの読み取り"),
    ("ticket", "馬券の入力（複勝100円）"),
    ("confirm", "確認画面（合計金額）"),
]


async def run_check(bot: SiteBot, venue: str, race_no: int, horse: int) -> list[dict]:
    """各段階の結果 [{"stage", "label", "ok", "info", "shot"}] を返す。失敗した段階で止まる。"""
    vars = {**bot.credentials, "venue": venue, "race_no": race_no, "num": horse, "bet_type": "複勝",
            "method_label": "通常", "units": 1, "amount": 100, "total": 100, "total_comma": "100",
            "points": 1, "combo": str(horse)}
    calls = {
        "login": lambda: bot.login(),
        "time": lambda: bot.read_post_time(venue, race_no),
        "race": lambda: bot.run("open_race", vars),
        "odds": lambda: bot.read_numbers("read_odds", vars, ("odds",)),
        "ticket": lambda: bot.run("ticket", vars, [(1, horse)]),
        "confirm": lambda: bot.run("confirm", vars),
    }
    out: list[dict] = []
    async with bot:
        for stage, label in STAGES:
            try:
                got = await calls[stage]()
                info = f"読み取り: {got}" if got else ""
                ok = True
            except Exception as e:
                info, ok = str(e)[:500], False
            shot = await bot.screenshot(f"check-{stage}")
            if not ok:
                await bot.dump(f"check-{stage}")
            out.append({"stage": stage, "label": label, "ok": ok, "info": info, "shot": shot})
            if not ok:
                break
        try:
            await bot.run("cancel", vars)
        except Exception:
            pass
    return out
