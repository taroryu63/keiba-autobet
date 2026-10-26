"""投票サイトの画面に操作手順を合わせるための調整ツール。購入ボタンは絶対に押さない。

使い方（サーバー上で）:
  docker compose exec app python -m tools.calibrate ipat --venue 東京 --race 11 --horse 4
  docker compose exec app python -m tools.calibrate spat4 --venue 大井 --race 3 --horse 5

ログイン → 発走時刻の読み取り → レースを開く → オッズ読み取り → 馬券1点の入力 → 確認画面 → 取消
の順に進み、各段階の画面（PNG）とHTMLを data/shots/ に保存する。
どこで止まったかを見て、data/selectors/<site>.json（app/selectors からコピー）を直す。
"""
from __future__ import annotations

import argparse
import asyncio
import shutil

from app.bots.engine import SiteBot
from app.config import ROOT, config
from app.scheduler import credentials


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("site", choices=["ipat", "spat4"])
    ap.add_argument("--venue", required=True)
    ap.add_argument("--race", type=int, required=True)
    ap.add_argument("--horse", type=int, required=True)
    ap.add_argument("--until", choices=["login", "time", "race", "odds", "ticket", "confirm"], default="confirm")
    ap.add_argument("--show", action="store_true", help="ブラウザを画面に表示する（自宅PCで使うとき）")
    a = ap.parse_args()

    config.data_dir.mkdir(parents=True, exist_ok=True)
    (config.data_dir / "shots").mkdir(exist_ok=True)
    sel_dir = config.data_dir / "selectors"
    sel_dir.mkdir(exist_ok=True)
    target = sel_dir / f"{a.site}.json"
    if not target.exists():
        shutil.copy(ROOT / "app" / "selectors" / f"{a.site}.json", target)
        print(f"手順ファイルをコピーしました: {target}（これを編集してください）")
    if a.show:
        config.headless = False

    bot = SiteBot(a.site, credentials(a.site))
    order = {"id": 0, "venue": a.venue, "race_no": a.race, "bet_type": "複勝", "points": 1,
             "total": 100, "unit_amount": 100, "tickets": [{"bet_type": "複勝", "combo": [a.horse], "amount": 100}]}
    vars = {**bot.credentials, "venue": a.venue, "race_no": a.race, "num": a.horse, "bet_type": "複勝",
            "method_label": "通常", "units": 1, "amount": 100, "total": 100, "total_comma": "100", "points": 1,
            "combo": str(a.horse)}
    stages = [
        ("login", lambda: bot.login()),
        ("time", lambda: bot.read_post_time(a.venue, a.race)),
        ("race", lambda: bot.run("open_race", vars)),
        ("odds", lambda: bot.read_numbers("read_odds", vars, ("odds",))),
        ("ticket", lambda: bot.run("ticket", vars, [(1, a.horse)])),
        ("confirm", lambda: bot.run("confirm", vars)),
    ]
    async with bot:
        try:
            for name, fn in stages:
                print(f"--- {name}")
                got = await fn()
                if got:
                    print("  読み取り:", got)
                await bot.dump(f"cal-{name}")
                if name == a.until:
                    break
            print("ここまで成功しました（購入はしていません）")
        except Exception as e:
            print("失敗:", e)
            await bot.dump("cal-error")
        finally:
            for line in bot.step_log:
                print(" ", line)
            try:
                await bot.run("cancel", vars)
            except Exception:
                pass
    print(f"画面とHTML: {config.data_dir / 'shots'}")


if __name__ == "__main__":
    asyncio.run(main())
