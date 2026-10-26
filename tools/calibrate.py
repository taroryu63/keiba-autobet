"""接続テストをサーバーのコマンドで行う（アプリの「接続テスト」ボタンと同じ処理）。購入はしない。

  docker compose exec app python -m tools.calibrate ipat --venue 東京 --race 11 --horse 4
"""
from __future__ import annotations

import argparse
import asyncio

from app.bots.engine import SiteBot
from app.calibration import run_check
from app.config import config
from app.scheduler import credentials


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("site", choices=["ipat", "spat4"])
    ap.add_argument("--venue", required=True)
    ap.add_argument("--race", type=int, required=True)
    ap.add_argument("--horse", type=int, required=True)
    a = ap.parse_args()
    (config.data_dir / "shots").mkdir(parents=True, exist_ok=True)
    for x in await run_check(SiteBot(a.site, credentials(a.site)), a.venue, a.race, a.horse):
        print("○" if x["ok"] else "×", x["label"], x["info"], x["shot"])


if __name__ == "__main__":
    asyncio.run(main())
