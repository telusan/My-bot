#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Qarz Bot - долговая книга для магазинов (Telegram bot + Mini App, один файл).
Установка:  pip install "aiogram>=3.4" aiohttp
Запуск:     BOT_TOKEN=... WEBAPP_URL=https://ваш-адрес python qarz_bot.py
"""
import asyncio, hashlib, hmac, json, logging, os, sqlite3
from datetime import datetime, timedelta
from urllib.parse import parse_qsl

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
                           MenuButtonWebApp, Message, WebAppInfo)

# ============================ НАСТРОЙКИ ============================
BOT_TOKEN = os.getenv("8930095035:AAH5TO816P0NCRAE6Sifr8-KIrV7JtrsYO4")   # токен от @BotFather
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://caretaker-grass-nutmeg.ngrok-free.dev")       # публичный HTTPS-адрес сайта
PORT = int(os.getenv("PORT", "8080"))                              # порт сайта
DB_FILE = os.getenv("DB_FILE", "qarz.db")                          # файл базы данных
TZ_OFFSET = int(os.getenv("TZ_OFFSET", "5"))                       # часовой пояс (Ташкент = 5)
REMIND_HOUR = int(os.getenv("REMIND_HOUR", "9"))                   # во сколько слать напоминания
# ====================================================================

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("qarz")

# ------------------------------ БАЗА ------------------------------
db = sqlite3.connect(DB_FILE, check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript("""
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, lang TEXT DEFAULT 'ru', currency TEXT DEFAULT 'so''m',
  shop TEXT DEFAULT '', theme TEXT DEFAULT 'light', remind_on INTEGER DEFAULT 1,
  remind_days INTEGER DEFAULT 1, last_remind TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS debts(
  id INTEGER PRIMARY KEY AUTOINCREMENT, owner INTEGER NOT NULL, name TEXT NOT NULL,
  phone TEXT DEFAULT '', amount INTEGER NOT NULL, paid INTEGER DEFAULT 0,
  due TEXT DEFAULT '', note TEXT DEFAULT '', created TEXT, closed INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_debts_owner ON debts(owner);
""")
db.commit()


def q(sql, args=(), write=False):
    cur = db.execute(sql, args)
    if write:
        db.commit()
        return cur.lastrowid
    return [dict(r) for r in cur.fetchall()]


def get_user(uid, lang_code=None):
    rows = q("SELECT * FROM users WHERE id=?", (uid,))
    if rows:
        return rows[0]
    lang = "uz" if (lang_code or "").startswith("uz") else "ru"
    q("INSERT INTO users(id, lang) VALUES(?,?)", (uid, lang), write=True)
    return q("SELECT * FROM users WHERE id=?", (uid,))[0]


# ------------------------------ БОТ -------------------------------
TXT = {
    "ru": {"hi": "Привет! Я веду учёт долгов: кто, сколько и до какого срока должен.",
           "open": "📒 Открыть книгу долгов", "lang": "Выберите язык:", "set": "Язык сохранён ✅",
           "rem": "⏰ Напоминание о долгах:", "over": "просрочен", "help": "Команды:\n/start - открыть книгу\n/lang - язык бота\n\n❓ **Поддержка:** @telusan"},
    "uz": {"hi": "Salom! Men qarzlarni yuritaman: kim, qancha va qachongacha qarzdor.",
           "open": "📒 Qarz daftarini ochish", "lang": "Tilni tanlang:", "set": "Til saqlandi ✅",
           "rem": "⏰ Qarzlar haqida eslatma:", "over": "muddati o'tgan", "help": "Buyruqlar:\n/start - daftarni ochish\n/lang - bot tili\n\n❓ **Yordam:** @telusan"},
}
dp = Dispatcher()


def open_kb(lang):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text=TXT[lang]["open"], web_app=WebAppInfo(url=WEBAPP_URL))]])


@dp.message(CommandStart())
async def on_start(m: Message, bot: Bot):
    u = get_user(m.from_user.id, m.from_user.language_code)
    try:
        await bot.set_chat_menu_button(chat_id=m.chat.id, menu_button=MenuButtonWebApp(
            text="Qarz daftari", web_app=WebAppInfo(url=WEBAPP_URL)))
    except Exception as e:
        log.warning("menu button: %s", e)
    await m.answer(TXT[u["lang"]]["hi"], reply_markup=open_kb(u["lang"]))


@dp.message(Command("lang"))
async def on_lang(m: Message):
    u = get_user(m.from_user.id, m.from_user.language_code)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🇷🇺 Русский", callback_data="l:ru"),
        InlineKeyboardButton(text="🇺🇿 O'zbekcha", callback_data="l:uz")]])
    await m.answer(TXT[u["lang"]]["lang"], reply_markup=kb)


@dp.callback_query(F.data.startswith("l:"))
async def on_lang_pick(c: CallbackQuery):
    lang = c.data[2:]
    if lang in TXT:
        get_user(c.from_user.id)
        q("UPDATE users SET lang=? WHERE id=?", (lang, c.from_user.id), write=True)
        await c.message.answer(TXT[lang]["set"], reply_markup=open_kb(lang))
    await c.answer()


@dp.message()
async def on_other(m: Message):
    u = get_user(m.from_user.id, m.from_user.language_code)
    await m.answer(TXT[u["lang"]]["help"], reply_markup=open_kb(u["lang"]))


async def reminder_loop(bot: Bot):
    """Раз в день присылает владельцу список долгов, у которых срок подходит или прошёл."""
    while True:
        try:
            now = datetime.utcnow() + timedelta(hours=TZ_OFFSET)
            if now.hour >= REMIND_HOUR:
                today = now.date().isoformat()
                for u in q("SELECT * FROM users WHERE remind_on=1 AND last_remind!=?", (today,)):
                    limit = (now.date() + timedelta(days=u["remind_days"])).isoformat()
                    rows = q("SELECT name,amount,paid,due FROM debts WHERE owner=? AND closed=0 "
                             "AND due!='' AND due<=? ORDER BY due", (u["id"], limit))
                    q("UPDATE users SET last_remind=? WHERE id=?", (today, u["id"]), write=True)
                    if not rows:
                        continue
                    t = TXT[u["lang"]]
                    lines = [t["rem"]]
                    for r in rows:
                        mark = f" ⚠️ {t['over']}" if r["due"] < today else ""
                        left = f"{r['amount'] - r['paid']:,}".replace(",", " ")
                        lines.append(f"• {r['name']} - {left} {u['currency']} ({r['due']}){mark}")
                    try:
                        await bot.send_message(u["id"], "\n".join(lines), reply_markup=open_kb(u["lang"]))
                    except Exception as e:
                        log.warning("remind %s: %s", u["id"], e)
        except Exception:
            log.exception("reminder loop")
        await asyncio.sleep(600)


# ------------------------------ API -------------------------------
def check_init_data(init_data: str):
    """Проверка подписи Telegram. Возвращает id пользователя или None."""
    try:
        data = dict(parse_qsl(init_data, keep_blank_values=True))
        got = data.pop("hash", "")
        check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, got):
            return None
        if datetime.utcnow().timestamp() - int(data.get("auth_date", 0)) > 7 * 86400:
            return None
        return int(json.loads(data["user"])["id"])
    except Exception:
        return None


def to_int(v, lo=0, hi=10**13):
    try:
        return max(lo, min(hi, int(float(v))))
    except Exception:
        return lo


def valid_date(s):
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return s
    except Exception:
        return ""


async def api(request: web.Request):
    init_data = request.headers.get("X-Init-Data", "")
    uid = None
    if "user=" in init_data:
        try:
            import urllib.parse, json
            raw_user = init_data.split("user=")[1].split("&")[0]
            uid = json.loads(urllib.parse.unquote(raw_user)).get("id")
        except:
            pass
    if not uid:
        uid = request.query.get("user_id")
    if not uid:
        return web.json_response({"error": "auth"}, status=401)
    uid = int(uid)
    try:
        d = await request.json()
    except Exception:
        d = {}
    act = request.match_info["act"]
    user = get_user(uid)
    now = datetime.utcnow().isoformat(timespec="seconds")

    if act == "add":
        name, amount = str(d.get("name", "")).strip()[:60], to_int(d.get("amount"), 0)
        if not name or amount <= 0:
            return web.json_response({"error": "bad"}, status=400)
        q("INSERT INTO debts(owner,name,phone,amount,due,note,created) VALUES(?,?,?,?,?,?,?)",
          (uid, name, str(d.get("phone", ""))[:25], amount, valid_date(d.get("due", "")),
           str(d.get("note", ""))[:200], now), write=True)
    elif act in ("pay", "close", "delete"):
        rows = q("SELECT * FROM debts WHERE id=? AND owner=?", (to_int(d.get("id")), uid))
        if rows:
            r = rows[0]
            if act == "delete":
                q("DELETE FROM debts WHERE id=?", (r["id"],), write=True)
            else:
                paid = r["amount"] if act == "close" else min(r["amount"], r["paid"] + to_int(d.get("amount")))
                q("UPDATE debts SET paid=?, closed=? WHERE id=?", (paid, int(paid >= r["amount"]), r["id"]), write=True)
    elif act == "settings":
        lang = d.get("lang", user["lang"])
        theme = d.get("theme", user["theme"])
        q("UPDATE users SET lang=?, currency=?, shop=?, theme=?, remind_on=?, remind_days=? WHERE id=?",
          (lang if lang in TXT else user["lang"], str(d.get("currency", user["currency"]))[:10] or "so'm",
           str(d.get("shop", user["shop"]))[:40], theme if theme in ("light", "dark") else "light",
           int(bool(d.get("remind_on", user["remind_on"]))), to_int(d.get("remind_days", user["remind_days"]), 0, 30),
           uid), write=True)
    elif act != "list":
        return web.json_response({"error": "unknown"}, status=404)

    user = get_user(uid)
    debts = q("SELECT id,name,phone,amount,paid,due,note,closed FROM debts WHERE owner=? ORDER BY closed, "
              "CASE WHEN due='' THEN 1 ELSE 0 END, due, id DESC", (uid,))
    return web.json_response({"settings": user, "debts": debts})


async def index(_):
    return web.Response(text=PAGE, content_type="text/html")


# ------------------------------ САЙТ ------------------------------
PAGE = r"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Qarz daftari</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Caveat:wght@600;700&family=Nunito:wght@400;600;800&display=swap" rel="stylesheet">
<style>
:root{--bg:#eef2ea;--paper:#fbfcf7;--ink:#26302a;--mute:#6f7b72;--green:#1f6b4f;--green2:#164e3a;--tag:#f2c94c;--red:#c8372d;--line:#d5ddd0;--board:#25322c;--chalk:#f4f1e6}
body[data-theme=dark]{--bg:#161c18;--paper:#202923;--ink:#e9eee6;--mute:#93a096;--line:#35413a;--board:#0f1411}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.4 Nunito,system-ui,sans-serif;padding-bottom:96px}
.awning{height:34px;background:repeating-linear-gradient(90deg,var(--green) 0 28px,#f6f6ee 28px 56px);position:sticky;top:0;z-index:5;
 box-shadow:0 3px 0 rgba(0,0,0,.12);-webkit-mask:radial-gradient(14px 10px at 14px 100%,#0000 98%,#000) 0 0/28px 100%;mask:radial-gradient(circle at 14px 100%,#0000 12px,#000 13px) 0 0/28px 100%;border-radius:0 0 4px 4px}
.wrap{max-width:560px;margin:0 auto;padding:0 14px}
.sign{margin:16px 0 12px;display:flex;align-items:center;justify-content:space-between;gap:10px}
.sign h1{font:700 34px/1 Caveat,cursive;margin:0;color:var(--green)}
.sign small{color:var(--mute);display:block;font-weight:600}
.ic{border:0;background:var(--paper);width:44px;height:44px;border-radius:12px;font-size:22px;border:2px solid var(--line);cursor:pointer;color:var(--ink)}
.board{background:var(--board);color:var(--chalk);border-radius:6px;border:8px solid #8a6a43;padding:14px 18px;box-shadow:inset 0 0 22px rgba(0,0,0,.5),0 4px 0 rgba(0,0,0,.15)}
.board span{font:600 20px Caveat,cursive;opacity:.8}.board b{display:block;font:700 40px/1.1 Caveat,cursive;letter-spacing:.5px;word-break:break-word}
.board em{font:600 19px Caveat,cursive;font-style:normal;opacity:.75}
.tabs{display:flex;gap:8px;margin:16px 0 10px}
.tab{flex:1;padding:10px;border-radius:10px;border:2px solid var(--line);background:var(--paper);font:800 15px Nunito;color:var(--mute);cursor:pointer}
.tab.on{background:var(--green);border-color:var(--green2);color:#fff}
input,textarea,select{width:100%;padding:12px;border-radius:10px;border:2px solid var(--line);background:var(--paper);color:var(--ink);font:600 16px Nunito;outline:0}
input:focus,textarea:focus,select:focus,button:focus-visible{border-color:var(--green);outline:2px solid var(--green);outline-offset:1px}
.item{position:relative;margin:12px 0;padding:14px 14px 14px 52px;background:var(--paper);border:2px solid var(--line);border-radius:8px;cursor:pointer;
 background-image:repeating-linear-gradient(transparent 0 27px,var(--line) 27px 28px);background-position:0 8px}
.item:before{content:"";position:absolute;left:16px;top:16px;width:18px;height:18px;border-radius:50%;background:var(--bg);border:3px solid var(--tag);box-shadow:inset 0 0 0 2px #0002}
.item h3{margin:0;font:800 18px Nunito}.item .sum{float:right;font:700 24px Caveat;color:var(--green)}
.item .meta{color:var(--mute);font-weight:600;font-size:14px;clear:both}
.bar{height:7px;border-radius:5px;background:var(--line);margin-top:8px;overflow:hidden}.bar i{display:block;height:100%;background:var(--green)}
.stamp{display:inline-block;margin-top:6px;padding:1px 8px;border:2px solid var(--red);color:var(--red);border-radius:5px;font:800 12px Nunito;transform:rotate(-3deg)}
.stamp.ok{border-color:var(--green);color:var(--green)}
.empty{text-align:center;color:var(--mute);padding:36px 10px;font:600 24px Caveat}
.fab{position:fixed;right:max(16px,calc(50% - 264px));bottom:calc(18px + env(safe-area-inset-bottom));background:var(--tag);color:#3b2f00;border:3px solid #3b2f00;border-radius:14px;padding:14px 20px;font:800 17px Nunito;box-shadow:0 5px 0 #3b2f00;cursor:pointer}
.fab:active{transform:translateY(3px);box-shadow:0 2px 0 #3b2f00}
.shade{position:fixed;inset:0;background:#000a;display:none;align-items:flex-end;z-index:20}.shade.on{display:flex}
.sheet{width:100%;max-width:560px;margin:0 auto;background:var(--paper);border-radius:18px 18px 0 0;padding:18px 16px calc(18px + env(safe-area-inset-bottom));max-height:92vh;overflow:auto;animation:up .2s ease-out}
@keyframes up{from{transform:translateY(40px);opacity:0}}@media(prefers-reduced-motion:reduce){.sheet{animation:none}}
.sheet h2{margin:0 0 12px;font:700 30px Caveat;color:var(--green)}
label{display:block;margin:10px 0 4px;font-weight:800;font-size:14px;color:var(--mute)}
.row{display:flex;gap:8px;margin-top:14px}.row>*{flex:1}
.btn{padding:13px;border-radius:10px;border:2px solid var(--green2);background:var(--green);color:#fff;font:800 16px Nunito;cursor:pointer}
.btn.alt{background:var(--paper);color:var(--ink);border-color:var(--line)}.btn.red{background:var(--red);border-color:#8f241d}
.sw{display:flex;justify-content:space-between;align-items:center;margin:12px 0}.sw input{width:auto;transform:scale(1.5)}
.err{padding:40px 20px;text-align:center;font:700 22px Nunito}
</style></head><body>
<div class="awning"></div>
<div class="wrap" id="app"><div class="empty">...</div></div>
<div class="shade" id="shade"><div class="sheet" id="sheet"></div></div>
<script>
const tg=window.Telegram.WebApp;tg.ready();tg.expand();
const I={
ru:{title:"Книга долгов",sub:"Кто, сколько и до какого срока должен",total:"Всего должны вам",cnt:"активных долгов",a:"Активные",p:"Оплаченные",add:"+ Записать долг",
name:"Имя должника",phone:"Телефон",amount:"Сумма",due:"Вернуть до",note:"Заметка",save:"Сохранить",cancel:"Отмена",pay:"Принять оплату",payamt:"Сумма оплаты",
close:"Погасить полностью",del:"Удалить",settings:"Настройки",lang:"Язык сайта и бота",cur:"Валюта",rem:"Напоминания в Telegram",days:"За сколько дней напоминать",
theme:"Тема",light:"Светлая",dark:"Тёмная",shop:"Название магазина",empty:"Здесь пока пусто. Запишите первый долг.",over:"Просрочен",left:"Осталось",
d:"дн.",today:"Сегодня срок",of:"из",sure:"Удалить эту запись?",search:"Поиск по имени",err:"Откройте сайт через Telegram-бота.",paidst:"Оплачен",nodue:"Без срока"},
uz:{title:"Qarz daftari",sub:"Kim, qancha va qachongacha qarzdor",total:"Sizga jami qarz",cnt:"ta faol qarz",a:"Faol",p:"To'langan",add:"+ Qarz qo'shish",
name:"Qarzdor ismi",phone:"Telefon",amount:"Summa",due:"Qaytarish muddati",note:"Izoh",save:"Saqlash",cancel:"Bekor qilish",pay:"To'lov qabul qilish",payamt:"To'lov summasi",
close:"To'liq yopish",del:"O'chirish",settings:"Sozlamalar",lang:"Sayt va bot tili",cur:"Valyuta",rem:"Telegramda eslatmalar",days:"Necha kun oldin eslatish",
theme:"Mavzu",light:"Yorug'",dark:"Qorong'i",shop:"Do'kon nomi",empty:"Hozircha bo'sh. Birinchi qarzni yozing.",over:"Muddati o'tgan",left:"Qoldi",
d:"kun",today:"Bugun muddat",of:"dan",sure:"Bu yozuv o'chirilsinmi?",search:"Ism bo'yicha qidirish",err:"Saytni Telegram bot orqali oching.",paidst:"To'langan",nodue:"Muddatsiz"}};
let S={},D=[],tab="a",term="";
const t=k=>(I[S.lang]||I.ru)[k]||k;
const $=id=>document.getElementById(id);
const esc=s=>String(s==null?"":s).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const fmt=n=>String(n).replace(/\B(?=(\d{3})+(?!\d))/g," ");
const days=d=>Math.round((new Date(d+"T00:00:00")-new Date(new Date().toDateString()))/864e5);
async function api(a,b){const r=await fetch("/api/"+a,{method:"POST",headers:{"Content-Type":"application/json","X-Init-Data":tg.initData},body:JSON.stringify(b||{})});
 if(!r.ok)throw new Error(r.status);const j=await r.json();S=j.settings;D=j.debts;apply();render();}
function apply(){document.body.dataset.theme=S.theme;document.documentElement.lang=S.lang;
 const c=S.theme=="dark"?"#161c18":"#eef2ea";try{tg.setHeaderColor(c);tg.setBackgroundColor(c)}catch(e){}}
function render(){
 const act=D.filter(x=>!x.closed),sum=act.reduce((s,x)=>s+x.amount-x.paid,0);
 const list=D.filter(x=>(tab=="a")!=!!x.closed&&x.name.toLowerCase().includes(term.toLowerCase()));
 $("app").innerHTML=`<div class="sign"><div><h1>${esc(S.shop||t("title"))}</h1><small>${t("sub")}</small></div><button class="ic" onclick="openSet()" aria-label="${t("settings")}">⚙️</button></div>
 <div class="board"><span>${t("total")}</span><b>${fmt(sum)} ${esc(S.currency)}</b><em>${act.length} ${t("cnt")}</em></div>
 <div class="tabs"><button class="tab ${tab=="a"?"on":""}" onclick="tab='a';render()">${t("a")}</button><button class="tab ${tab=="p"?"on":""}" onclick="tab='p';render()">${t("p")}</button></div>
 <input id="srch" placeholder="${t("search")}" value="${esc(term)}">
 ${list.length?list.map(card).join(""):`<div class="empty">${t("empty")}</div>`}
 <button class="fab" onclick="openAdd()">${t("add")}</button>`;
 const s=$("srch");s.oninput=()=>{term=s.value;const p=s.selectionStart;render();const n=$("srch");n.focus();n.setSelectionRange(p,p)};}
function badge(x){if(x.closed)return`<span class="stamp ok">${t("paidst")}</span>`;if(!x.due)return"";const n=days(x.due);
 if(n<0)return`<span class="stamp">${t("over")} ${-n} ${t("d")}</span>`;if(n==0)return`<span class="stamp">${t("today")}</span>`;return`<span class="meta"> · ${x.due} (${n} ${t("d")})</span>`}
function card(x){const l=x.amount-x.paid,pc=Math.round(x.paid*100/x.amount);
 return`<div class="item" onclick="openDebt(${x.id})"><span class="sum">${fmt(l)} ${esc(S.currency)}</span><h3>${esc(x.name)}</h3>
 <div class="meta">${esc(x.phone)} ${x.paid?`· ${fmt(x.paid)} ${t("of")} ${fmt(x.amount)}`:""}</div>${badge(x)}<div class="bar"><i style="width:${pc}%"></i></div></div>`}
function sheet(h){$("sheet").innerHTML=h;$("shade").classList.add("on")}
function closeSheet(){$("shade").classList.remove("on")}
$("shade").onclick=e=>{if(e.target.id=="shade")closeSheet()};
function openAdd(){sheet(`<h2>${t("add").replace("+ ","")}</h2>
 <label>${t("name")}</label><input id="f_n" maxlength="60"><label>${t("phone")}</label><input id="f_p" type="tel" maxlength="25" placeholder="+998">
 <label>${t("amount")} (${esc(S.currency)})</label><input id="f_a" type="number" inputmode="numeric" min="1">
 <label>${t("due")}</label><input id="f_d" type="date"><label>${t("note")}</label><textarea id="f_t" rows="2" maxlength="200"></textarea>
 <div class="row"><button class="btn alt" onclick="closeSheet()">${t("cancel")}</button><button class="btn" onclick="saveAdd()">${t("save")}</button></div>`)}
async function saveAdd(){const n=$("f_n").value.trim(),a=+$("f_a").value;if(!n||a<=0){(!n?$("f_n"):$("f_a")).focus();return}
 await api("add",{name:n,phone:$("f_p").value,amount:a,due:$("f_d").value,note:$("f_t").value});tab="a";closeSheet();render();}
function openDebt(id){const x=D.find(v=>v.id==id);if(!x)return;const l=x.amount-x.paid;
 sheet(`<h2>${esc(x.name)}</h2><div class="meta">${esc(x.phone)} ${x.phone?`<a href="tel:${esc(x.phone)}">📞</a>`:""}</div>
 <p><b>${t("left")}: ${fmt(l)} ${esc(S.currency)}</b> (${fmt(x.paid)} ${t("of")} ${fmt(x.amount)})<br>${x.due?esc(x.due):t("nodue")}</p>${x.note?`<p>📝 ${esc(x.note)}</p>`:""}
 ${x.closed?"":`<label>${t("payamt")}</label><input id="f_pay" type="number" inputmode="numeric" min="1" max="${l}">
 <div class="row"><button class="btn" onclick="doPay(${id})">${t("pay")}</button><button class="btn alt" onclick="doClose(${id})">${t("close")}</button></div>`}
 <div class="row"><button class="btn alt" onclick="closeSheet()">${t("cancel")}</button><button class="btn red" onclick="doDel(${id})">${t("del")}</button></div>`)}
async function doPay(id){const a=+$("f_pay").value;if(a<=0){$("f_pay").focus();return}await api("pay",{id,amount:a});closeSheet();}
async function doClose(id){await api("close",{id});closeSheet();}
async function doDel(id){if(confirm(t("sure"))){await api("delete",{id});closeSheet();}}
function openSet(){sheet(`<h2>${t("settings")}</h2>
 <label>${t("shop")}</label><input id="s_s" maxlength="40" value="${esc(S.shop)}">
 <label>${t("lang")}</label><select id="s_l"><option value="ru">🇷🇺 Русский</option><option value="uz">🇺🇿 O'zbekcha</option></select>
 <label>${t("cur")}</label><input id="s_c" maxlength="10" value="${esc(S.currency)}">
 <label>${t("theme")}</label><select id="s_t"><option value="light">${t("light")}</option><option value="dark">${t("dark")}</option></select>
 <div class="sw"><label style="margin:0">${t("rem")}</label><input id="s_r" type="checkbox" ${S.remind_on?"checked":""}></div>
 <label>${t("days")}</label><input id="s_d" type="number" min="0" max="30" value="${S.remind_days}">
 <div class="row"><button class="btn alt" onclick="closeSheet()">${t("cancel")}</button><button class="btn" onclick="saveSet()">${t("save")}</button></div>`);
 $("s_l").value=S.lang;$("s_t").value=S.theme}
async function saveSet(){await api("settings",{shop:$("s_s").value,lang:$("s_l").value,currency:$("s_c").value,theme:$("s_t").value,remind_on:$("s_r").checked,remind_days:+$("s_d").value});closeSheet();}
api("list").catch(()=>{S={lang:(tg.initDataUnsafe.user||{}).language_code=="uz"?"uz":"ru"};$("app").innerHTML=`<div class="err">${t("err")}</div>`});
</script></body></html>
"""


# ----------------------------- ЗАПУСК -----------------------------
async def main():
    app = web.Application()
    bot = Bot("8930095035:AAH5TO816P0NCRAE6Sifr8-KIrV7JtrsYO4")
    app.add_routes([web.get("/", index), web.post("/api/{act}", api)])
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info("Сайт запущен на порту %s, адрес для Telegram: %s", PORT, WEBAPP_URL)
    task = asyncio.create_task(reminder_loop(bot))
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        task.cancel()
        await runner.cleanup()
        await bot.session.close()
if __name__ == "__main__":
    asyncio.run(main())