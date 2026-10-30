"""SQLiteへの保存。予約した買い目・設定・ログを持つ。"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from .config import JST

# 予算に数える状態（予約中・投票中・投票済み・結果不明）
COUNTED = ("pending", "voting", "done", "unknown")

DEFAULT_SETTINGS = {
    "mode": "off",           # off=ブラウザを使わない練習 / rehearsal=購入直前まで / live=本番
    "vote_offset_min": 5,    # 発走の何分前に投票するか
    # 地方：複勝◎の転がし
    "roll_budget": 3000,     # 転がしの資金（円）。500円×6回外れで終わり
    "roll_stake": 500,       # 転がしの1回目の金額（円）
    "roll_target": 4,        # 何回連続で的中したら終了するか
    # 中央：単勝◎
    "tansho_base": 100,      # 単勝の最初の金額（円）。外れるたびにこの額ずつ増やす
    "tansho_max_losses": 7,  # 何回連続で外れたら、その日の単勝を終わりにするか（100〜700円、計2,800円）
    "tansho_min_odds": 2.1,  # この倍率以下の単勝は見送る
    "odds_unknown": "skip",  # オッズを読めなかったとき: skip=単勝を見送る / buy=買う
    "pending_policy": "assume_loss",  # 前の単勝の結果が未確定: assume_loss=外れ扱いで増額 / skip=見送る
    # 地方・中央共通：ワイド・3連複の流しと複勝★
    "bet_limit": 7000,       # 1日の予算（円）。地方と中央で別々に数える
    "wide_amount": 100,      # ワイド◎→★の1点の金額
    "sanrenpuku_amount": 100,  # 3連複◎→★の1点の金額
    "fukusho_amount": 100,   # 複勝★の1点の金額
    "result_delay_min": 10,  # 発走から何分後に払戻を確認し始めるか
    "paused": False,         # 一時停止
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  org TEXT NOT NULL,
  race_date TEXT NOT NULL,
  venue TEXT NOT NULL,
  race_no INTEGER NOT NULL,
  post_time TEXT NOT NULL,
  bet_type TEXT NOT NULL,
  method TEXT NOT NULL,
  columns_json TEXT NOT NULL,
  unit_amount INTEGER NOT NULL,
  points INTEGER NOT NULL,
  total INTEGER NOT NULL,
  tickets_json TEXT NOT NULL,
  dedupe_key TEXT NOT NULL,
  vote_at TEXT NOT NULL,
  status TEXT NOT NULL,
  message TEXT NOT NULL DEFAULT '',
  shot TEXT NOT NULL DEFAULT '',
  strategy TEXT NOT NULL DEFAULT 'manual',  -- manual / roll（平日の複勝転がし）/ holiday（休日の◎★）
  plan_json TEXT NOT NULL DEFAULT '{}',     -- 戦略の入力（◎・★など）
  chain_id INTEGER,          -- 転がし・単勝の増額の管理ID
  payout INTEGER,            -- 払戻金（外れは0、未確定はNULL）
  next_check_at TEXT,
  winner INTEGER             -- 1着馬番（結果確認で分かったもの）
);
CREATE TABLE IF NOT EXISTS chains (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  race_date TEXT NOT NULL,
  kind TEXT NOT NULL,        -- roll（平日の複勝転がし）/ tansho（休日の単勝の増額）
  status TEXT NOT NULL,      -- active / won / exhausted / stopped
  losses INTEGER NOT NULL DEFAULT 0,      -- 単勝: 確定した外れの数
  wins INTEGER NOT NULL DEFAULT 0,        -- 今の挑戦での連続的中数
  stake INTEGER,             -- 次に賭ける金額（NULLなら新しく500円から始める）
  attempts INTEGER NOT NULL DEFAULT 0,    -- 何回500円から始めたか
  own_spent INTEGER NOT NULL DEFAULT 0,   -- 自分の資金から使った額
  carry INTEGER NOT NULL DEFAULT 0,       -- 賭けずに残った端数
  message TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL,
  UNIQUE(race_date, kind)
);
CREATE INDEX IF NOT EXISTS orders_status ON orders(status, vote_at);
CREATE TABLE IF NOT EXISTS sims (
  race_date TEXT PRIMARY KEY,
  text TEXT NOT NULL,
  report_json TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL,
  order_id INTEGER,
  level TEXT NOT NULL,
  text TEXT NOT NULL
);
"""


def now() -> datetime:
    return datetime.now(JST)


def now_iso() -> str:
    return now().isoformat(timespec="seconds")


class DB:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        with self.conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def conn(self):
        with self._lock:
            c = sqlite3.connect(self.path)
            c.row_factory = sqlite3.Row
            try:
                yield c
                c.commit()
            finally:
                c.close()

    # --- 設定 ---
    def settings(self) -> dict:
        out = dict(DEFAULT_SETTINGS)
        with self.conn() as c:
            for r in c.execute("SELECT key, value FROM settings"):
                if r["key"] in out:
                    out[r["key"]] = json.loads(r["value"])
        return out

    def save_settings(self, values: dict) -> dict:
        with self.conn() as c:
            for k, v in values.items():
                if k in DEFAULT_SETTINGS:
                    c.execute(
                        "INSERT INTO settings(key, value) VALUES(?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (k, json.dumps(v)),
                    )
        return self.settings()

    # --- 買い目 ---
    def add_order(self, o: dict) -> int:
        """買い目を保存する。転がしの候補は status='queued'（金額は投票時に決まる）。"""
        t = now_iso()
        with self.conn() as c:
            cur = c.execute(
                "INSERT INTO orders(created_at, updated_at, org, race_date, venue, race_no, post_time,"
                " bet_type, method, columns_json, unit_amount, points, total, tickets_json, dedupe_key,"
                " vote_at, status, chain_id, strategy, plan_json)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (t, t, o["org"], o["race_date"], o["venue"], o["race_no"], o["post_time"],
                 o["bet_type"], o["method"], json.dumps(o["columns"]), o["unit_amount"],
                 o["points"], o["total"], json.dumps(o["tickets"]), o["dedupe_key"], o["vote_at"],
                 o.get("status", "pending"), o.get("chain_id"), o.get("strategy", "manual"),
                 json.dumps(o.get("plan", {}))),
            )
            return cur.lastrowid

    def update_order(self, order_id: int, **fields) -> None:
        for k in ("tickets", "plan", "columns"):
            if k in fields:
                fields[k + "_json"] = json.dumps(fields.pop(k))
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.conn() as c:
            c.execute(f"UPDATE orders SET {cols}, updated_at=? WHERE id=?",
                      (*fields.values(), now_iso(), order_id))

    def get(self, order_id: int) -> dict | None:
        with self.conn() as c:
            r = c.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        return _row(r) if r else None

    def list_orders(self, race_date: str | None = None, limit: int = 200) -> list[dict]:
        with self.conn() as c:
            if race_date:
                rows = c.execute(
                    "SELECT * FROM orders WHERE race_date=? ORDER BY post_time, id", (race_date,)
                ).fetchall()
            else:
                rows = c.execute("SELECT * FROM orders ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [_row(r) for r in rows]

    def due_orders(self, at_iso: str) -> list[dict]:
        """投票時刻が来た通常の買い目（転がしの候補は含まない）。"""
        with self.conn() as c:
            rows = c.execute(
                "SELECT * FROM orders WHERE status='pending' AND vote_at<=? ORDER BY vote_at, id",
                (at_iso,),
            ).fetchall()
        return [_row(r) for r in rows]

    def chain_orders(self, chain_id: int, status: str | None = None) -> list[dict]:
        q = "SELECT * FROM orders WHERE chain_id=?"
        args: list = [chain_id]
        if status:
            q += " AND status=?"
            args.append(status)
        with self.conn() as c:
            rows = c.execute(q + " ORDER BY vote_at, id", args).fetchall()
        return [_row(r) for r in rows]

    def day_total(self, race_date: str, include_roll: bool = True) -> int:
        """その日に自分の資金から使う額。転がし以外の買い目の合計＋転がしで使った元手。
        転がしの2回目以降は払戻金を賭けているので数えない。include_roll=False なら転がしを除く。"""
        with self.conn() as c:
            normal = c.execute(
                f"SELECT COALESCE(SUM(total),0) FROM orders WHERE race_date=? AND strategy<>'roll'"
                f" AND status IN ({','.join('?'*len(COUNTED))})",
                (race_date, *COUNTED),
            ).fetchone()[0]
            chain = c.execute(
                "SELECT COALESCE(SUM(own_spent),0) FROM chains WHERE race_date=? AND kind='roll'", (race_date,)
            ).fetchone()[0]
        return normal + (chain if include_roll else 0)

    def bets_used(self, race_date: str, org: str, exclude_id: int | None = None) -> int:
        """その日・その競馬（地方か中央）で、予算（bet_limit）に数える額。単勝と転がしは除く。"""
        from .strategy import bets_cost
        total = 0
        for o in self.list_orders(race_date):
            if (o["org"] == org and o["strategy"] == "holiday" and o["status"] in COUNTED
                    and o["id"] != exclude_id):
                total += bets_cost(o["tickets"])
        return total

    def find_duplicate(self, dedupe_key: str) -> dict | None:
        with self.conn() as c:
            r = c.execute(
                f"SELECT * FROM orders WHERE dedupe_key=? AND status IN ({','.join('?'*len(COUNTED))},'queued')",
                (dedupe_key, *COUNTED),
            ).fetchone()
        return _row(r) if r else None

    def set_status(self, order_id: int, status: str, message: str = "", shot: str = "",
                   expect: str | None = None) -> bool:
        """状態を更新する。expect を渡すと、その状態のときだけ更新する（二重実行防止）。"""
        q = "UPDATE orders SET status=?, message=?, shot=COALESCE(NULLIF(?, ''), shot), updated_at=? WHERE id=?"
        args: list = [status, message, shot, now_iso(), order_id]
        if expect:
            q += " AND status=?"
            args.append(expect)
        with self.conn() as c:
            return c.execute(q, args).rowcount == 1

    def recover_interrupted(self) -> list[int]:
        """再起動時、投票中のまま止まった買い目は「結果不明」にする（自動で再投票しない）。"""
        with self.conn() as c:
            ids = [r[0] for r in c.execute("SELECT id FROM orders WHERE status='voting'")]
            c.execute(
                "UPDATE orders SET status='unknown', message='投票中にサーバーが再起動しました。投票サイトで購入履歴を確認してください', updated_at=? WHERE status='voting'",
                (now_iso(),),
            )
        return ids

    # --- 転がし ---
    def chain_for(self, race_date: str, kind: str, create: bool = False) -> dict | None:
        with self.conn() as c:
            q = "SELECT * FROM chains WHERE race_date=? AND kind=?"
            r = c.execute(q, (race_date, kind)).fetchone()
            if r is None and create:
                c.execute("INSERT INTO chains(race_date, kind, status, updated_at) VALUES(?, ?, 'active', ?)",
                          (race_date, kind, now_iso()))
                r = c.execute(q, (race_date, kind)).fetchone()
        return dict(r) if r else None

    def chains_on(self, race_date: str) -> list[dict]:
        with self.conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM chains WHERE race_date=?", (race_date,))]

    def awaiting_results(self, at_iso: str) -> list[dict]:
        """投票済みで結果を確認する時刻が来た買い目。"""
        with self.conn() as c:
            rows = c.execute(
                "SELECT * FROM orders WHERE status='done' AND strategy<>'manual' AND payout IS NULL"
                " AND next_check_at IS NOT NULL AND next_check_at<=? ORDER BY post_time",
                (at_iso,),
            ).fetchall()
        return [_row(r) for r in rows]

    def active_chains(self) -> list[dict]:
        with self.conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM chains WHERE status='active'")]

    def update_chain(self, chain_id: int, **fields) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.conn() as c:
            c.execute(f"UPDATE chains SET {cols}, updated_at=? WHERE id=?",
                      (*fields.values(), now_iso(), chain_id))

    # --- 仮想収支 ---
    def save_sim(self, race_date: str, text: str, report: dict) -> None:
        with self.conn() as c:
            c.execute("INSERT INTO sims(race_date, text, report_json, updated_at) VALUES(?,?,?,?) "
                      "ON CONFLICT(race_date) DO UPDATE SET text=excluded.text, report_json=excluded.report_json,"
                      " updated_at=excluded.updated_at", (race_date, text, json.dumps(report), now_iso()))

    def sims(self) -> list[dict]:
        with self.conn() as c:
            rows = c.execute("SELECT * FROM sims ORDER BY race_date DESC").fetchall()
        return [{"race_date": r["race_date"], "text": r["text"], "report": json.loads(r["report_json"]),
                 "updated_at": r["updated_at"]} for r in rows]

    def delete_sim(self, race_date: str) -> None:
        with self.conn() as c:
            c.execute("DELETE FROM sims WHERE race_date=?", (race_date,))

    # --- ログ ---
    def log(self, text: str, order_id: int | None = None, level: str = "info") -> None:
        with self.conn() as c:
            c.execute("INSERT INTO logs(at, order_id, level, text) VALUES(?,?,?,?)",
                      (now_iso(), order_id, level, text))

    def logs(self, limit: int = 100) -> list[dict]:
        with self.conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM logs ORDER BY id DESC LIMIT ?", (limit,))]


def _row(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["columns"] = json.loads(d.pop("columns_json"))
    d["tickets"] = json.loads(d.pop("tickets_json"))
    d["plan"] = json.loads(d.pop("plan_json"))
    return d
