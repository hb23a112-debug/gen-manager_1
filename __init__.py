"""
Gen Manager — ComfyUI のサイドバーに「生成結果の管理」タブを追加するカスタムノード(拡張機能)。

  ・生成が終わるたびに、設定・所要時間・成功/失敗・出力ファイルを自動で記録
  ・output の全ファイルをギャラリーで表示(日付ごと・種類・条件・お気に入り・タグ・メモ)
  ・画像などのファイルは動かさず、場所だけを記録する

インストール:
    ComfyUI/custom_nodes/ に、このフォルダごと置いて ComfyUI を再起動。
    記録は output/_gen_tracker/ に保存される。
"""
import asyncio
import functools
import os
import sqlite3
import time
import traceback

from aiohttp import web
from server import PromptServer
import folder_paths

from . import gen_manager as gm
from . import tracker as tr

WEB_DIRECTORY = "./web"
NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}
__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]

LOG = "[GenManager]"
HISTORY_FETCH = 100   # 1回の同期で見る履歴の件数(新しい方から)

_state = {"last_sync": None, "last_added": 0, "error": "", "seen": set()}


def _output_dir() -> str:
    return folder_paths.get_output_directory()


def _root() -> str:
    return os.path.join(_output_dir(), tr.STATE_DIRNAME)


def _open() -> "gm.Manager":
    # リクエストごとに接続を開く(スレッドをまたいで使い回さないため)
    return gm.Manager(_root())


def _get_history(max_items=HISTORY_FETCH) -> dict:
    q = PromptServer.instance.prompt_queue
    try:
        return q.get_history(max_items=max_items)
    except TypeError:  # 古い ComfyUI は max_items を持たない
        return q.get_history()


def do_sync() -> dict:
    """ComfyUI の実行履歴を DB に反映する(同期処理。短時間で終わる)。"""
    settings = tr.load_settings(_root())
    with _open() as mgr:
        res = tr.sync_history(mgr, _get_history(), _output_dir(), settings, _state["seen"])
    _state["last_sync"] = time.time()
    _state["last_added"] = res["added"]
    _state["error"] = ""
    return res


def _api(fn):
    """例外を JSON のエラーにして返す(画面にメッセージを出せるように)。"""
    @functools.wraps(fn)
    async def wrapper(request):
        try:
            return await fn(request)
        except gm.ManagerError as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            return web.json_response({"ok": False, "error": f"{type(e).__name__}: {e}"}, status=500)
    return wrapper


async def _json(request) -> dict:
    try:
        data = await request.json()
    except Exception:
        raise gm.ManagerError("リクエストの形式が不正です")
    if not isinstance(data, dict):
        raise gm.ManagerError("リクエストの形式が不正です")
    return data


def _int(v, default=0, lo=None, hi=None):
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default
    if lo is not None:
        n = max(lo, n)
    if hi is not None:
        n = min(hi, n)
    return n


routes = PromptServer.instance.routes


@routes.get("/gen_tracker/state")
@_api
async def gt_state(request):
    with _open() as mgr:
        total = mgr.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    return web.json_response({
        "ok": True, "settings": tr.load_settings(_root()), "db_path": os.path.join(_root(), gm.DB_NAME),
        "output_dir": _output_dir(), "total": total, "last_sync": _state["last_sync"],
        "last_added": _state["last_added"], "sync_error": _state["error"],
    })


@routes.post("/gen_tracker/settings")
@_api
async def gt_settings(request):
    data = await _json(request)
    return web.json_response({"ok": True, "settings": tr.save_settings(_root(), data)})


@routes.post("/gen_tracker/sync")
@_api
async def gt_sync(request):
    return web.json_response({"ok": True, **do_sync()})


@routes.get("/gen_tracker/files")
@_api
async def gt_files(request):
    def work():  # 大量のファイルを走査するので、サーバーを止めないよう別スレッドで実行
        with _open() as mgr:
            return tr.gallery_payload(mgr, _output_dir())
    payload = await asyncio.get_running_loop().run_in_executor(None, work)
    return web.json_response({"ok": True, **payload})


@routes.post("/gen_tracker/file_update")
@_api
async def gt_file_update(request):
    data = await _json(request)
    patch = {k: data[k] for k in ("favorite", "tags", "note", "condition") if k in data}
    with _open() as mgr:
        tr.update_file(mgr, _output_dir(), data.get("path"), patch, tr.load_settings(_root()))
    return web.json_response({"ok": True})


@routes.get("/gen_tracker/file_info")
@_api
async def gt_file_info(request):
    full = tr.safe_rel_path(_output_dir(), request.rel_url.query.get("path"))
    if full is None:
        return web.json_response({"ok": False, "error": "不正なパスです"}, status=400)
    if not os.path.isfile(full):
        return web.json_response({"ok": False, "error": "ファイルが見つかりません"}, status=404)
    if not full.lower().endswith(".png"):
        return web.json_response({"ok": False, "error": "生成情報の表示は PNG 画像のみ対応しています"})
    return web.json_response(await asyncio.get_running_loop().run_in_executor(None, tr.read_png_info, full))


@routes.post("/gen_tracker/ingest")
@_api
async def gt_ingest(request):
    data = await _json(request)
    experiment = str(data.get("experiment") or "過去分")[:200]
    settings = tr.load_settings(_root())

    def work():  # 大量の PNG を読むので、サーバーを止めないよう別スレッドで実行
        with _open() as mgr:
            return tr.ingest_output_folder(mgr, _output_dir(), experiment, settings["condition_rule"])

    res = await asyncio.get_running_loop().run_in_executor(None, work)
    return web.json_response({"ok": True, **res})


# ---- 実行したときの「いまの実験名・条件」を、キュー投入時点で固定する ----
def _on_prompt(json_data):
    """Queue を押した時点のラベルを prompt に添える。同期が遅れて、後から変えたラベルが
    付いてしまうのを防ぐため。失敗しても生成は止めない。"""
    try:
        s = tr.load_settings(_root())
        extra = json_data.setdefault("extra_data", {})
        if isinstance(extra, dict):
            extra[tr.LABEL_KEY] = {"experiment": s["current_experiment"], "condition": s["current_condition"]}
    except Exception:  # noqa: BLE001
        traceback.print_exc()
    return json_data


try:
    PromptServer.instance.add_on_prompt_handler(_on_prompt)
except Exception as _e:  # noqa: BLE001
    print(f"{LOG} キュー投入時のラベル付けを登録できませんでした(自動で現在のラベルを使います): {_e}")


# ---- 画面を開いていなくても記録されるよう、バックグラウンドでも同期する ----
async def _poll_loop():
    while True:
        try:
            s = tr.load_settings(_root())
            await asyncio.sleep(max(2.0, float(s.get("poll_seconds", 10))))
            if s.get("auto_sync", True):
                do_sync()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            _state["error"] = f"{type(e).__name__}: {e}"
            print(f"{LOG} 自動同期に失敗: {e}")
            await asyncio.sleep(30)


async def _on_startup(app):
    app["gen_tracker_task"] = asyncio.ensure_future(_poll_loop())


async def _on_cleanup(app):
    task = app.get("gen_tracker_task")
    if task:
        task.cancel()


try:
    PromptServer.instance.app.on_startup.append(_on_startup)
    PromptServer.instance.app.on_cleanup.append(_on_cleanup)
except Exception as _e:  # noqa: BLE001
    print(f"{LOG} バックグラウンド同期を開始できませんでした(画面からの同期は使えます): {_e}")

print(f"{LOG} 読み込みました。記録先: output/{tr.STATE_DIRNAME}/")
