"""
ComfyUI 連携のロジック(ComfyUI 本体なしでテストできる部分)。

  ・ComfyUI の実行履歴(history)1件を「実行の記録」に変換する
  ・履歴を gen_manager のDBへ同期する(prompt_id で重複登録を防ぐ)
  ・output フォルダにある過去の PNG を取り込む
  ・画面(JS)に返すJSONの整形

HTTP のルートや ComfyUI 本体との接続は __init__.py 側にある。
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
from datetime import datetime
from pathlib import Path

try:
    from . import gen_manager as gm
except ImportError:  # 単体(テスト・CLI)から読み込む場合
    import gen_manager as gm

LABEL_KEY = "gen_tracker"          # キュー投入時に extra_data へ入れるラベルのキー
STATE_DIRNAME = "_gen_tracker"     # output 直下の管理フォルダ(DB と設定を置く)
SETTINGS_FILE = "settings.json"

EXT_KIND = {
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".webp": "image", ".gif": "image", ".bmp": "image",
    ".mp4": "video", ".webm": "video", ".mov": "video", ".m4v": "video",
    ".mp3": "audio", ".wav": "audio", ".flac": "audio", ".m4a": "audio", ".ogg": "audio",
}

DEFAULT_SETTINGS = {
    "current_experiment": "ComfyUI",   # 以降に生成する分へ付ける実験名
    "current_condition": "",           # 空ならモデル名などから自動で決める
    "condition_rule": "model",         # model / model_lora / none
    "auto_sync": True,
    "poll_seconds": 10,
}
CONDITION_RULES = ("model", "model_lora", "none")


# --------------------------------------------------------------------------
# 設定(settings.json)
# --------------------------------------------------------------------------
def load_settings(root) -> dict:
    s = dict(DEFAULT_SETTINGS)
    try:
        with open(Path(root) / SETTINGS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            s.update(validate_settings(raw, strict=False))
    except (OSError, ValueError):
        pass
    return s


def validate_settings(patch: dict, strict: bool = True) -> dict:
    """型と範囲を確かめて、使える項目だけ返す。strict なら不正値は例外。"""
    out = {}

    def bad(msg):
        if strict:
            raise gm.ManagerError(msg)

    for k, v in patch.items():
        if k not in DEFAULT_SETTINGS:
            bad(f"未知の設定項目です: {k}")
        elif k in ("current_experiment", "current_condition"):
            if isinstance(v, str) and len(v) <= 200:
                out[k] = v.strip()
            else:
                bad(f"{k} は200文字以内の文字列です")
        elif k == "condition_rule":
            if v in CONDITION_RULES:
                out[k] = v
            else:
                bad(f"condition_rule は {CONDITION_RULES} のいずれかです")
        elif k == "auto_sync":
            if isinstance(v, bool):
                out[k] = v
            else:
                bad("auto_sync は true / false です")
        elif k == "poll_seconds":
            if isinstance(v, (int, float)) and not isinstance(v, bool) and 2 <= v <= 3600:
                out[k] = float(v)
            else:
                bad("poll_seconds は 2〜3600 の数値です")
    return out


def save_settings(root, patch: dict) -> dict:
    new = load_settings(root)
    new.update(validate_settings(patch, strict=True))
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=root, prefix=".settings-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(new, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, root / SETTINGS_FILE)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return new


# --------------------------------------------------------------------------
# 履歴 → 記録
# --------------------------------------------------------------------------
def auto_condition(settings: dict, rule: str) -> str:
    """生成設定から条件名を自動で作る。"""
    if rule == "none":
        return ""
    model = settings.get("model")
    name = Path(str(model)).stem if model else ""
    if rule == "model_lora" and settings.get("loras"):
        loras = []
        for item in settings["loras"]:
            n, _, strength = str(item).rpartition("@")
            loras.append(f"{Path(n or item).stem}@{strength}" if n else Path(item).stem)
        name = "+".join(filter(None, [name, *loras]))
    return name


def _find_msg(messages, names):
    for name in names:
        for m in messages:
            if isinstance(m, (list, tuple)) and len(m) == 2 and m[0] == name and isinstance(m[1], dict):
                return name, m[1]
    return None, None


def _inside(base: str, full: str) -> bool:
    try:
        return os.path.commonpath([base, full]) == base
    except ValueError:
        return False


def parse_history_item(prompt_id: str, item: dict, output_dir) -> dict | None:
    """ComfyUI の history の1件を、記録に必要な形へ。まだ終わっていない/読めなければ None。

    所要時間は history の status.messages にある execution_start と
    execution_success(または error / interrupted)のタイムスタンプ差。
    """
    if not isinstance(item, dict):
        return None
    status = item.get("status") or {}
    messages = status.get("messages") or []

    _, start = _find_msg(messages, ["execution_start"])
    end_name, end = _find_msg(messages, ["execution_error", "execution_interrupted", "execution_success"])
    status_str = status.get("status_str")
    if end_name is None and status_str not in ("success", "error"):
        return None  # 終了が確認できない

    if end_name == "execution_error" or (end_name is None and status_str == "error"):
        e = end or {}
        detail = (e.get("exception_message") or "").strip().splitlines()
        node = e.get("node_type") or ""
        run_status = gm.STATUS_FAILED
        error = (f"{node}: " if node else "") + (detail[0] if detail else "不明なエラー")
    elif end_name == "execution_interrupted":
        run_status, error = gm.STATUS_FAILED, "中断されました"
    else:
        run_status, error = gm.STATUS_OK, ""

    t0 = (start or {}).get("timestamp")
    t1 = (end or {}).get("timestamp")
    duration = None
    if isinstance(t0, (int, float)) and isinstance(t1, (int, float)) and t1 >= t0:
        duration = (t1 - t0) / 1000.0
    created_at = (datetime.fromtimestamp(t0 / 1000.0).strftime("%Y-%m-%dT%H:%M:%S")
                  if isinstance(t0, (int, float)) else gm.now_iso())

    prompt = item.get("prompt")
    graph = prompt[2] if isinstance(prompt, (list, tuple)) and len(prompt) > 2 and isinstance(prompt[2], dict) else {}
    extra = prompt[3] if isinstance(prompt, (list, tuple)) and len(prompt) > 3 and isinstance(prompt[3], dict) else {}
    labels = extra.get(LABEL_KEY) if isinstance(extra.get(LABEL_KEY), dict) else {}

    # 出力ファイル(type=output のみ。temp のプレビューは対象外)
    base = os.path.realpath(str(output_dir))
    files, seen = [], set()
    for node_out in (item.get("outputs") or {}).values():
        if not isinstance(node_out, dict):
            continue
        for entries in node_out.values():
            if not isinstance(entries, list):
                continue
            for e in entries:
                if not (isinstance(e, dict) and e.get("type") == "output" and isinstance(e.get("filename"), str)):
                    continue
                full = os.path.realpath(os.path.join(base, str(e.get("subfolder") or ""), e["filename"]))
                if _inside(base, full) and os.path.isfile(full) and full not in seen:
                    seen.add(full)
                    files.append(full)

    return {
        "source_id": prompt_id, "created_at": created_at, "status": run_status, "error": error,
        "duration": duration, "settings": gm.extract_comfy_settings(graph), "labels": labels, "files": files,
    }


def sync_history(mgr, history: dict, output_dir, settings: dict, seen: set | None = None) -> dict:
    """history を DB に反映する。何度呼んでも同じ実行を二重に登録しない。"""
    added = known = empty = 0
    seen = seen if seen is not None else set()
    parsed = []
    for pid, item in (history or {}).items():
        pid = str(pid)
        if pid in seen:
            continue
        p = parse_history_item(pid, item, output_dir)
        if p is not None:
            parsed.append(p)
    parsed.sort(key=lambda p: p["created_at"])  # 古い順に登録して ID を時系列にする

    for p in parsed:
        pid = p["source_id"]
        if mgr.has_source(pid):
            known += 1
            seen.add(pid)
            continue
        # 同じファイルが既に別の実行として記録されていれば除く(キャッシュ再実行・過去分取り込み済み)
        files = [f for f in p["files"] if not mgr.has_path(f)]
        if p["status"] == gm.STATUS_OK and not files:
            empty += 1  # 新しい出力が無い成功 = キャッシュだけの実行。記録しない
            seen.add(pid)
            continue
        labels = p["labels"]
        experiment = str(labels.get("experiment") or settings.get("current_experiment") or "")
        condition = str(labels.get("condition") or "") or auto_condition(p["settings"], settings.get("condition_rule", "model"))
        try:
            mgr.record(experiment=experiment, condition=condition, settings=p["settings"], files=files,
                       status=p["status"], duration=p["duration"], error=p["error"], mode="none",
                       created_at=p["created_at"], source_id=pid)
            added += 1
        except sqlite3.IntegrityError:  # 別経路で同時に登録された
            known += 1
        seen.add(pid)
    return {"added": added, "known": known, "empty": empty}


# --------------------------------------------------------------------------
# 過去分の取り込み(output フォルダの PNG)
# --------------------------------------------------------------------------
def ingest_output_folder(mgr, output_dir, experiment: str, rule: str) -> dict:
    base = Path(output_dir)
    skip = [mgr.root.resolve()]
    added = dup = nometa = 0
    for p in sorted(base.rglob("*.png")):
        try:
            rp = p.resolve()
        except OSError:
            continue
        if any(s in rp.parents for s in skip) or "_favorites" in p.relative_to(base).parts:
            continue
        if mgr.has_path(str(rp)):
            dup += 1
            continue
        settings = gm.read_png_settings(p)
        if settings is None:
            nometa += 1
            continue
        created = datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%dT%H:%M:%S")
        mgr.record(experiment=experiment, condition=auto_condition(settings, rule), settings=settings,
                   files=[p], mode="none", created_at=created)
        added += 1
    return {"added": added, "known": dup, "no_meta": nometa}


# --------------------------------------------------------------------------
# ギャラリー(output の全ファイル + 記録との突き合わせ)
# --------------------------------------------------------------------------
SKIP_DIRS = {"_favorites", STATE_DIRNAME}   # お気に入りのコピー先と、このツールの管理フォルダは対象外
MEDIA_EXTS = set(EXT_KIND)


def safe_rel_path(output_dir, rel) -> str | None:
    """クライアントから受け取った相対パスを検証し、output 内の実パスを返す。不正なら None。

    絶対パス・`..`・NUL・対象外の拡張子を拒否し、realpath + commonpath で「本当に output の配下か」を
    判定する(startswith だと output2 のような兄弟フォルダを通してしまうため)。
    シンボリックリンク経由の脱出も、realpath で解決してから判定するため拒否される。
    """
    if not isinstance(rel, str) or not rel or "\x00" in rel:
        return None
    if os.path.isabs(rel) or os.path.splitdrive(rel)[0]:
        return None
    if any(p == ".." for p in rel.replace("\\", "/").split("/")):
        return None
    if os.path.splitext(rel)[1].lower() not in MEDIA_EXTS:
        return None
    base = os.path.realpath(str(output_dir))
    full = os.path.realpath(os.path.join(base, rel))
    return full if _inside(base, full) else None


def scan_media(output_dir) -> list:
    """output 配下のメディアファイル一覧(作成日時の新しい順)。"""
    base = str(output_dir)
    items = []
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        rel_root = os.path.relpath(root, base)
        sub = "" if rel_root == "." else rel_root.replace("\\", "/")
        for fname in files:
            ext = os.path.splitext(fname)[1].lower()
            kind = EXT_KIND.get(ext)
            if not kind:
                continue
            try:
                st = os.stat(os.path.join(root, fname))
            except OSError:
                continue
            # Windows は st_ctime が作成日時、Linux/Mac は変更系。コピー等で更新日時だけ新しくなる
            # ケースに強いよう「古い方」を作成日時の近似に使う
            created = getattr(st, "st_birthtime", None)
            if created is None:
                created = min(st.st_ctime, st.st_mtime)
            items.append({
                "path": f"{sub}/{fname}" if sub else fname, "filename": fname, "subfolder": sub,
                "kind": kind, "ext": ext.lstrip("."), "ctime": created, "mtime": st.st_mtime,
            })
    items.sort(key=lambda x: x["ctime"], reverse=True)
    return items


def gallery_payload(mgr, output_dir) -> dict:
    """ギャラリー表示用: ファイルごとに お気に入り・タグ・メモ と、記録(条件・所要時間)を付ける。"""
    base = os.path.realpath(str(output_dir))
    metas = mgr.get_file_meta()
    runs = {}
    for r in mgr.db.execute(
            "SELECT o.path AS path, r.id AS id, r.condition AS condition, r.experiment AS experiment,"
            " r.status AS status, r.duration_sec AS duration_sec, r.created_at AS created_at"
            " FROM outputs o JOIN runs r ON r.id = o.run_id"):
        runs[r["path"]] = {k: r[k] for k in ("id", "condition", "experiment", "status", "duration_sec", "created_at")}
    items, unrecorded, conditions = [], 0, set()
    for f in scan_media(base):
        m = metas.get(f["path"]) or {}
        run = runs.get(os.path.join(base, f["path"].replace("/", os.sep)))
        if run is None and f["ext"] == "png":
            unrecorded += 1
        if run and run["condition"]:
            conditions.add(run["condition"])
        items.append({**f, "favorite": bool(m.get("favorite")), "tags": m.get("tags", []),
                      "note": m.get("note", ""), "run": run})
    return {"items": items, "conditions": sorted(conditions), "unrecorded_png": unrecorded}


def update_file(mgr, output_dir, rel, patch: dict, settings: dict) -> None:
    """お気に入り・タグ・メモ(ファイル単位)と、条件(その生成の記録)を更新する。

    条件は「生成」に付くため、同じ生成の画像すべてに反映される。まだ記録が無いファイルに条件を
    付けたときは、その場で記録を作る(PNG なら埋め込み設定も読む)。
    """
    full = safe_rel_path(output_dir, rel)
    if full is None:
        raise gm.ManagerError("不正なパスです")
    rel = rel.replace("\\", "/")
    meta = {k: patch[k] for k in ("favorite", "tags", "note") if k in patch}
    if meta:
        mgr.set_file_meta(rel, **meta)
    if "condition" in patch:
        cond = str(patch["condition"] or "").strip()
        if len(cond) > 200:
            raise gm.ManagerError("条件は200文字以内です")
        row = mgr.db.execute("SELECT run_id FROM outputs WHERE path=? LIMIT 1", (full,)).fetchone()
        if row:
            mgr.update_run(row["run_id"], condition=cond)
        elif cond:
            try:
                parsed = gm.read_png_settings(Path(full)) if full.lower().endswith(".png") else None
            except gm.ManagerError:  # Pillow が無い
                parsed = None
            st = os.stat(full)
            created = datetime.fromtimestamp(min(st.st_ctime, st.st_mtime)).strftime("%Y-%m-%dT%H:%M:%S")
            mgr.record(experiment=str(settings.get("current_experiment") or ""), condition=cond,
                       settings=parsed or {}, files=[full], mode="none", created_at=created)


# ---- PNG に埋め込まれた生成情報(モーダルに表示する) ----
SAMPLER_CLASS_HINTS = ("KSampler", "SamplerCustom")
CHECKPOINT_CLASS_HINTS = ("CheckpointLoader",)
LORA_CLASS_HINTS = ("LoraLoader", "LoRALoader", "LoraLoaderModelOnly")


def _node_title(node: dict, node_id: str) -> str:
    meta = node.get("_meta") or {}
    return meta.get("title") or node.get("class_type") or node_id


def parse_prompt_graph(prompt: dict) -> dict:
    """ComfyUI が画像に埋め込む API 形式のグラフから、プロンプト・モデル・LoRA・サンプラー設定を抜き出す。
    positive / negative に辿れなかったテキストは「その他」にノード名付きでまとめる。"""
    positive = negative = None
    model_name = None
    loras, sampler_info = [], None
    pos_id = neg_id = None
    for node in prompt.values():
        if not isinstance(node, dict):
            continue
        ct = node.get("class_type", "")
        inp = node.get("inputs", {}) or {}
        if any(h in ct for h in SAMPLER_CLASS_HINTS) and sampler_info is None:
            sampler_info = {
                "sampler_name": inp.get("sampler_name"), "scheduler": inp.get("scheduler"),
                "steps": inp.get("steps"), "cfg": inp.get("cfg"),
                "seed": inp.get("seed") if "seed" in inp else inp.get("noise_seed"),
                "denoise": inp.get("denoise"),
            }
            if isinstance(inp.get("positive"), list) and inp["positive"]:
                pos_id = str(inp["positive"][0])
            if isinstance(inp.get("negative"), list) and inp["negative"]:
                neg_id = str(inp["negative"][0])
        if any(h in ct for h in CHECKPOINT_CLASS_HINTS) and model_name is None:
            model_name = inp.get("ckpt_name")
        if any(h in ct for h in LORA_CLASS_HINTS) and inp.get("lora_name"):
            loras.append({"name": inp.get("lora_name"), "strength": inp.get("strength_model", inp.get("strength"))})
    texts = {}
    for nid, node in prompt.items():
        if isinstance(node, dict):
            t = (node.get("inputs", {}) or {}).get("text")
            if isinstance(t, str) and t.strip():
                texts[str(nid)] = t
    if pos_id in texts:
        positive = texts.pop(pos_id)
    if neg_id in texts:
        negative = texts.pop(neg_id)
    others = [{"label": _node_title(prompt.get(nid, {}), nid), "text": t} for nid, t in texts.items()]
    return {"positive": positive, "negative": negative, "other_texts": others,
            "model": model_name, "loras": loras, "sampler": sampler_info}


def read_png_info(full_path: str) -> dict:
    try:
        from PIL import Image
    except ImportError:
        return {"ok": False, "error": "Pillow 未導入のため読み取れません"}
    try:
        with Image.open(full_path) as img:
            img.load()
            raw = img.info.get("prompt")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"画像を開けませんでした: {e}"}
    if not raw:
        return {"ok": False, "error": "この画像には ComfyUI の生成情報が埋め込まれていません"}
    try:
        graph = json.loads(raw)
        if not isinstance(graph, dict):
            raise ValueError("形式が不正です")
        parsed = parse_prompt_graph(graph)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"生成情報の解析に失敗しました: {e}"}
    parsed["ok"] = True
    return parsed
