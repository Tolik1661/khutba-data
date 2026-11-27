"""Описание и цикл для записей Абу Ислама, которые уже были в приложении (по подписям из выгрузки канала)."""
import json, re, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import robot
code = Path(__file__).with_name("refine_abu_islam.py").read_text()
ns = {"robot": robot, "json": json, "re": re, "anthropic": __import__("anthropic")}
exec(code[code.index("PROMPT = "):code.index("todo = ")], ns)   # тот же промпт и та же функция разбора
refine = ns["refine"]

exp = json.load(open(sys.argv[1]))
def text(m):
    t = m.get("text", ""); return t if isinstance(t, str) else "".join(x if isinstance(x, str) else x.get("text", "") for x in t)
posts = [m for m in exp["messages"] if m.get("media_type") == "audio_file"]
data = robot.read_json(robot.KHUTBA_JSON, {})
out = {}
for rec in data["khutba"] + data["lectures"]:
    if rec["speaker"] != "Абу Ислам аш-Шаркаси" or rec.get("description"):
        continue
    want = robot.norm_title(rec["title"])
    cands = [m for m in posts if want and (want in robot.norm_title(text(m)) or want == robot.norm_title(m.get("title") or ""))]
    if not cands:
        print("  нет подписи для:", rec["title"][:60]); continue
    m = min(cands, key=lambda m: abs(int(m["date"][:4]) - int(rec["date"][:4])))
    row = {"text": text(m), "title": rec["title"], "file_title": m.get("title") or "", "file_name": m.get("file_name") or "",
           "duration": int(m.get("duration_seconds") or 0), "post_date": m["date"][:10]}
    _, res = refine(row)
    if "error" in res:
        print("  ошибка:", res["error"]); continue
    t = re.sub(r"\s+", " ", res["title"]).strip().strip("«»\"").rstrip(".")
    out[rec["id"]] = {"title": t[:1].upper() + t[1:], "description": res["description"].strip()[:260], "series": res["series"].strip(),
                      "duration": row["duration"], "old_title": rec["title"]}
Path(__file__).with_name("abu_islam-existing.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
for k, v in out.items():
    print(f"  {v['old_title'][:45]!r} → {v['title'][:60]!r} [{v['series']}] {v['duration']}с\n      — {v['description']}")
