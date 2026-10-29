"""投票サイトの画面操作。手順は app/selectors/*.json に書かれていて、
data/selectors/*.json を置くとそちらが優先される（サーバー上で画面に合わせて直せる）。

手順の書き方（1ステップ = 1つのdict）:
  {"do": "goto",   "url": "..."}
  {"do": "fill",   "sel": "...", "value": "{pin}"}
  {"do": "click",  "sel": "..."}
  {"do": "select", "sel": "...", "label": "{bet_type}"}
  {"do": "press",  "key": "Enter"}
  {"do": "wait",   "ms": 500}
  {"do": "wait_for", "sel": "..."}
  {"do": "expect_text", "text": "{total_comma}"}   見つからなければ中止
  {"do": "pick"}   券種の各列・各馬番について "pick" の手順を実行
  {"do": "read",   "sel": "...", "var": "odds"}   画面の数字を読み取って変数に入れる
  {"do": "has",    "sel": "...", "var": "tansho_hit"}   表示されていれば1、なければ0
  {"do": "read_time", "sel": "...", "var": "post_time"}  画面の「15:45」などを読み取る
共通オプション: "optional": true（失敗しても続ける）, "timeout": ミリ秒
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from playwright.async_api import Browser, Page, async_playwright
from playwright.async_api import TimeoutError as PWTimeout

from ..config import JST, ROOT, config
from ..db import now

log = logging.getLogger(__name__)

METHOD_LABELS = {"normal": "通常", "box": "ボックス", "formation": "フォーメーション"}


@dataclass
class VoteResult:
    status: str  # done / rehearsed / failed / unknown / skipped
    message: str
    shot: str = ""
    tickets: list | None = None  # 実際に入力した馬券（オッズで単勝を外した後）
    odds: float | None = None


ELEMENTS_JS = r"""() => {
  const out = [];
  const q = 'a,button,input,select,textarea,label,img,area,[onclick],[role=button]';
  for (const el of document.querySelectorAll(q)) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    let d = el.tagName.toLowerCase();
    if (el.id) d += '#' + el.id;
    if (el.className && typeof el.className === 'string') d += '.' + el.className.trim().split(/\s+/).slice(0, 3).join('.');
    for (const a of ['name', 'type', 'alt', 'href']) {
      const v = el.getAttribute(a);
      if (v) d += `[${a}=${v.slice(0, 40)}]`;
    }
    if (el.tagName === 'INPUT' && ['button', 'submit', 'radio', 'checkbox', 'image'].includes(el.type)) d += `[value=${(el.value || '').slice(0, 20)}]`;
    if (el.disabled) d += '[disabled]';
    const oc = el.getAttribute('onclick');
    if (oc) d += ` onclick=${oc.slice(0, 50)}`;
    const t = (el.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 25);
    if (t) d += ` "${t}"`;
    out.push(d);
  }
  return out;
}"""


class StepError(RuntimeError):
    pass


ONSCREEN_DEFAULT = "a, button, input[type=button], input[type=submit], span, div, li, td, p, img"
ONSCREEN_TEXT = "div, p, span, td, th, li, b, strong, dd, dt"
ONSCREEN_JS = r"""([sel, text, index, contains]) => {
  document.querySelectorAll('[data-kb-target]').forEach((x) => x.removeAttribute('data-kb-target'));
  const label = (e) => ((e.tagName === 'INPUT' ? e.value : e.textContent) || '').replace(/\s+/g, '').trim();
  const want = text.replace(/\s+/g, '');
  const hits = [];
  for (const e of document.querySelectorAll(sel)) {
    if (want && (contains ? !label(e).includes(want) : label(e) !== want)) continue;
    const r = e.getBoundingClientRect();
    if (!r.width || !r.height) continue;
    const st = getComputedStyle(e);
    if (st.visibility === 'hidden' || st.opacity === '0') continue;
    e.scrollIntoView({ block: 'center', inline: 'center' });
    const q = e.getBoundingClientRect();
    const x = q.left + q.width / 2, y = q.top + q.height / 2;
    if (x < 0 || y < 0 || x > innerWidth || y > innerHeight) continue;
    const top = document.elementFromPoint(x, y);
    if (!top || !(top === e || e.contains(top))) continue;
    // 同じ文字の入れ子（div > span など）は外側だけ数える
    if (want && hits.some((h) => h.contains(e))) continue;
    hits.push(e);
  }
  const e = hits[index];
  if (!e) return false;
  e.setAttribute('data-kb-target', '1');
  return true;
}"""


def wide_nagashi(tickets: list[dict]) -> tuple[dict | None, list[dict]]:
    """同じ金額のワイドが2点以上あり、全部に共通の馬（◎）がいれば、流し1つにまとめる。
    戻り値: (流し {"axis", "partners", "amount"（1組あたり）} または None, 残りの馬券)"""
    wides = [t for t in tickets if t["bet_type"] == "ワイド"]
    if len(wides) < 2 or len({t["amount"] for t in wides}) != 1:
        return None, tickets
    common = set(wides[0]["combo"])
    for t in wides[1:]:
        common &= set(t["combo"])
    if len(common) != 1:
        return None, tickets
    axis = common.pop()
    partners = [n for t in wides for n in t["combo"] if n != axis]
    rest = [t for t in tickets if t["bet_type"] != "ワイド"]
    return {"axis": axis, "partners": partners, "amount": wides[0]["amount"]}, rest


def load_flow(site: str) -> dict:
    override = config.data_dir / "selectors" / f"{site}.json"
    path = override if override.exists() else ROOT / "app" / "selectors" / f"{site}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def render(template: str, vars: dict) -> str:
    def rep(m: re.Match) -> str:
        key = m.group(1)
        if key not in vars:
            raise StepError(f"手順の変数が不明です: {{{key}}}")
        return str(vars[key])
    return re.sub(r"\{(\w+)\}", rep, template)


class SiteBot:
    """1回のログインで、同じサイトの買い目をまとめて投票する。"""

    def __init__(self, site: str, credentials: dict):
        self.site = site
        self.flow = load_flow(site)
        self.credentials = credentials
        self._pw = None
        self.browser: Browser | None = None
        self.page: Page | None = None
        self.step_log: list[str] = []

    async def __aenter__(self) -> "SiteBot":
        missing = [k for k, v in self.credentials.items() if not v and k in self.flow.get("required", [])]
        if missing:
            raise StepError(f"{self.site} のログイン情報が未設定です: {', '.join(missing)}")
        self._pw = await async_playwright().start()
        # メモリの少ないサーバー（1GB）でも動くよう、ブラウザのメモリ使用を抑える
        kw = {"headless": config.headless,
              "args": ["--disable-dev-shm-usage", "--disable-gpu", "--disable-extensions",
                       "--renderer-process-limit=2", "--js-flags=--max-old-space-size=256"]}
        if config.chromium_path:
            kw["executable_path"] = config.chromium_path
        self.browser = await self._pw.chromium.launch(**kw)
        ctx = await self.browser.new_context(
            locale="ja-JP", timezone_id="Asia/Tokyo",
            viewport=self.flow.get("viewport", {"width": 1280, "height": 900}),
            user_agent=self.flow.get("user_agent") or None,
        )
        ctx.set_default_timeout(self.flow.get("timeout", 15000))
        self.page = await ctx.new_page()
        self.page.on("dialog", lambda d: d.accept())  # 確認ダイアログは常にOK
        ctx.on("page", self._on_new_window)
        return self

    def _on_new_window(self, page: Page) -> None:
        """サイトが新しいウィンドウ（投票画面など）を開いたら、以後はそちらを操作する。"""
        page.on("dialog", lambda d: d.accept())
        self.page = page

    async def __aexit__(self, *exc) -> None:
        if self.browser:
            await self.browser.close()
        if self._pw:
            await self._pw.stop()

    async def screenshot(self, tag: str) -> str:
        name = f"{now().strftime('%Y%m%d-%H%M%S')}-{self.site}-{tag}.png"
        path = config.data_dir / "shots" / name
        try:
            await self.page.screenshot(path=str(path), full_page=True)
            return name
        except Exception:
            return ""

    async def elements(self, limit: int = 400) -> str:
        """調整用：画面の押せる部品の一覧を文字で返す。入力欄に入っている値（暗証番号など）は含めない。"""
        lines: list[str] = []
        for fi, frame in enumerate(self.page.frames):
            try:
                rows = await frame.evaluate(ELEMENTS_JS)
            except Exception:
                continue
            lines += [f"f{fi} {r}" for r in rows]
            if len(lines) >= limit:
                break
        return "\n".join(lines[:limit])

    async def dump(self, tag: str) -> None:
        """調整用：画面とHTMLを保存する。"""
        await self.screenshot(tag)
        html = config.data_dir / "shots" / f"{now().strftime('%Y%m%d-%H%M%S')}-{self.site}-{tag}.html"
        try:
            html.write_text(await self.page.content(), encoding="utf-8")
        except Exception:
            pass

    async def run(self, section: str, vars: dict, pick_targets: list[tuple[int, int]] | None = None) -> None:
        for i, step in enumerate(self.flow.get(section, [])):
            label = f"{section}[{i}] {step.get('do')} {step.get('sel') or step.get('url') or step.get('text') or ''}"
            try:
                if step["do"] == "pick":
                    for col, num in pick_targets or []:
                        await self.run("pick", {**vars, "col": col, "num": num})
                else:
                    await self._step(step, vars)
                self.step_log.append("ok " + label)
            except Exception as e:
                if step.get("optional"):
                    self.step_log.append("skip " + label)
                    continue
                raise StepError(f"{label} で失敗: {e}") from e

    async def _mark_onscreen(self, sel: str, text: str, index: int, timeout: int, contains: bool = False) -> str:
        """sel（CSS）に合う部品のうち、画面の表に出ているもの（他の画面に隠れていない）の index 番目に
        目印を付け、その目印のセレクタを返す。text を渡すと、文字がちょうど一致するものだけにする。
        画面が切り替わる途中のこともあるので、timeout まで待ちながら探す。"""
        deadline = asyncio.get_running_loop().time() + timeout / 1000
        while True:
            found = await self.page.evaluate(ONSCREEN_JS, [sel, text, index, contains])
            if found:
                return "[data-kb-target='1']"
            if asyncio.get_running_loop().time() > deadline:
                raise StepError(f"画面に出ている「{text or sel}」の{index + 1}番目が見つかりません")
            await asyncio.sleep(0.3)

    async def _find(self, sel: str, timeout: int):
        """画面に見えている要素のうち最初のものを、ページの中の区画（フレーム）もすべて探して返す。
        サイトによっては同じボタンが隠れて複数あったり、古い作りで画面が区画に分かれていたりするため。
        見つからないまま timeout になったらエラーにする。"""
        deadline = asyncio.get_running_loop().time() + timeout / 1000
        while True:
            for frame in self.page.frames:
                loc = frame.locator(sel).filter(visible=True)
                try:
                    if await loc.count():
                        return loc.first
                except Exception as e:
                    if "pars" in str(e) or "selector" in str(e).lower() and "detached" not in str(e):
                        raise  # セレクタの書き方の間違いはすぐに知らせる
            if asyncio.get_running_loop().time() > deadline:
                raise StepError(f"{timeout}ms 待っても見つかりません: {sel}")
            await asyncio.sleep(0.25)

    def _visible(self, sel: str):
        """画面に見えている要素のうち最初のもの。サイトによっては同じボタンが隠れて複数あるため。"""
        return self.page.locator(sel).filter(visible=True).first

    async def _step(self, step: dict, vars: dict) -> None:
        p = self.page
        t = step.get("timeout")
        do = step["do"]
        sel = render(step["sel"], vars) if "sel" in step else None
        if step.get("onscreen"):
            # 画面の表に実際に出ている部品だけから選ぶ（同じ部品が裏に重なっているサイト用）
            if do == "expect_text":
                # 画面の表に出ている部品に、この文字が含まれていること
                await self._mark_onscreen(sel or ONSCREEN_TEXT, render(step["text"], vars), 0, t or 8000,
                                          contains=True)
                return
            sel = await self._mark_onscreen(sel or ONSCREEN_DEFAULT, render(step.get("text", ""), vars),
                                            int(render(str(step.get("index", 0)), vars)), t or 8000)
        loc = None
        if sel and do in ("fill", "click", "select", "wait_for", "read", "read_time", "has"):
            default = 3000 if do == "has" else (5000 if do in ("read", "read_time") else self.flow.get("timeout", 15000))
            try:
                loc = await self._find(sel, t or default)
            except StepError:
                # ボタンが見つからない・押せないとき、サイトのJavaScriptを直接呼ぶ（"js_fallback" がある場合のみ）。
                # SPAT4のログインボタンは大きさ0の部品に見えるため
                if do == "click" and step.get("js_fallback"):
                    await p.evaluate(step["js_fallback"])
                    return
                raise
            t = t or default  # 見つけた後の操作（押したあとの画面の切り替わり待ちも含む）は、同じ時間まで待つ
        if do == "main_window":
            # 別ウィンドウ（投票画面など）を閉じて、最初のウィンドウに戻る
            pages = p.context.pages
            for extra in pages[1:]:
                await extra.close()
            self.page = pages[0]
            return
        if do == "goto":
            await p.goto(render(step["url"], vars), wait_until="domcontentloaded")
        elif do == "fill":
            await loc.fill(render(step["value"], vars), timeout=t)
        elif do == "click":
            try:
                if step.get("new_window"):
                    # 新しいウィンドウが開くかもしれないボタン。開いたらそちらへ、開かなければ今の画面のまま
                    waiter = asyncio.ensure_future(p.context.wait_for_event("page", timeout=max(t, 15000)))
                    try:
                        await loc.click(timeout=t)
                    except Exception:
                        # 押した直後に別ウィンドウが開くと、押す操作そのものが時間切れ扱いになることがある。
                        # ウィンドウが開いていれば成功とみなす
                        try:
                            self.page = await asyncio.wait_for(asyncio.shield(waiter), 5)
                            await self.page.wait_for_load_state("domcontentloaded")
                            return
                        except Exception:
                            waiter.cancel()
                            raise
                    try:
                        self.page = await waiter
                        await self.page.wait_for_load_state("domcontentloaded")
                    except PWTimeout:
                        pass
                else:
                    await loc.click(timeout=t)
            except Exception:
                # ボタンが押せないとき、サイトのJavaScriptを直接呼ぶ（"js_fallback" がある場合のみ）
                if not step.get("js_fallback"):
                    raise
                await p.evaluate(step["js_fallback"])
        elif do == "select":
            await loc.select_option(label=render(step["label"], vars), timeout=t)
        elif do == "press":
            await p.keyboard.press(step["key"])
        elif do == "wait":
            await p.wait_for_timeout(step["ms"])
        elif do == "wait_for":
            await loc.wait_for(state="visible", timeout=t)
        elif do == "read":
            text = await loc.inner_text(timeout=t or 5000)
            # "after_name": 馬名（数字でない言葉）のすぐ後ろの数字を読む。
            #   オッズの行は「枠 馬番 馬名 単勝 複勝」と並び、170倍のように小数点のないオッズもあるため
            if step.get("pattern") == "after_name":
                m = re.search(r"[^\d\s.,\-]+\s+(\d[\d,]*(?:\.\d+)?)", text)
            else:
                m = re.search(r"(\d[\d,]*(?:\.\d+)?)", text)
            if not m:
                raise StepError(f"数字を読み取れません: {text!r}")
            vars[step["var"]] = float(m.group(1).replace(",", ""))
        elif do == "read_time":
            text = await loc.inner_text(timeout=t or 5000)
            m = re.search(r"(\d{1,2})[:：時](\d{2})", text)
            if not m:
                raise StepError(f"時刻を読み取れません: {text!r}")
            vars[step["var"]] = f"{int(m.group(1)):02d}:{m.group(2)}"
        elif do == "has":
            try:
                await loc.wait_for(state="visible", timeout=t or 3000)
                vars[step["var"]] = 1
            except Exception:
                vars[step["var"]] = 0
        elif do == "expect_text":
            text = render(step["text"], vars)
            await p.get_by_text(text).first.wait_for(state="visible", timeout=t or 8000)
        else:
            raise StepError(f"不明な手順です: {do}")

    async def login(self) -> None:
        await self.run("login", dict(self.credentials))

    async def read_numbers(self, section: str, vars: dict, keys: tuple[str, ...]) -> dict:
        """手順を実行して読み取った数字を返す。読めなかった項目は含まれない。"""
        out = dict(vars)
        await self.run(section, out)
        return {k: out[k] for k in keys if k in out}

    async def read_post_time(self, venue: str, race_no: int) -> str | None:
        vars = {**self.credentials, "venue": venue, "race_no": race_no}
        await self.run("post_time", vars)
        t = vars.get("post_time")
        add = self.flow.get("post_time_add_min", 0)  # 画面の時刻が締切時刻のサイト用
        if t and add:
            h, m = map(int, t.split(":"))
            total = h * 60 + m + add
            t = f"{total // 60 % 24:02d}:{total % 60:02d}"
        return t

    async def check_result(self, order: dict) -> dict:
        """払戻を確認する。{"settled": 確定なら1, "payout": この買い目の払戻合計,
        "tansho_hit": 単勝が当たったら1, "winner": 1着馬番} のうち読めたものを返す。"""
        vars = {**self.credentials, "venue": order["venue"], "race_no": order["race_no"]}
        got = await self.read_numbers("result", vars, ("settled", "winner", "payout", "tansho_hit"))
        return {k: int(v) for k, v in got.items()}

    async def _enter(self, tickets: list[dict], base: dict) -> None:
        """馬券を入力する（購入はしない）。base の total などはこの分の金額に書き換える。"""
        total = sum(t["amount"] for t in tickets)
        base.update(total=total, total_comma=f"{total:,}", points=len(tickets))
        if self.flow.get("entry_mode") == "groups":
            await self._enter_groups(tickets, base)
            return
        # マークカード式のサイトは、1画面にcards_per_page枚のカードがあり、1点ごとに次のカードを使う
        per_page = self.flow.get("cards_per_page", 0)
        for i, t in enumerate(tickets):
            if per_page and i and i % per_page == 0:
                await self.run("next_cards", base)
            combo = t["combo"]
            targets = [(1, combo[0])] if len(combo) == 1 else [(j + 1, n) for j, n in enumerate(combo)]
            await self.run("ticket", {**base, "bet_type": t["bet_type"],
                                      "bet_label": self.flow.get("bet_labels", {}).get(t["bet_type"], t["bet_type"]),
                                      "card": i % per_page + 1 if per_page else 1,
                                      "units": t["amount"] // 100,
                                      "amount": t["amount"], "combo": "-".join(map(str, combo))}, targets)

    async def _enter_groups(self, tickets: list[dict], base: dict) -> None:
        """マークカード式のサイト（SPAT4）。
        カードに印をつける → 「投票金額入力へ」→ 1行ずつ金額 →（続きがあれば「買い目を追加する」）を繰り返す。
        カードの入力は券種ごとに分ける（途中で券種を変えると、それまでの印が消えるため）。
        ワイドは◎からの流しにまとめて、カード1枚で入れる。"""
        per_page = self.flow.get("cards_per_page", 6)
        groups = []
        nagashi, rest = wide_nagashi(tickets) if self.flow.get("nagashi") else (None, tickets)
        if nagashi:
            groups.append(("nagashi", nagashi))
        # SPAT4は、カードの途中で券種を変えると入力が消える（「式別を変更すると選択した内容がクリアされます」）。
        # そのため券種ごとに分け、それぞれ金額まで入れてから「買い目を追加する」で次の券種に進む
        by_type: dict[str, list[dict]] = {}
        for t in rest:
            by_type.setdefault(t["bet_type"], []).append(t)
        for same in by_type.values():
            for i in range(0, len(same), per_page):
                groups.append(("cards", same[i:i + per_page]))
        labels = self.flow.get("bet_labels", {})
        row = 0
        for gi, (kind, g) in enumerate(groups):
            if gi:
                await self.run("add_more", base)
            if kind == "nagashi":
                v = {**base, "bet_label": labels.get("ワイド", "ワイド")}
                await self.run("nagashi", v)
                await self.run("pick", {**v, "num": g["axis"], "card": 1, "card0": 0})
                for n in g["partners"]:
                    await self.run("pick", {**v, "num": n, "card": 2, "card0": 1})
                amounts = [g["amount"] // 100]
            else:
                await self.run("cards_tab", base)
                for i, t in enumerate(g):
                    combo = t["combo"]
                    await self.run("ticket", {**base, "bet_type": t["bet_type"],
                                              "bet_label": labels.get(t["bet_type"], t["bet_type"]),
                                              "card": i + 1, "card0": i},
                                   [(j + 1, n) for j, n in enumerate(combo)])
                amounts = [t["amount"] // 100 for t in g]
            await self.run("to_amount", base)
            off = self.flow.get("amount_row_offset", 0)
            for u in amounts:
                await self.run("amount", {**base, "units": u, "row": row, "row_input": off + row})
                row += 1

    async def vote(self, order: dict, mode: str, odds_rule: dict | None = None) -> VoteResult:
        """1件の買い目を入力し、mode=live のときだけ購入ボタンを押す。

        odds_rule = {"horse": 馬番, "min": 2.1, "unknown": "skip"|"buy"} を渡すと、
        レースを開いた直後に単勝オッズを読み、min以下なら単勝の馬券を外す。
        """
        tickets = [dict(t) for t in order["tickets"]]
        base = {
            **self.credentials,
            "venue": order["venue"],
            "race_no": order["race_no"],
            "bet_type": order["bet_type"],
            "method_label": METHOD_LABELS["normal"],
        }
        purchase_clicked = False
        bought = 0
        odds = None
        try:
            # オッズが投票画面とは別の画面にあるサイトは、先にオッズを読んでから投票画面を開く
            early = self.flow.get("odds_before_open")
            if not early:
                await self.run("open_race", base)
            if odds_rule:
                try:
                    got = await self.read_numbers("read_odds", {**base, "num": odds_rule["horse"]}, ("odds",))
                    odds = got.get("odds")
                except StepError:
                    odds = None
                drop = (odds is None and odds_rule["unknown"] == "skip") or (odds is not None and odds <= odds_rule["min"])
                if drop:
                    tickets = [t for t in tickets if t["bet_type"] != "単勝"]
                if not tickets:
                    why = "オッズを読めない" if odds is None else f"単勝{odds}倍"
                    return VoteResult("skipped", f"{why}のため見送りました", odds=odds, tickets=[])
            if early:
                await self.run("open_race", base)
            total = sum(t["amount"] for t in tickets)
            # 1回の投票で入れられる点数に上限があるサイトは、分けて投票する
            limit = self.flow.get("max_tickets_per_vote") or len(tickets)
            batches = [tickets[i:i + limit] for i in range(0, len(tickets), limit)]
            bought = 0
            for n, batch in enumerate(batches):
                if n:
                    await self.run("open_race", base)
                await self._enter(batch, base)
                await self.run("confirm", base)  # ここで合計金額が一致するか確認する
                if mode != "live":
                    await self.run("cancel", base)
                    continue
                purchase_clicked = True
                await self.run("purchase", base)
                await self.run("done_check", base)
                bought += 1
            split = f"、{len(batches)}回に分けて" if len(batches) > 1 else ""
            if mode != "live":
                shot = await self.screenshot(f"order{order['id']}-rehearsal")
                return VoteResult("rehearsed", f"購入直前まで入力できました（{total:,}円{split}、購入はしていません）",
                                  shot, tickets, odds)
            shot = await self.screenshot(f"order{order['id']}-done")
            return VoteResult("done", f"投票しました（{len(tickets)}点 {total:,}円{split}）", shot, tickets, odds)
        except Exception as e:
            shot = await self.screenshot(f"order{order['id']}-error")
            if not purchase_clicked:
                # 入力途中の馬券が残ると次の買い目に混ざるので取り消しておく
                try:
                    await self.run("cancel", base)
                except Exception:
                    pass
            if purchase_clicked:
                part = f"（分けた投票のうち{bought}回分は購入済み）" if bought else ""
                return VoteResult("unknown", f"購入ボタンを押した後にエラー{part}。購入履歴を確認してください: {e}",
                                  shot, tickets, odds)
            return VoteResult("failed", f"購入前に中止しました: {e}", shot, tickets, odds)
