"""接続テストをサーバーのコマンドで行う（アプリの「接続テスト」ボタンと同じ処理）。購入はしない。

  docker compose exec -T app python -m tools.calibrate spat4 --venue 大井 --race 11 --horse 1
  docker compose exec -T app python -m tools.calibrate races nar        # 今日の開催とレース一覧

各段階の結果と、失敗した段階の「画面の部品の一覧」を表示する。画面の画像は data/shots/ に残る。
"""
from __future__ import annotations

import argparse
import asyncio

from app.bots.engine import SiteBot
from app.calibration import run_check
from app.config import config
from app.db import now
from app.scheduler import credentials


async def check(a) -> None:
    (config.data_dir / "shots").mkdir(parents=True, exist_ok=True)
    for x in await run_check(SiteBot(a.site, credentials(a.site)), a.venue, a.race, a.horse):
        print("○" if x["ok"] else "×", x["label"], "|", x["info"], "|", x["shot"])
        if x.get("parts"):
            print("---- 画面の部品の一覧 ----")
            print(x["parts"])
            print("----")


def races(org: str) -> None:
    from app import results
    from app.betting import VENUES
    for (venue, no), info in sorted(results.race_list(org, now().date().isoformat(), VENUES[org]).items()):
        print(venue, f"{no}R", info["post_time"])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("site", choices=["ipat", "spat4", "races"])
    ap.add_argument("org", nargs="?", default="nar", help="races のとき: jra か nar")
    ap.add_argument("--venue")
    ap.add_argument("--race", type=int)
    ap.add_argument("--horse", type=int, default=1)
    a = ap.parse_args()
    if a.site == "races":
        races(a.org)
        return
    if not a.venue or not a.race:
        ap.error("--venue と --race を指定してください")
    asyncio.run(check(a))


if __name__ == "__main__":
    main()
