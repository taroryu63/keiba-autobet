"use strict";
const $ = (s) => document.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const yen = (n) => `${Number(n || 0).toLocaleString("ja-JP")}円`;
const signed = (n) => `<span class="${n >= 0 ? "pos" : "neg"}">${n >= 0 ? "+" : "−"}${yen(Math.abs(n))}</span>`;

const STATUS = {
  needs_time: "発走時刻待ち", queued: "予約中", pending: "投票待ち", voting: "投票中", done: "投票済み",
  rehearsed: "リハーサル済み", failed: "失敗", unknown: "要確認", held: "保留（上限超え）",
  cancelled: "取消", expired: "締切切れ", skipped: "見送り",
};
const MODE = { off: "練習", rehearsal: "リハーサル", live: "本番" };
const MODE_HELP = {
  off: "投票サイトには接続しません。結果はnetkeibaから取り込み、ルール通りに進みます。",
  rehearsal: "投票サイトにログインして購入直前まで入力し、購入はしません。画面の調整用です。",
  live: "実際に購入します。",
};

let S = null; // /api/state の結果
let lastParse = null;

function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => t.classList.remove("show"), 3500);
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    method: opts.method || (opts.body ? "POST" : "GET"),
    headers: opts.body ? { "Content-Type": "application/json" } : {},
    body: opts.body ? JSON.stringify(opts.body) : undefined,
    credentials: "same-origin",
  });
  if (res.status === 401 && path !== "/api/login") {
    showLogin();
    throw new Error("ログインしてください");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const d = data.detail;
    throw new Error(typeof d === "string" ? d : Array.isArray(d) ? d.map((x) => x.msg).join(" / ") : `エラー (${res.status})`);
  }
  return data;
}

function showLogin() {
  $("#app").hidden = true;
  $("#login").hidden = false;
}

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("/api/login", { body: { password: $("#pw").value } });
    $("#login").hidden = true;
    start();
  } catch (err) {
    $("#login-err").textContent = err.message;
  }
});

// --- タブ ---
document.querySelectorAll(".tabs button").forEach((b) =>
  b.addEventListener("click", () => {
    document.querySelectorAll(".tabs button").forEach((x) => x.classList.toggle("on", x === b));
    document.querySelectorAll(".tab").forEach((t) => (t.hidden = t.id !== `tab-${b.dataset.tab}`));
    if (b.dataset.tab === "sim") loadSims();
    if (b.dataset.tab === "settings") loadLogs();
  })
);

// --- 今日 ---
async function load() {
  const d = $("#date").value;
  S = await api(`/api/state?d=${encodeURIComponent(d)}`);
  renderDay();
  renderOrders();
  renderSettings();
}

function renderDay() {
  const d = S.day;
  const mode = S.settings.mode;
  const chip = $("#mode-chip");
  chip.textContent = (S.settings.paused ? "一時停止中・" : "") + MODE[mode];
  chip.className = `chip ${mode}`;
  const pct = Math.min(100, (d.used / Math.max(1, d.limit)) * 100);
  let html = `<div class="row" style="justify-content:space-between"><h2 style="margin:0">${d.holiday ? "土日祝" : "平日"}のルール</h2>`
    + `<span class="muted">${esc(d.date)}</span></div>`;
  if (d.holiday) {
    const t = d.tansho;
    const ended = t.status !== "active";
    html += `<p class="muted">単勝◎は外れるたびに${yen(S.settings.tansho_base)}ずつ増額し、的中したらその日は終了します。ワイド◎-★と複勝★は各${yen(S.settings.wide_amount)}です。</p>
      <div class="stats">
        <div class="stat"><b>${ended ? "終了" : yen(t.next)}</b><span>${ended ? "単勝的中で終了" : "次の単勝"}</span></div>
        <div class="stat"><b>${t.losses}回</b><span>単勝の連続外れ</span></div>
        <div class="stat"><b>${yen(d.used)}</b><span>使用 / 上限 ${yen(d.limit)}</span></div>
      </div>
      <div class="bar-meter ${d.used > d.limit ? "over" : ""}"><i style="width:${pct}%"></i></div>`;
    if (t.message) html += `<p class="msg">${esc(t.message)}</p>`;
  } else {
    const r = d.roll;
    const ended = r.status !== "active";
    html += `<p class="muted">複勝◎を${yen(S.settings.roll_stake)}から転がし、${S.settings.roll_target}回連続で的中するか資金${yen(S.settings.weekday_limit)}を使い切ったら終了します。</p>
      <div class="stats">
        <div class="stat"><b>${r.wins} / ${S.settings.roll_target}</b><span>連続的中</span></div>
        <div class="stat"><b>${ended ? "終了" : yen(r.next)}</b><span>${ended ? (r.status === "won" ? "目標達成" : "資金切れ") : "次に賭ける額"}</span></div>
        <div class="stat"><b>${yen(r.own_spent)}</b><span>使った資金 / ${yen(S.settings.weekday_limit)}</span></div>
      </div>
      <div class="bar-meter"><i style="width:${pct}%"></i></div>`;
    if (r.message) html += `<p class="msg">${esc(r.message)}</p>`;
  }
  $("#day-card").innerHTML = html;
}

function ticketText(o) {
  if (!o.tickets.length) {
    if (o.strategy === "roll") return `複勝◎${o.plan.horse}（金額は投票時に決定）`;
    if (o.strategy === "holiday") {
      const p = o.plan;
      const parts = [];
      if (p.honmei) parts.push(`単勝◎${p.honmei}`);
      if (p.honmei && p.stars.length) parts.push(`ワイド${p.honmei}-${p.stars.join(".")}`);
      if (p.stars.length) parts.push(`複勝${p.stars.join(".")}`);
      return parts.join(" / ") + "（金額は投票時に決定）";
    }
  }
  const groups = {};
  for (const t of o.tickets) {
    const k = `${t.bet_type} ${t.amount}円`;
    (groups[k] ||= []).push(t.combo.join("-"));
  }
  return Object.entries(groups).map(([k, v]) => `${k.split(" ")[0]} ${v.join(", ")}（各${k.split(" ")[1]}）`).join(" / ");
}

function renderOrders() {
  const list = S.orders;
  if (!list.length) {
    $("#orders").innerHTML = `<p class="muted">この日の予約はありません。</p>`;
    return;
  }
  $("#orders").innerHTML = list.map((o) => {
    const acts = [];
    if (["needs_time", "queued", "pending"].includes(o.status)) acts.push(`<button class="small" data-act="time" data-id="${o.id}">発走時刻</button>`);
    if (o.status === "held") {
      acts.push(`<button class="small primary" data-act="approve" data-id="${o.id}">上限を超えて買う</button>`);
      acts.push(`<button class="small" data-act="cancel" data-id="${o.id}">見送る</button>`);
    } else if (["needs_time", "queued", "pending"].includes(o.status)) {
      acts.push(`<button class="small danger" data-act="cancel" data-id="${o.id}">取消</button>`);
    }
    if (o.status === "unknown") {
      acts.push(`<button class="small" data-act="bought" data-id="${o.id}">買えていた</button>`);
      acts.push(`<button class="small" data-act="notbought" data-id="${o.id}">買えていなかった</button>`);
    }
    if (o.status === "done" && o.strategy !== "manual" && o.payout === null) acts.push(`<button class="small" data-act="result" data-id="${o.id}">結果を入力</button>`);
    if (o.shot) acts.push(`<a class="small muted" href="/api/shots/${encodeURIComponent(o.shot)}" target="_blank">画面</a>`);
    const money = o.total ? yen(o.total) : "";
    const pay = o.payout !== null && o.payout !== undefined ? ` → 払戻 ${yen(o.payout)}` : "";
    const extra = o.plan.tansho_skipped ? `（単勝見送り: ${esc(o.plan.tansho_skipped)}）` : "";
    return `<div class="order">
      <div class="head"><span class="race">${esc(o.venue)}${o.race_no}R <span class="muted">${esc(o.post_time || "--:--")}</span></span>
        <span class="st-${o.status}">${STATUS[o.status] || esc(o.status)}</span></div>
      <div class="tickets">${esc(ticketText(o))}</div>
      <div class="msg">${money}${pay} ${esc(o.message || "")}${extra}${o.plan.paper ? '<span class="chip">練習</span>' : ""}</div>
      ${acts.length ? `<div class="row">${acts.join("")}</div>` : ""}
    </div>`;
  }).join("");
}

$("#orders").addEventListener("click", async (e) => {
  const b = e.target.closest("button[data-act]");
  if (!b) return;
  const id = Number(b.dataset.id);
  const o = S.orders.find((x) => x.id === id);
  try {
    if (b.dataset.act === "cancel") {
      if (!confirm(`${o.venue}${o.race_no}R を取り消しますか？`)) return;
      await api(`/api/orders/${id}/cancel`, { body: {} });
    } else if (b.dataset.act === "approve") {
      if (!confirm(`${o.venue}${o.race_no}R を上限を超えて買いますか？`)) return;
      await api(`/api/orders/${id}/approve`, { body: {} });
    } else if (b.dataset.act === "bought" || b.dataset.act === "notbought") {
      const bought = b.dataset.act === "bought";
      if (!confirm(bought ? "投票サイトの購入履歴で、購入されていたことを確認しましたか？" : "購入されていなかったことを確認しましたか？")) return;
      await api(`/api/orders/${id}/resolve`, { body: { bought } });
    } else if (b.dataset.act === "time") {
      const t = await ask(`${o.venue}${o.race_no}R の発走時刻`, `<input name="t" type="time" required value="${esc(o.post_time)}">`);
      if (!t) return;
      await api(`/api/orders/${id}/time`, { body: { post_time: t.get("t") } });
    } else if (b.dataset.act === "result") {
      const tansho = o.strategy === "holiday" && o.plan.tansho;
      const f = await ask(`${o.venue}${o.race_no}R の結果`,
        (tansho ? `<label>単勝◎${o.plan.honmei}<select name="hit"><option value="0">外れ</option><option value="1">的中</option></select></label>` : "")
        + `<label>払戻金の合計（円）<input name="payout" type="number" min="0" step="10" value="0" required></label>`);
      if (!f) return;
      const body = { payout: Number(f.get("payout")) };
      if (tansho) body.tansho_hit = f.get("hit") === "1";
      const r = await api(`/api/orders/${id}/result`, { body });
      toast(r.message);
    }
    await load();
  } catch (err) {
    toast(err.message);
  }
});

function ask(title, inner) {
  return new Promise((resolve) => {
    const dlg = $("#dlg");
    $("#dlg-form").innerHTML = `<h2>${esc(title)}</h2>${inner}
      <div class="row" style="justify-content:flex-end"><button value="cancel" formnovalidate>やめる</button><button class="primary" value="ok">決定</button></div>`;
    dlg.onclose = () => resolve(dlg.returnValue === "ok" ? new FormData($("#dlg-form")) : null);
    dlg.returnValue = "";
    dlg.showModal();
  });
}

$("#btn-parse").addEventListener("click", async () => {
  try {
    lastParse = await api("/api/parse", { body: { race_date: $("#date").value, text: $("#text").value } });
    renderParse();
  } catch (err) {
    toast(err.message);
  }
});
$("#btn-clear").addEventListener("click", () => {
  $("#text").value = "";
  $("#parse-out").innerHTML = "";
  lastParse = null;
});

function renderParse() {
  const p = lastParse;
  let html = "";
  if (p.errors.length) html += `<p class="err">${p.errors.map(esc).join("\n")}</p>`;
  if (p.rows.length) {
    html += `<div class="scroll"><table class="list"><tr><th>レース</th><th>買うもの</th></tr>`
      + p.rows.map((r) => `<tr><td>${esc(r.venue)}${r.race_no}R${r.post_time ? `<br><span class="muted">${esc(r.post_time)}</span>` : ""}</td>`
        + `<td class="${r.strategy ? "" : "muted"}">${esc(r.desc)}</td></tr>`).join("")
      + `</table></div>`;
    if (p.holiday) {
      html += `<p class="muted">ワイド・複勝の合計 ${yen(p.fixed_cost)} ＋ 単勝${p.tansho_races}レース分（100円から増額）。発走時刻は自動で取得します。</p>`;
    } else {
      html += `<p class="muted">◎のレースを順に転がしの候補にします。発走時刻は自動で取得します。</p>`;
    }
    html += `<button id="btn-bulk" class="primary" ${p.errors.length ? "disabled" : ""}>この内容で予約する</button>`;
  }
  $("#parse-out").innerHTML = html;
  const btn = $("#btn-bulk");
  if (btn) btn.addEventListener("click", async () => {
    btn.disabled = true;
    try {
      const r = await api("/api/bulk", { body: { race_date: $("#date").value, text: $("#text").value } });
      toast(`${r.created}レースを予約しました` + (r.skipped.length ? `（${r.skipped.length}件は登録せず）` : ""));
      $("#parse-out").innerHTML = r.skipped.length ? `<p class="muted">${r.skipped.map(esc).join("<br>")}</p>` : "";
      $("#text").value = "";
      await load();
    } catch (err) {
      toast(err.message);
      btn.disabled = false;
    }
  });
}

// --- 仮想収支 ---
$("#btn-sim").addEventListener("click", async () => {
  const btn = $("#btn-sim");
  btn.disabled = true;
  btn.textContent = "結果を取り込み中…";
  $("#sim-err").textContent = "";
  try {
    await api("/api/sims", { body: { race_date: $("#sim-date").value, text: $("#sim-text").value } });
    $("#sim-text").value = "";
    await loadSims();
  } catch (err) {
    $("#sim-err").textContent = err.message;
  } finally {
    btn.disabled = false;
    btn.textContent = "計算する";
  }
});

async function loadSims() {
  const sims = await api("/api/sims");
  const tot = sims.reduce((a, s) => ({ cost: a.cost + s.report.cost, payout: a.payout + s.report.payout }), { cost: 0, payout: 0 });
  const box = $("#sim-total");
  box.hidden = !sims.length;
  box.innerHTML = `<h2>通算（${sims.length}日）</h2><div class="stats">
    <div class="stat"><b>${signed(tot.payout - tot.cost)}</b><span>収支</span></div>
    <div class="stat"><b>${tot.cost ? ((tot.payout / tot.cost) * 100).toFixed(1) : "-"}%</b><span>回収率</span></div>
    <div class="stat"><b>${yen(tot.cost)}</b><span>購入合計</span></div></div>`;
  $("#sims").innerHTML = sims.map((s) => {
    const r = s.report;
    const rows = r.rows.map((x) => `<tr><td>${esc(x.venue)}${x.race_no}R<br><span class="muted">${esc(x.post_time)}</span></td>`
      + `<td>${x.tickets.length ? esc(ticketText({ tickets: x.tickets, strategy: "manual", plan: {} })) : ""}`
      + `${x.note ? `<br><span class="muted">${esc(x.note)}</span>` : ""}</td>`
      + `<td class="n">${x.cost ? yen(x.cost) : "-"}<br>${x.payout ? `<span class="pos">${yen(x.payout)}</span>` : ""}</td></tr>`).join("");
    return `<details class="card"><summary class="row" style="justify-content:space-between">
        <b>${esc(s.race_date)}（${r.holiday ? "土日祝" : "平日"}）</b><span>${signed(r.profit)} <span class="muted">回収率 ${r.roi ?? "-"}%</span></span></summary>
      <p class="muted">購入 ${yen(r.cost)} / 払戻 ${yen(r.payout)}${r.holiday ? "" : ` / 使った資金 ${yen(r.own)}`}${r.over_limit ? ` / <span class="neg">上限${yen(r.limit)}超え</span>` : ""}</p>
      ${r.notes && r.notes.length ? `<p class="err">${r.notes.map(esc).join("\n")}</p>` : ""}
      <div class="scroll"><table class="list"><tr><th>レース</th><th>買い目</th><th class="n">購入/払戻</th></tr>${rows}</table></div>
      <div class="row"><button class="small" data-redo="${esc(s.race_date)}">再計算</button><button class="small danger" data-del="${esc(s.race_date)}">削除</button></div>
      <textarea hidden data-text="${esc(s.race_date)}">${esc(s.text)}</textarea>
    </details>`;
  }).join("");
}

$("#sims").addEventListener("click", async (e) => {
  const redo = e.target.closest("[data-redo]");
  const del = e.target.closest("[data-del]");
  try {
    if (redo) {
      const d = redo.dataset.redo;
      redo.disabled = true;
      await api("/api/sims", { body: { race_date: d, text: document.querySelector(`textarea[data-text="${d}"]`).value } });
      await loadSims();
    } else if (del && confirm(`${del.dataset.del} の仮想収支を削除しますか？`)) {
      await api(`/api/sims/${del.dataset.del}`, { method: "DELETE" });
      await loadSims();
    }
  } catch (err) {
    toast(err.message);
  }
});

// --- 設定 ---
function renderSettings() {
  const f = $("#settings");
  if (f.contains(document.activeElement)) return; // 入力中は上書きしない
  const s = S.settings;
  for (const [k, v] of Object.entries(s)) {
    const el = f.elements[k];
    if (!el) continue;
    if (el instanceof RadioNodeList) el.value = v;
    else if (el.type === "checkbox") el.checked = !!v;
    else el.value = v;
  }
  f.querySelector('input[value="live"]').disabled = !S.allow_live;
  fillCheckVenues();
  $("#mode-help").textContent = MODE_HELP[s.mode] + (S.allow_live ? "" : "（本番はサーバーの設定で有効にすると選べます）");
}

$("#settings").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  const body = {};
  for (const el of f.elements) {
    if (!el.name) continue;
    if (el.type === "radio") { if (el.checked) body[el.name] = el.value; }
    else if (el.type === "checkbox") body[el.name] = el.checked;
    else if (el.type === "number") body[el.name] = Number(el.value);
    else body[el.name] = el.value;
  }
  if (body.mode === "live" && S.settings.mode !== "live" && !confirm("本番モードにすると実際に購入します。よろしいですか？")) return;
  try {
    await api("/api/settings", { method: "PUT", body });
    document.activeElement.blur();
    $("#settings-msg").textContent = "保存しました";
    await load();
  } catch (err) {
    $("#settings-msg").textContent = err.message;
  }
});

async function loadLogs() {
  const logs = await api("/api/logs");
  $("#logs").innerHTML = logs.map((l) => `<div><span>${esc(l.at.slice(5, 16).replace("T", " "))}</span> ${esc(l.text)}</div>`).join("") || "ログはありません";
}

// --- 接続テスト ---
function fillCheckVenues() {
  if (!S) return;
  const org = $("#chk-site").value === "ipat" ? "jra" : "nar";
  const sel = $("#chk-venue");
  if (sel.dataset.org === org) return;
  sel.dataset.org = org;
  sel.innerHTML = S.venues[org].map((v) => `<option>${esc(v)}</option>`).join("");
}
$("#chk-site").addEventListener("change", fillCheckVenues);
$("#btn-check").addEventListener("click", async () => {
  const btn = $("#btn-check");
  btn.disabled = true;
  btn.textContent = "テスト中…（1分ほどかかります）";
  $("#chk-out").innerHTML = "";
  try {
    const r = await api("/api/check", { body: {
      site: $("#chk-site").value, venue: $("#chk-venue").value,
      race_no: Number($("#chk-race").value), horse: Number($("#chk-horse").value) } });
    $("#chk-out").innerHTML = `<p class="${r.ok ? "pos" : "neg"}"><b>${r.ok ? "すべて成功しました（購入はしていません）" : "途中で止まりました"}</b></p>`
      + r.steps.map((x) => `<div class="order"><div class="head"><span>${x.ok ? "○" : "×"} ${esc(x.label)}</span></div>`
        + (x.info ? `<div class="msg ${x.ok ? "" : "neg"}">${esc(x.info)}</div>` : "")
        + (x.shot ? `<img src="/api/shots/${encodeURIComponent(x.shot)}" alt="${esc(x.label)}の画面" style="width:100%;border:1px solid var(--line);border-radius:8px;margin-top:6px">` : "")
        + (x.parts ? `<details style="margin-top:6px"><summary>画面の部品の一覧（Claudeに送る用）</summary>
            <textarea readonly rows="8" class="parts">${esc(x.parts)}</textarea>
            <button class="small primary" data-copy>コピーする</button></details>` : "")
        + `</div>`).join("");
  } catch (err) {
    $("#chk-out").innerHTML = `<p class="err">${esc(err.message)}</p>`;
  } finally {
    btn.disabled = false;
    btn.textContent = "テストする";
  }
});

$("#chk-out").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-copy]");
  if (!b) return;
  const ta = b.parentElement.querySelector("textarea");
  try {
    await navigator.clipboard.writeText(ta.value);
  } catch {
    ta.select();
    document.execCommand("copy");
  }
  toast("コピーしました。Claudeのチャットに貼り付けてください");
});

// --- 起動 ---
async function start() {
  $("#app").hidden = false;
  try {
    const st = await api("/api/state");
    if (!$("#date").value) $("#date").value = st.today;
    if (!$("#sim-date").value) {
      const y = new Date(st.today);
      y.setDate(y.getDate() - 1);
      $("#sim-date").value = y.toISOString().slice(0, 10);
    }
    await load();
  } catch (err) {
    if (!$("#login").hidden) return;
    toast(err.message);
  }
}
$("#date").addEventListener("change", load);
setInterval(() => { if (!document.hidden && !$("#app").hidden && !$("#dlg").open) load().catch(() => {}); }, 20000);
document.addEventListener("visibilitychange", () => { if (!document.hidden && !$("#app").hidden) load().catch(() => {}); });
if ("serviceWorker" in navigator) navigator.serviceWorker.register("sw.js").catch(() => {});
start();
