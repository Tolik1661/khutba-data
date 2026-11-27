#!/usr/bin/env python3
"""Разовый импорт истории канала из выгрузки Telegram Desktop (папка с result.json и файлами).

Шаг 1 — разбор (ничего не публикует):
    python robot/import_export.py plan "<папка выгрузки>" --source shkhanukov
    → robot/import/<source>-plan.json и <source>-plan.md (таблица для проверки)
Шаг 2 — применение после проверки таблицы:
    python robot/import_export.py apply --source shkhanukov [--audio-dir audio] [--base-url …]
    → сжимает звук в mp3 32 кбит/с и добавляет записи в khutba.json

Подписи разбирает та же модель и по тем же правилам, что и робот (robot.py → PROMPT).
Ключ — ANTHROPIC_API_KEY.
"""
import argparse, datetime as dt, json, os, re, sys, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import robot  # noqa: E402

ROOT = robot.ROOT
IMPORT_DIR = ROOT / "robot" / "import"
BULK_MP3 = ["-vn", "-ac", "1", "-ar", "22050", "-b:a", "32k", "-map_metadata", "-1"]


def fix_mojibake(s: str) -> str:
    """Название в UTF-16 с перепутанным порядком байт («㈀　㈀㈀…» → «2022…»)."""
    if not s:
        return s
    odd = sum(1 for ch in s if 0x2E80 <= ord(ch) <= 0x9FFF or 0xAC00 <= ord(ch) <= 0xD7FF)
    if odd < max(3, len(s) // 2):
        return s
    try:
        fixed = s.encode("utf-16-be", "surrogatepass").decode("utf-16-le")
        return fixed if sum(ch.isalnum() for ch in fixed) >= len(fixed) // 3 else s
    except UnicodeError:
        return s


def msg_text(m) -> str:
    t = m.get("text", "")
    return t if isinstance(t, str) else "".join(x if isinstance(x, str) else x.get("text", "") for x in t)


def load_items(export: Path, channel: str):
    d = json.loads((export / "result.json").read_text(encoding="utf-8"))
    msgs = d["messages"]
    by_id = {m["id"]: m for m in msgs}
    items = []
    for k, m in enumerate(msgs):
        if m.get("media_type") not in ("audio_file", "voice_message"):
            continue
        f = m.get("file") or ""
        path = export / f
        if f.startswith("(File") or not path.exists():
            continue
        # подпись бывает отдельным постом прямо перед файлом
        ctx = ""
        prev = msgs[k - 1] if k else None
        if prev and not prev.get("media_type") and not prev.get("file") and msg_text(prev):
            gap = abs(int(m.get("date_unixtime", 0)) - int(prev.get("date_unixtime", 0)))
            if gap <= 600:
                ctx = msg_text(prev)
        reply = by_id.get(m.get("reply_to_message_id"))
        if reply and msg_text(reply) and not ctx:
            ctx = msg_text(reply)
        fwd_date = ""
        items.append({
            "key": f"tg:{channel}:{m['id']}",
            "order": m["id"],
            "post_date": m["date"][:10],
            "original_date": fwd_date,
            "text": msg_text(m),
            "context": ctx[:600],
            "file_title": fix_mojibake(m.get("title") or ""),
            "file_performer": fix_mojibake(m.get("performer") or ""),
            "file_name": fix_mojibake(m.get("file_name") or Path(f).name),
            "duration": int(m.get("duration_seconds") or 0),
            "size_mb": round(path.stat().st_size / 1048576, 1),
            "url": f"https://t.me/{channel}/{m['id']}",
            "path": str(path),
        })
    return items


def plan(args):
    src = next(s for s in json.loads(robot.SOURCES_JSON.read_text())["sources"] if s["id"] == args.source)
    items = load_items(Path(args.export), src["channel"])
    print(f"аудио с файлами: {len(items)}")
    import anthropic
    api = anthropic.Anthropic()
    data = robot.read_json(robot.KHUTBA_JSON, {"khutba": [], "lectures": []})
    drafts = robot.read_json(robot.DRAFTS_JSON, {"drafts": []})

    def one(item):
        if item["duration"] and item["duration"] < robot.MIN_SECONDS:
            return item, {"kind": "skip", "reason": f"короче {robot.MIN_SECONDS} с", "title": item["file_title"] or item["file_name"],
                          "speaker": src["speaker"], "date": item["post_date"], "language": "", "confidence": "high"}
        for attempt in range(4):
            try:
                return item, robot.clean_result(src, item, robot.classify(api, src, item))
            except Exception as e:  # noqa: BLE001
                err = e
                time.sleep(5 * (attempt + 1))
        return item, {"kind": "error", "reason": f"{type(err).__name__}: {str(err)[:120]}", "title": item["file_name"],
                      "speaker": src["speaker"], "date": item["post_date"], "language": "", "confidence": "low"}

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(one, items))

    # повторы: с тем, что уже есть, и внутри выгрузки (перепосты)
    titles = robot.all_titles(data, drafts)
    rows = []
    for item, res in sorted(results, key=lambda r: r[0]["order"]):
        status = res["kind"]
        if status in ("khutba", "lecture"):
            if robot.is_duplicate(titles, res):
                status = "duplicate"
            else:
                titles.setdefault((robot.norm_title(res["speaker"]), robot.norm_title(res["title"])), []).append(res["date"])
        rows.append({**{k: item[k] for k in ("key", "url", "post_date", "duration", "size_mb", "path", "file_name", "text")},
                     **res, "status": status})
    IMPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = IMPORT_DIR / f"{args.source}-plan.json"
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    write_table(rows, IMPORT_DIR / f"{args.source}-plan.md", src)
    from collections import Counter
    print("итог:", dict(Counter(r["status"] for r in rows)))
    print("таблица:", IMPORT_DIR / f"{args.source}-plan.md")


def write_table(rows, path, src):
    lines = [f"# Импорт: {src.get('title')}", "",
             "Статусы: khutba / lecture — будут добавлены; duplicate — уже есть; skip — пропуск; error — не разобрано.", "",
             "| № | Статус | Дата | Мин | Название | Уверенность | Исходный файл | Почему |", "|---|---|---|---|---|---|---|---|"]
    for k, r in enumerate(rows, 1):
        cell = lambda s: str(s).replace("|", "/").replace("\n", " ")[:90]
        lines.append(f"| {k} | {r['status']} | {r['date']} | {r['duration'] // 60} | {cell(r['title'])} | {r['confidence']} | "
                     f"{cell(r['file_name'])} | {cell(r.get('reason', ''))} |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def apply(args):
    import subprocess
    rows = json.loads((IMPORT_DIR / f"{args.source}-plan.json").read_text(encoding="utf-8"))
    data = robot.read_json(robot.KHUTBA_JSON, {"khutba": [], "lectures": []})
    old = json.loads(json.dumps(data))
    audio_dir = (ROOT / args.audio_dir) if not Path(args.audio_dir).is_absolute() else Path(args.audio_dir)
    audio_dir.mkdir(parents=True, exist_ok=True)
    base = args.base_url.rstrip("/")
    ff = robot.ffmpeg_exe()
    done = 0
    for r in rows:
        if r["status"] not in ("khutba", "lecture") or r.get("applied"):
            continue
        ts = int(time.time() * 1000)
        name = f"{r['date']}_{ts}.mp3"
        proc = subprocess.run([ff, "-hide_banner", "-y", "-i", r["path"], *BULK_MP3, str(audio_dir / name)],
                              capture_output=True, text=True, timeout=3600)
        if proc.returncode != 0:
            print("  ! ffmpeg:", r["file_name"], proc.stderr.strip().splitlines()[-1][:150]); continue
        times = re.findall(r"time=(\d+):(\d+):(\d+)", proc.stderr)
        if times:                                   # в выгрузке длительность бывает нулевой — берём из ffmpeg
            h, mi, se = map(int, times[-1]); r["duration"] = h * 3600 + mi * 60 + se
        if r["duration"] < 20:                      # короткие ответы на вопросы (20–45 с) — тоже записи
            (audio_dir / name).unlink(missing_ok=True); r["status"] = "skip"; r["reason"] = "запись слишком короткая"; continue
        y, m, d = r["date"].split("-")
        rec = {"id": f"{'khutba' if r['status'] == 'khutba' else 'lecture'}_{r['date'].replace('-', '')}_{ts}",
               "date": r["date"], "date_display": f"{d}.{m}.{y}", "title": r["title"], "speaker": r["speaker"],
               "audio_url": f"{base}/{name}", "duration": int(r["duration"])}
        for extra in ("description", "series"):           # приложение пока их не показывает; старые версии такие поля пропускают
            if r.get(extra):
                rec[extra] = r[extra]
        data["khutba" if r["status"] == "khutba" else "lectures"].insert(0, rec)
        r["applied"] = rec["id"]; done += 1
        time.sleep(0.002)
        if done % 10 == 0:
            print(f"  {done} записей", flush=True)
    data["last_updated"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    if base == robot.PAGES_BASE + "/audio":
        robot.check_data(old, data)
    robot.write_json(robot.KHUTBA_JSON, data)
    (IMPORT_DIR / f"{args.source}-plan.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"добавлено: {done}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan"); p.add_argument("export"); p.add_argument("--source", required=True)
    a = sub.add_parser("apply"); a.add_argument("--source", required=True)
    a.add_argument("--audio-dir", default="audio"); a.add_argument("--base-url", default=robot.PAGES_BASE + "/audio")
    args = ap.parse_args()
    plan(args) if args.cmd == "plan" else apply(args)
