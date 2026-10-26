"""環境変数から読む設定。ログイン情報はDBに保存せず、ここ（.env）だけに置く。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")
ROOT = Path(__file__).resolve().parent.parent


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass
class Config:
    data_dir: Path = field(default_factory=lambda: Path(_env("DATA_DIR", str(ROOT / "data"))))
    app_password: str = field(default_factory=lambda: _env("APP_PASSWORD"))
    secret_key: str = field(default_factory=lambda: _env("SECRET_KEY"))
    # live投票を許可するか。設定画面でliveを選ぶには、これが1である必要がある
    allow_live: bool = field(default_factory=lambda: _env("ALLOW_LIVE") == "1")
    ntfy_url: str = field(default_factory=lambda: _env("NTFY_URL"))
    chromium_path: str = field(default_factory=lambda: _env("CHROMIUM_PATH"))
    headless: bool = field(default_factory=lambda: _env("HEADLESS", "1") != "0")

    # 即PAT（JRA）
    ipat_inet_id: str = field(default_factory=lambda: _env("IPAT_INET_ID"))
    ipat_subscriber: str = field(default_factory=lambda: _env("IPAT_SUBSCRIBER_NO"))
    ipat_pin: str = field(default_factory=lambda: _env("IPAT_PIN"))
    ipat_pars: str = field(default_factory=lambda: _env("IPAT_PARS"))

    # SPAT4（地方）
    spat4_subscriber: str = field(default_factory=lambda: _env("SPAT4_SUBSCRIBER_NO"))
    spat4_user_id: str = field(default_factory=lambda: _env("SPAT4_USER_ID"))
    spat4_pin: str = field(default_factory=lambda: _env("SPAT4_PIN"))

    def validate(self) -> None:
        if len(self.app_password) < 8:
            raise SystemExit("APP_PASSWORD を8文字以上で設定してください")
        if len(self.secret_key) < 32:
            raise SystemExit("SECRET_KEY を32文字以上で設定してください")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "shots").mkdir(exist_ok=True)


config = Config()
