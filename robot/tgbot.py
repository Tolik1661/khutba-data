#!/usr/bin/env python3
"""Telegram-бот админки: черновики с кнопками, команды и оповещения.

Что умеет:
  • присылает каждый новый черновик карточкой со звуком и кнопками
    «Опубликовать», «Изменить», «Удалить»;
  • «Изменить» — правка названия, лектора, даты и раздела прямо в чате;
  • команды /drafts, /recent, /sources, /status, /run, /help;
  • оповещения о сбоях робота, о нехватке места и о том, что давно нет новой хутбы.

Бот отвечает только чатам из TG_ADMIN_CHAT_IDS. Остальным он лишь сообщает их chat id.

Запуск:  python robot/tgbot.py [--linger СЕКУНД]
Секреты: TG_BOT_TOKEN, TG_ADMIN_CHAT_IDS (через запятую); для записи в репозиторий —
GITHUB_TOKEN и GITHUB_REPOSITORY (на GitHub Actions подставляются сами).

Только стандартная библиотека: бот запускается часто и не должен ничего устанавливать.
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KHUTBA_JSON = ROOT / "khutba.json"
DRAFTS_JSON = ROOT / "drafts.json"
STATE_JSON = ROOT / "robot" / "state.json"
SOURCES_JSON = ROOT / "robot" / "sources.json"
AUDIO_DIR = ROOT / "audio"
PAGES_BASE = "https://Tolik1661.github.io/khutba-data"
ROBOT_WORKFLOW = "robot.yml"
STORAGE_LIMIT_MB = 1000     # лимит GitHub Pages

SECTION = {"khutba": "Хутбы", "lectures": "Лекции"}
FIELD = {"t": ("title", "название"), "s": ("speaker", "лектора"), "d": ("date", "дату (ДД.ММ.ГГГГ)")}
EDIT_MARK = re.compile(r"⚙️ edit:([tsd]):([\w-]+):(\d+)")

HELP = """<b>Знания — управление</b>

Новые записи робот присылает сюда карточкой со звуком. Под карточкой кнопки:
✅ опубликовать в приложении · ✏️ изменить · 🗑 удалить.

Кнопки меню внизу экрана:
📥 Черновики — записи, ждущие решения
📗 Опубликованные — последние записи в приложении, их можно снять с публикации
📡 Источники — включить, выключить, автопубликация
📊 Состояние — когда работал робот, сколько записей и места
🔄 Проверить сейчас — искать новые записи, не дожидаясь расписания

То же самое доступно командами: /drafts /recent /sources /status /run /help"""

# Постоянное меню внизу экрана: текст кнопки → команда
MENU_BUTTONS = {"📥 Черновики": "/drafts", "📗 Опубликованные": "/recent", "📡 Источники": "/sources",
                "📊 Состояние": "/status", "🔄 Проверить сейчас": "/run", "❓ Справка": "/help"}
MENU = {"keyboard": [[{"text": "📥 Черновики"}, {"text": "📗 Опубликованные"}],
                     [{"text": "📡 Источники"}, {"text": "📊 Состояние"}],
                     [{"text": "🔄 Проверить сейчас"}, {"text": "❓ Справка"}]],
        "resize_keyboard": True, "is_persistent": True}


def quick(*names: str) -> list:
    """Кнопки-подсказки под ответом бота: следующий вероятный шаг в одно нажатие."""
    labels = {"drafts": "📥 Черновики", "recent": "📗 Опубликованные", "sources": "📡 Источники",
              "status": "📊 Состояние", "run": "🔄 Проверить сейчас"}
    return [[{"text": labels[n], "callback_data": f"m:{n}"} for n in names]]


def log(msg: str) -> None:
    print(msg, flush=True)


class TgError(Exception):
    pass


# ── Настройки ───────────────────────────────────────────────────────────────

def admins() -> set[int]:
    out = set()
    for part in re.split(r"[,\s]+", os.environ.get("TG_ADMIN_CHAT_IDS", "")):
        if re.fullmatch(r"-?\d+", part):
            out.add(int(part))
    return out


def enabled() -> bool:
    return bool(os.environ.get("TG_BOT_TOKEN") and admins())


# ── Bot API ─────────────────────────────────────────────────────────────────

def call(method: str, params: dict | None = None, file: tuple[str, Path] | None = None, timeout: int = 60):
    """Запрос к Bot API. Токен в тексты ошибок не попадает."""
    url = f"https://api.telegram.org/bot{os.environ['TG_BOT_TOKEN']}/{method}"
    params = {k: v for k, v in (params or {}).items() if v is not None}
    if file:
        boundary = uuid.uuid4().hex
        body = bytearray()
        for k, v in params.items():
            v = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
            body += f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
        field, path = file
        body += (f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="{path.name}"\r\n'
                 f"Content-Type: audio/mpeg\r\n\r\n").encode()
        body += path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(url, data=bytes(body),
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    else:
        req = urllib.request.Request(url, data=json.dumps(params).encode(),
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            desc = json.loads(e.read()).get("description", "")
        except Exception:
            desc = ""
        raise TgError(f"{method}: {e.code} {desc}") from None
    except urllib.error.URLError as e:
        raise TgError(f"{method}: сеть недоступна ({e.reason})") from None
    if not data.get("ok"):
        raise TgError(f"{method}: {data.get('description')}")
    return data["result"]


def send(chat_id: int, text: str, keyboard: list | None = None, reply_markup: dict | None = None):
    markup = reply_markup or ({"inline_keyboard": keyboard} if keyboard else None)
    return call("sendMessage", {"chat_id": chat_id, "text": text[:4000], "parse_mode": "HTML",
                                "disable_web_page_preview": True, "reply_markup": markup})


def alert(text: str) -> None:
    """Сообщение всем админам. Сбой отправки не должен ронять вызывающий код."""
    if not enabled():
        return
    for chat in admins():
        try:
            send(chat, text)
        except TgError as e:
            log(f"  ! оповещение не отправлено: {e}")


def edit_card(chat_id: int, message_id: int, text: str, keyboard: list | None = None) -> None:
    """Карточка бывает со звуком (тогда у неё подпись) и без (обычный текст)."""
    markup = {"inline_keyboard": keyboard or []}
    try:
        call("editMessageCaption", {"chat_id": chat_id, "message_id": message_id, "caption": text[:1000],
                                    "parse_mode": "HTML", "reply_markup": markup})
    except TgError as e:
        if "not modified" in str(e):
            return
        try:
            call("editMessageText", {"chat_id": chat_id, "message_id": message_id, "text": text[:4000],
                                     "parse_mode": "HTML", "disable_web_page_preview": True,
                                     "reply_markup": markup})
        except TgError as e2:
            if "not modified" not in str(e2):
                log(f"  ! карточка не обновлена: {e2}")


# ── Данные ──────────────────────────────────────────────────────────────────

def read_json(path: Path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_data() -> dict:
    data = read_json(KHUTBA_JSON, {"khutba": [], "lectures": []})
    data.setdefault("khutba", [])
    data.setdefault("lectures", [])
    return data


def load_drafts() -> dict:
    d = read_json(DRAFTS_JSON, {"drafts": []})
    return {"drafts": d.get("drafts", [])}


def section_of(draft: dict) -> str:
    return "khutba" if draft.get("kind") == "khutba" else "lectures"


def fmt_date(iso: str) -> str:
    try:
        y, m, d = iso.split("-")
        return f"{d}.{m}.{y}"
    except ValueError:
        return iso


def parse_date(text: str) -> str | None:
    text = text.strip()
    m = re.fullmatch(r"(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})", text)
    if m:
        d, mth, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    else:
        m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
        if not m:
            return None
        y, mth, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        return dt.date(y, mth, d).isoformat()
    except ValueError:
        return None


def audio_rel(url: str) -> str | None:
    """Путь файла в репозитории по ссылке, только если это наш audio/*.mp3."""
    m = re.fullmatch(re.escape(PAGES_BASE) + r"/(audio/[\w.-]+\.mp3)", url or "", flags=re.I)
    return m.group(1) if m else None


# ── Запись в репозиторий ────────────────────────────────────────────────────

def git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, check=check)


def git_on() -> bool:
    return os.environ.get("BOT_NO_GIT") != "1"


def git_pull() -> None:
    if git_on():
        git("pull", "--rebase", "-q", "origin", "main", check=False)


def git_sync(message: str, remove: list[str] | None = None) -> bool:
    """Коммитит и отправляет правку. При неудаче откатывается к версии с GitHub."""
    if not git_on():
        return True
    try:
        for rel in remove or []:
            git("rm", "-q", "--cached", "--sparse", "--ignore-unmatch", "--", rel)
            (ROOT / rel).unlink(missing_ok=True)
        git("add", "--sparse", "khutba.json", "drafts.json", "robot/sources.json")
        if git("diff", "--cached", "--quiet", check=False).returncode == 0:
            return True
        git("-c", "user.name=khutba-bot", "-c", "user.email=41898282+github-actions[bot]@users.noreply.github.com",
            "commit", "-q", "-m", message)
        git("pull", "--rebase", "-q", "origin", "main")
        token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
        target = f"https://x-access-token:{token}@github.com/{repo}.git" if token and repo else "origin"
        git("push", "-q", target, "HEAD:main")
        return True
    except subprocess.CalledProcessError as e:
        tail = ((e.stderr or "").strip().splitlines() or ["git"])[-1]
        log("  ! не удалось сохранить: " + tail[:200])
        git("rebase", "--abort", check=False)
        git("fetch", "-q", "origin", "main", check=False)
        git("reset", "-q", "--hard", "origin/main", check=False)
        return False


# ── Карточки ────────────────────────────────────────────────────────────────

def card_text(draft: dict, header: str | None = None) -> str:
    kind = "🕌 Хутба" if draft.get("kind") == "khutba" else "🎓 Лекция"
    mins = f" · {round(draft['duration'] / 60)} мин" if draft.get("duration") else ""
    lines = [header or f"{kind} · черновик",
             f"<b>{html.escape(draft.get('title', ''))}</b>",
             f"{html.escape(draft.get('speaker', ''))} · {fmt_date(draft.get('date', ''))}{mins}"]
    if header is None:
        if draft.get("confidence") == "low":
            lines.append(f"⚠️ Проверьте внимательно: {html.escape(draft.get('reason', ''))}")
        src = draft.get("source_url", "")
        if src.startswith("https://"):
            lines.append(f'<a href="{html.escape(src, quote=True)}">Исходный пост</a>')
    return "\n".join(lines)


def kb_main(did: str) -> list:
    return [[{"text": "✅ Опубликовать", "callback_data": f"pub:{did}"}],
            [{"text": "✏️ Изменить", "callback_data": f"edit:{did}"},
             {"text": "🗑 Удалить", "callback_data": f"del:{did}"}]]


def kb_edit(draft: dict) -> list:
    did = draft["id"]
    other = "Лекции" if draft.get("kind") == "khutba" else "Хутбы"
    return [[{"text": "Название", "callback_data": f"ef:t:{did}"},
             {"text": "Лектор", "callback_data": f"ef:s:{did}"}],
            [{"text": "Дата", "callback_data": f"ef:d:{did}"},
             {"text": f"Перенести в «{other}»", "callback_data": f"ek:{did}"}],
            [{"text": "← Назад", "callback_data": f"back:{did}"}]]


def send_card(chat_id: int, draft: dict, local_file: Path | None = None) -> None:
    """Карточка черновика со звуком. Файл загружаем сами, если он есть под рукой
    (робот шлёт карточку раньше, чем файл появится на сайте); иначе даём ссылку."""
    text, markup = card_text(draft), {"inline_keyboard": kb_main(draft["id"])}
    base = {"chat_id": chat_id, "caption": text[:1000], "parse_mode": "HTML", "reply_markup": markup,
            "title": draft.get("title", "")[:60], "performer": draft.get("speaker", "")[:60],
            "duration": draft.get("duration") or None}
    try:
        if local_file and local_file.exists() and local_file.stat().st_size < 49_000_000:
            call("sendAudio", base, file=("audio", local_file), timeout=300)
            return
        if audio_rel(draft.get("audio_url", "")):
            call("sendAudio", {**base, "audio": draft["audio_url"]}, timeout=120)
            return
    except TgError as e:
        log(f"  ! звук в карточку не попал: {e}")
    send(chat_id, text + f'\n<a href="{html.escape(draft.get("audio_url", ""), quote=True)}">Прослушать</a>',
         kb_main(draft["id"]))


def notify_draft(draft: dict, local_file: Path | None = None) -> bool:
    if not enabled():
        return False
    ok = False
    for chat in admins():
        try:
            send_card(chat, draft, local_file)
            ok = True
        except TgError as e:
            log(f"  ! карточка не отправлена: {e}")
    return ok


def notify_published(record: dict, kind: str) -> None:
    if not enabled():
        return
    text = card_text({**record, "kind": kind}, header=f"✅ Опубликовано автоматически · {SECTION['khutba' if kind == 'khutba' else 'lectures']}")
    for chat in admins():
        try:
            send(chat, text, [[{"text": "Снять с публикации", "callback_data": f"unp:{record['id']}"}]])
        except TgError as e:
            log(f"  ! сообщение не отправлено: {e}")


# ── Действия ────────────────────────────────────────────────────────────────

def find_draft(drafts: dict, did: str) -> dict | None:
    return next((d for d in drafts["drafts"] if d.get("id") == did), None)


def publish(did: str) -> tuple[bool, str]:
    drafts, data = load_drafts(), load_data()
    draft = find_draft(drafts, did)
    if not draft:
        return False, "Эта запись уже обработана."
    key, date = section_of(draft), draft.get("date", "")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) or not draft.get("title", "").strip() or not draft.get("speaker", "").strip():
        return False, "У записи не заполнены название, лектор или дата — нажмите «Изменить»."
    if not audio_rel(draft.get("audio_url", "")):
        return False, "У записи нет аудиофайла."
    ts = re.sub(r"\D", "", did.rsplit("_", 1)[-1]) or str(int(time.time() * 1000))
    record = {"id": f"{'khutba' if key == 'khutba' else 'lecture'}_{date.replace('-', '')}_{ts}",
              "date": date, "date_display": fmt_date(date), "title": draft["title"].strip(),
              "speaker": draft["speaker"].strip(), "audio_url": draft["audio_url"],
              "duration": int(draft.get("duration") or 0)}
    if any(r.get("id") == record["id"] for r in data[key]):
        return False, "Такая запись уже опубликована."
    data[key].insert(0, record)
    data["last_updated"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    drafts["drafts"] = [d for d in drafts["drafts"] if d.get("id") != did]
    write_json(KHUTBA_JSON, data)
    write_json(DRAFTS_JSON, drafts)
    if not git_sync(f"Бот: опубликовано «{record['title'][:60]}»"):
        return False, "Не удалось сохранить на GitHub. Попробуйте ещё раз через минуту."
    return True, card_text({**record, "kind": draft.get("kind")}, header=f"✅ Опубликовано · {SECTION[key]}")


def delete_draft(did: str) -> tuple[bool, str]:
    drafts = load_drafts()
    draft = find_draft(drafts, did)
    if not draft:
        return False, "Эта запись уже обработана."
    drafts["drafts"] = [d for d in drafts["drafts"] if d.get("id") != did]
    write_json(DRAFTS_JSON, drafts)
    rel = audio_rel(draft.get("audio_url", ""))
    if not git_sync(f"Бот: черновик удалён «{draft.get('title', '')[:60]}»", remove=[rel] if rel else None):
        return False, "Не удалось сохранить на GitHub. Попробуйте ещё раз через минуту."
    return True, card_text(draft, header="🗑 Удалено")


def update_draft(did: str, field: str, value: str) -> tuple[dict | None, str]:
    drafts = load_drafts()
    draft = find_draft(drafts, did)
    if not draft:
        return None, "Эта запись уже обработана."
    if field == "date":
        iso = parse_date(value)
        if not iso:
            return None, "Не понял дату. Пришлите в виде 13.11.2026."
        draft["date"] = iso
        draft["date_display"] = fmt_date(iso)
    elif field == "kind":
        draft["kind"] = "lecture" if draft.get("kind") == "khutba" else "khutba"
    else:
        value = re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f]", " ", value)).strip()
        if not value or len(value) > 200:
            return None, "Нужен текст от 1 до 200 знаков."
        draft[field] = value
    draft["confidence"] = "high"   # человек посмотрел и поправил
    write_json(DRAFTS_JSON, drafts)
    if not git_sync(f"Бот: правка черновика «{draft.get('title', '')[:60]}»"):
        return None, "Не удалось сохранить на GitHub. Попробуйте ещё раз через минуту."
    return draft, "Сохранено."


def unpublish(rid: str) -> tuple[bool, str]:
    data = load_data()
    for key in ("khutba", "lectures"):
        rec = next((r for r in data[key] if r.get("id") == rid), None)
        if rec:
            data[key] = [r for r in data[key] if r.get("id") != rid]
            data["last_updated"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
            write_json(KHUTBA_JSON, data)
            rel = audio_rel(rec.get("audio_url", ""))
            if not git_sync(f"Бот: снято с публикации «{rec.get('title', '')[:60]}»", remove=[rel] if rel else None):
                return False, "Не удалось сохранить на GitHub. Попробуйте ещё раз через минуту."
            return True, card_text({**rec, "kind": "khutba" if key == "khutba" else "lecture"},
                                   header=f"🚫 Снято с публикации · {SECTION[key]}")
    return False, "Этой записи уже нет в приложении."


# ── Команды ─────────────────────────────────────────────────────────────────

def cmd_drafts(chat: int) -> None:
    items = load_drafts()["drafts"]
    if not items:
        send(chat, "Черновиков нет.", quick("run", "recent"))
        return
    send(chat, f"Черновиков: {len(items)}")
    for d in items[:15]:
        send_card(chat, d)
    if len(items) > 15:
        send(chat, f"Показаны первые 15 из {len(items)}.")


def cmd_recent(chat: int) -> None:
    data = load_data()
    shown = 0
    for key in ("khutba", "lectures"):
        for rec in data[key][:4]:
            kind = "khutba" if key == "khutba" else "lecture"
            send(chat, card_text({**rec, "kind": kind}, header=f"📗 В приложении · {SECTION[key]}"),
                 [[{"text": "Снять с публикации", "callback_data": f"unp:{rec['id']}"}]])
            shown += 1
    if not shown:
        send(chat, "В приложении пока нет записей.", quick("drafts"))


def sources_view() -> tuple[str, list]:
    srcs = read_json(SOURCES_JSON, {"sources": []})["sources"]
    lines, kb = ["<b>Источники</b>"], []
    for s in srcs:
        on = s.get("enabled", True)
        auto = s.get("auto_publish", False)
        lines.append(f"\n{'🟢' if on else '⚪️'} <b>{html.escape(s.get('title') or s['id'])}</b>\n"
                     f"Лектор: {html.escape(s.get('speaker', ''))}\n"
                     f"Новые записи: {'сразу в приложение' if auto else 'в черновики'}")
        kb.append([{"text": f"{'Выключить' if on else 'Включить'}: {s['id']}", "callback_data": f"son:{s['id']}"},
                   {"text": "→ в черновики" if auto else "→ сразу публиковать", "callback_data": f"sau:{s['id']}"}])
    return "\n".join(lines), kb


def toggle_source(sid: str, field: str) -> bool:
    cfg = read_json(SOURCES_JSON, {"sources": []})
    src = next((s for s in cfg["sources"] if s["id"] == sid), None)
    if not src:
        return False
    src[field] = not src.get(field, field == "enabled")
    SOURCES_JSON.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return git_sync(f"Бот: источник {sid}, {field} = {src[field]}")


def cmd_status(chat: int) -> None:
    data, drafts = load_data(), load_drafts()
    state = read_json(STATE_JSON, {})
    run = state.get("last_run", {})
    lines = ["<b>Состояние</b>",
             f"В приложении: {len(data['khutba'])} хутб, {len(data['lectures'])} лекций",
             f"Черновиков: {len(drafts['drafts'])}"]
    if run:
        lines.append(f"Робот работал: {html.escape(run.get('at', '—'))} UTC — {html.escape(run.get('summary', ''))}")
    else:
        lines.append("Робот ещё не запускался.")
    if "storage_mb" in state:
        lines.append(f"Место под аудио: {state['storage_mb']} из {STORAGE_LIMIT_MB} МБ")
    for sid, s in state.get("sources", {}).items():
        if s.get("error"):
            lines.append(f"⚠️ {html.escape(sid)}: {html.escape(s['error'])}")
    send(chat, "\n".join(lines), quick("drafts", "run") + quick("sources", "recent"))


def cmd_run(chat: int) -> None:
    token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        send(chat, "Запуск доступен только когда бот работает на GitHub.")
        return
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/actions/workflows/{ROBOT_WORKFLOW}/dispatches",
        data=json.dumps({"ref": "main"}).encode(), method="POST",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
    try:
        urllib.request.urlopen(req, timeout=30).read()
        send(chat, "Запустил проверку источников. Новые записи придут сюда через несколько минут.",
             quick("status", "drafts"))
    except urllib.error.HTTPError as e:
        send(chat, f"Не удалось запустить робота (ошибка {e.code}).")


def dispatch(chat: int, cmd: str) -> bool:
    if cmd in ("/start", "/help"):
        send(chat, HELP, reply_markup=MENU)
    elif cmd == "/drafts":
        cmd_drafts(chat)
    elif cmd == "/recent":
        cmd_recent(chat)
    elif cmd == "/sources":
        text, kb = sources_view()
        send(chat, text, kb)
    elif cmd == "/status":
        cmd_status(chat)
    elif cmd == "/run":
        cmd_run(chat)
    else:
        return False
    return True


# ── Обработка входящих ──────────────────────────────────────────────────────

def answer(cq_id: str, text: str = "") -> None:
    try:
        call("answerCallbackQuery", {"callback_query_id": cq_id, "text": text[:190] or None})
    except TgError:
        pass   # кнопку нажали давно, Telegram уже не ждёт ответа


def on_callback(cq: dict) -> None:
    msg = cq.get("message") or {}
    chat, mid = (msg.get("chat") or {}).get("id"), msg.get("message_id")
    data = cq.get("data", "")
    if chat not in admins():
        answer(cq["id"], "Нет доступа")
        return
    if data.startswith("m:"):
        answer(cq["id"])
        dispatch(chat, "/" + data[2:])
        return
    m = re.fullmatch(r"(pub|del|delok|edit|back|ek|unp|unpok|unpno|son|sau):([\w-]+)|ef:([tsd]):([\w-]+)", data)
    if not m:
        answer(cq["id"])
        return
    action, target = (m.group(1), m.group(2)) if m.group(1) else ("ef", m.group(4))
    answer(cq["id"])

    if action in ("son", "sau"):
        ok = toggle_source(target, "enabled" if action == "son" else "auto_publish")
        text, kb = sources_view()
        edit_card(chat, mid, text if ok else text + "\n\n⚠️ Не удалось сохранить.", kb)
        return
    if action == "unp":
        call("editMessageReplyMarkup", {"chat_id": chat, "message_id": mid, "reply_markup": {"inline_keyboard": [[
            {"text": "Да, снять", "callback_data": f"unpok:{target}"},
            {"text": "Отмена", "callback_data": f"unpno:{target}"}]]}})
        return
    if action == "unpno":
        call("editMessageReplyMarkup", {"chat_id": chat, "message_id": mid, "reply_markup": {"inline_keyboard": [[
            {"text": "Снять с публикации", "callback_data": f"unp:{target}"}]]}})
        return
    if action == "unpok":
        ok, text = unpublish(target)
        edit_card(chat, mid, text) if ok else send(chat, text)
        return

    draft = find_draft(load_drafts(), target)
    if not draft:
        call("editMessageReplyMarkup", {"chat_id": chat, "message_id": mid, "reply_markup": {"inline_keyboard": []}})
        send(chat, "Эта запись уже обработана.")
        return
    if action == "pub":
        ok, text = publish(target)
        edit_card(chat, mid, text) if ok else send(chat, text)
    elif action == "del":
        edit_card(chat, mid, card_text(draft), [[{"text": "Да, удалить", "callback_data": f"delok:{target}"},
                                                {"text": "Отмена", "callback_data": f"back:{target}"}]])
    elif action == "delok":
        ok, text = delete_draft(target)
        edit_card(chat, mid, text) if ok else send(chat, text)
    elif action == "edit":
        edit_card(chat, mid, card_text(draft), kb_edit(draft))
    elif action == "back":
        edit_card(chat, mid, card_text(draft), kb_main(target))
    elif action == "ek":
        new, text = update_draft(target, "kind", "")
        edit_card(chat, mid, card_text(new), kb_edit(new)) if new else send(chat, text)
    elif action == "ef":
        code = m.group(3)
        key, what = FIELD[code]
        send(chat, f"✏️ Пришлите новое {what} ответом на это сообщение.\n"
                   f"Сейчас: <code>{html.escape(fmt_date(draft[key]) if key == 'date' else draft.get(key, ''))}</code>\n\n"
                   f"⚙️ edit:{code}:{target}:{mid}",
             reply_markup={"force_reply": True, "input_field_placeholder": f"Новое {what}"})


def on_message(msg: dict) -> None:
    chat = (msg.get("chat") or {}).get("id")
    text = (msg.get("text") or "").strip()
    if chat not in admins():
        if (msg.get("chat") or {}).get("type") == "private":
            send(chat, f"Это служебный бот. Ваш chat id: <code>{chat}</code>\n"
                       "Чтобы получить доступ, владелец должен добавить его в настройку TG_ADMIN_CHAT_IDS.")
        return

    reply = msg.get("reply_to_message") or {}
    mark = EDIT_MARK.search(reply.get("text") or "")
    if mark and text and not text.startswith("/"):
        code, did, card_mid = mark.group(1), mark.group(2), int(mark.group(3))
        draft, note = update_draft(did, FIELD[code][0], text)
        if draft:
            edit_card(chat, card_mid, card_text(draft), kb_main(did))
            send(chat, "Сохранено. Карточка выше обновлена — можно публиковать.", quick("drafts"))
        else:
            send(chat, note)
        return

    cmd = MENU_BUTTONS.get(text) or (text.split()[0].split("@")[0].lower() if text.startswith("/") else "")
    if not dispatch(chat, cmd):
        send(chat, "Не понял. Пользуйтесь кнопками меню внизу. Чтобы изменить запись, нажмите «Изменить» "
                   "под карточкой и ответьте на сообщение бота.", reply_markup=MENU)


def handle(update: dict) -> None:
    try:
        if "callback_query" in update:
            on_callback(update["callback_query"])
        elif "message" in update:
            on_message(update["message"])
    except TgError as e:
        log(f"  ! {e}")
    except Exception as e:   # один сбойный апдейт не должен останавливать бота
        log(f"  ! {type(e).__name__}: {str(e)[:200]}")


def serve(linger: int, max_total: int = 1200) -> int:
    """Обрабатывает накопившиеся нажатия и сообщения. Если они были, остаётся на связи,
    пока не пройдёт linger секунд тишины: правка идёт диалогом, ждать следующего запуска неудобно."""
    if not enabled():
        log("Бот не настроен: нужны TG_BOT_TOKEN и TG_ADMIN_CHAT_IDS")
        return 0
    started = time.time()
    quiet_until = started          # первая проверка — без ожидания
    offset, handled = None, 0
    while True:
        wait = max(0, min(25, int(quiet_until - time.time())))
        updates = call("getUpdates", {"timeout": wait, "offset": offset,
                                      "allowed_updates": ["message", "callback_query"]}, timeout=wait + 20)
        if updates:
            git_pull()
            for u in updates:
                offset = u["update_id"] + 1
                handle(u)
                handled += 1
            quiet_until = time.time() + linger
        if time.time() >= quiet_until or time.time() - started > max_total:
            break
    if offset is not None:
        call("getUpdates", {"timeout": 0, "offset": offset})   # подтверждаем обработанное
    log(f"обработано событий: {handled}")
    return 0


def setup_menu() -> None:
    call("setMyCommands", {"commands": [
        {"command": "drafts", "description": "Черновики, ждущие решения"},
        {"command": "recent", "description": "Последние опубликованные"},
        {"command": "sources", "description": "Источники и автопубликация"},
        {"command": "status", "description": "Состояние робота и место"},
        {"command": "run", "description": "Проверить источники сейчас"},
        {"command": "help", "description": "Справка"}]})


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Telegram-бот админки хутб и лекций")
    ap.add_argument("--linger", type=int, default=0, help="сколько секунд оставаться на связи после последнего события")
    ap.add_argument("--setup", action="store_true", help="записать меню команд в Telegram и выйти")
    a = ap.parse_args()
    if a.setup:
        setup_menu()
        log("меню команд обновлено")
        sys.exit(0)
    sys.exit(serve(a.linger))
