"""ComfyUI 連携(tracker.py と __init__.py のルート)のテスト。ComfyUI 本体は不要。

    python -m unittest discover -s tests
"""
import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import gen_manager as gm  # noqa: E402
import tracker as tr  # noqa: E402

try:
    from PIL import Image, PngImagePlugin
except ImportError:
    Image = None
try:
    import aiohttp  # noqa: F401
    from aiohttp import web
except ImportError:
    aiohttp = None

T0 = 1_700_000_000_000  # ms

GRAPH = {
    "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "sdxl/base_v1.safetensors"}},
    "2": {"class_type": "CLIPTextEncode", "inputs": {"text": "a cat"}},
    "3": {"class_type": "CLIPTextEncode", "inputs": {"text": "blurry"}},
    "4": {"class_type": "KSampler", "inputs": {"seed": 7, "steps": 25, "cfg": 6.5, "sampler_name": "euler",
                                                "scheduler": "normal", "denoise": 1,
                                                "positive": ["2", 0], "negative": ["3", 0]}},
    "5": {"class_type": "LoraLoader", "inputs": {"lora_name": "style.safetensors", "strength_model": 0.7}},
}


def history_item(files=(), outcome="success", dur_ms=5000, labels=None, error=None, graph=GRAPH, start=T0,
                 file_type="output"):
    msgs = [["execution_start", {"prompt_id": "p", "timestamp": start}],
            ["execution_cached", {"nodes": [], "prompt_id": "p", "timestamp": start}]]
    if outcome == "success":
        msgs.append(["execution_success", {"prompt_id": "p", "timestamp": start + dur_ms}])
    elif outcome == "error":
        msgs.append(["execution_error", {"prompt_id": "p", "timestamp": start + dur_ms, "node_type": "KSampler",
                                         "exception_message": error or "CUDA out of memory\nextra line"}])
    elif outcome == "interrupted":
        msgs.append(["execution_interrupted", {"prompt_id": "p", "timestamp": start + dur_ms}])
    extra = {tr.LABEL_KEY: labels} if labels else {}
    return {
        "prompt": [1, "p", graph, extra, ["9"]],
        "outputs": {"9": {"images": [{"filename": f, "subfolder": "", "type": file_type} for f in files]}} if files else {},
        "status": {"status_str": "error" if outcome == "error" else "success",
                   "completed": outcome == "success", "messages": msgs},
    }


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.work = Path(self._tmp.name)
        self.out = self.work / "output"
        self.out.mkdir()
        self.root = self.out / tr.STATE_DIRNAME
        self.m = gm.Manager(self.root)
        self.settings = dict(tr.DEFAULT_SETTINGS)

    def tearDown(self):
        self.m.close()
        self._tmp.cleanup()

    def img(self, name, data=b"x"):
        p = self.out / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return name


class TestParse(Base):
    def test_success_duration_settings_outputs(self):
        self.img("a.png")
        p = tr.parse_history_item("p1", history_item(["a.png"]), self.out)
        self.assertEqual(p["status"], "ok")
        self.assertAlmostEqual(p["duration"], 5.0)
        self.assertEqual(p["settings"]["steps"], 25)
        self.assertEqual(p["settings"]["model"], "sdxl/base_v1.safetensors")
        self.assertEqual(len(p["files"]), 1)

    def test_error_keeps_first_line_and_node(self):
        p = tr.parse_history_item("p1", history_item(outcome="error"), self.out)
        self.assertEqual(p["status"], "failed")
        self.assertEqual(p["error"], "KSampler: CUDA out of memory")
        self.assertAlmostEqual(p["duration"], 5.0)

    def test_interrupted_is_failed(self):
        p = tr.parse_history_item("p1", history_item(outcome="interrupted"), self.out)
        self.assertEqual((p["status"], p["error"]), ("failed", "中断されました"))

    def test_unfinished_or_garbage_returns_none(self):
        self.assertIsNone(tr.parse_history_item("p", {"prompt": [], "status": {"messages": []}}, self.out))
        self.assertIsNone(tr.parse_history_item("p", "nonsense", self.out))
        self.assertIsNone(tr.parse_history_item("p", None, self.out))

    def test_temp_and_outside_files_are_excluded(self):
        self.img("ok.png")
        (self.work / "secret.png").write_bytes(b"s")
        item = history_item(["ok.png"])
        item["outputs"]["9"]["images"] += [
            {"filename": "../secret.png", "subfolder": "", "type": "output"},   # output の外
            {"filename": "tmp.png", "subfolder": "", "type": "temp"},           # プレビュー
            {"filename": "missing.png", "subfolder": "", "type": "output"},     # 存在しない
        ]
        p = tr.parse_history_item("p", item, self.out)
        self.assertEqual([os.path.basename(f) for f in p["files"]], ["ok.png"])


class TestSync(Base):
    def sync(self, history, seen=None):
        return tr.sync_history(self.m, history, self.out, self.settings, seen)

    def test_records_success_and_failure_with_auto_condition(self):
        self.img("a.png")
        res = self.sync({"p1": history_item(["a.png"]), "p2": history_item(outcome="error", start=T0 + 60_000)})
        self.assertEqual(res["added"], 2)
        rows = {r["source_id"]: r for r in map(dict, self.m.db.execute("SELECT * FROM runs"))}
        self.assertEqual(rows["p1"]["condition"], "base_v1")      # モデル名から自動
        self.assertEqual(rows["p1"]["experiment"], "ComfyUI")
        self.assertEqual(rows["p2"]["status"], "failed")
        self.assertEqual(self.m.list_runs(ids=[rows["p1"]["id"]])[0]["n_outputs"], 1)

    def test_is_idempotent(self):
        self.img("a.png")
        h = {"p1": history_item(["a.png"])}
        self.assertEqual(self.sync(h)["added"], 1)
        res = self.sync(h)
        self.assertEqual((res["added"], res["known"]), (0, 1))
        self.assertEqual(len(self.m.list_runs()), 1)

    def test_labels_at_queue_time_win_over_current_settings(self):
        self.img("a.png")
        self.settings["current_experiment"] = "あとから変えた名前"
        self.sync({"p1": history_item(["a.png"], labels={"experiment": "キュー時の実験", "condition": "手動条件"})})
        r = self.m.list_runs()[0]
        self.assertEqual((r["experiment"], r["condition"]), ("キュー時の実験", "手動条件"))

    def test_cached_rerun_with_no_new_files_is_skipped(self):
        self.img("a.png")
        self.sync({"p1": history_item(["a.png"])})
        res = self.sync({"p2": history_item(["a.png"], start=T0 + 1000)})  # 同じファイルを指すだけ
        self.assertEqual((res["added"], res["empty"]), (0, 1))

    def test_failed_run_without_outputs_is_still_recorded(self):
        self.assertEqual(self.sync({"p1": history_item(outcome="error")})["added"], 1)

    def test_condition_rules(self):
        s = tr.gm.extract_comfy_settings(GRAPH)
        self.assertEqual(tr.auto_condition(s, "model"), "base_v1")
        self.assertEqual(tr.auto_condition(s, "model_lora"), "base_v1+style@0.7")
        self.assertEqual(tr.auto_condition(s, "none"), "")

    def test_runs_are_registered_in_time_order(self):
        self.img("a.png"); self.img("b.png")
        self.sync({"late": history_item(["b.png"], start=T0 + 9000), "early": history_item(["a.png"], start=T0)})
        ids = {r["source_id"]: r["id"] for r in map(dict, self.m.db.execute("SELECT id, source_id FROM runs"))}
        self.assertLess(ids["early"], ids["late"])


class TestSettings(Base):
    def test_roundtrip_and_validation(self):
        s = tr.save_settings(self.root, {"current_experiment": "  実験1 ", "auto_sync": False, "poll_seconds": 30})
        self.assertEqual(s["current_experiment"], "実験1")
        self.assertEqual(tr.load_settings(self.root)["auto_sync"], False)
        for bad in ({"auto_sync": "yes"}, {"poll_seconds": 0}, {"condition_rule": "x"}, {"nope": 1},
                    {"current_experiment": "a" * 201}, {"poll_seconds": True}):
            with self.assertRaises(gm.ManagerError, msg=str(bad)):
                tr.save_settings(self.root, bad)

    def test_corrupt_settings_file_falls_back_to_defaults(self):
        self.root.mkdir(exist_ok=True)
        (self.root / tr.SETTINGS_FILE).write_text("{broken", encoding="utf-8")
        self.assertEqual(tr.load_settings(self.root), tr.DEFAULT_SETTINGS)


class TestManagerV2(Base):
    def test_update_delete_rating_search(self):
        a = self.m.record(experiment="e1", condition="c1", note="猫の写真")
        b = self.m.record(experiment="e2", condition="c2")
        self.assertTrue(self.m.update_run(a, rating=4, note="良い", condition="c9"))
        r = self.m.list_runs(ids=[a])[0]
        self.assertEqual((r["rating"], r["note"], r["condition"]), (4, "良い", "c9"))
        self.assertEqual([x["id"] for x in self.m.list_runs(min_rating=3)], [a])
        self.assertEqual([x["id"] for x in self.m.list_runs(search="良")], [a])
        self.assertEqual(self.m.list_runs(search="100%"), [])          # % は文字として扱う
        self.assertTrue(self.m.delete_run(b))
        self.assertFalse(self.m.delete_run(b))
        for bad in (6, -1, "x"):
            with self.assertRaises(gm.ManagerError):
                self.m.update_run(a, rating=bad)
        with self.assertRaises(gm.ManagerError):
            self.m.update_run(a, status="ok")

    def test_delete_keeps_the_file(self):
        f = self.out / "keep.png"; f.write_bytes(b"x")
        rid = self.m.record(files=[f], mode="none")
        self.m.delete_run(rid)
        self.assertTrue(f.exists())
        self.assertEqual(self.m.db.execute("SELECT COUNT(*) FROM outputs").fetchone()[0], 0)

    def test_v1_database_is_migrated(self):
        import sqlite3
        p = self.work / "old"; p.mkdir()
        con = sqlite3.connect(p / gm.DB_NAME)
        con.executescript(gm.SCHEMA)
        con.execute("INSERT INTO runs (created_at, status) VALUES ('2026-01-01T00:00:00', 'ok')")
        con.commit(); con.close()
        with gm.Manager(p) as m2:
            r = m2.list_runs()[0]
            self.assertEqual((r["rating"], r["source_id"]), (0, None))

    def test_duplicate_source_id_rejected(self):
        import sqlite3
        self.m.record(source_id="x")
        with self.assertRaises(sqlite3.IntegrityError):
            self.m.record(source_id="x")


@unittest.skipIf(Image is None, "Pillow が無いため取り込みを検証できません")
class TestIngestFolder(Base):
    def png(self, name, with_meta=True, color="red"):
        p = self.out / name
        p.parent.mkdir(parents=True, exist_ok=True)
        info = PngImagePlugin.PngInfo()
        if with_meta:
            info.add_text("prompt", json.dumps(GRAPH))
        Image.new("RGB", (4, 4), color).save(p, pnginfo=info)
        return p

    def test_ingest_in_place_and_idempotent(self):
        self.png("a.png"); self.png("sub/b.png", color="blue"); self.png("plain.png", with_meta=False)
        self.png("_favorites/copy.png")  # お気に入りコピーは対象外
        r = tr.ingest_output_folder(self.m, self.out, "過去分", "model")
        self.assertEqual((r["added"], r["no_meta"]), (2, 1))
        self.assertTrue((self.out / "a.png").exists())  # 動かさない
        r = tr.ingest_output_folder(self.m, self.out, "過去分", "model")
        self.assertEqual((r["added"], r["known"]), (0, 2))

    def test_history_sync_does_not_duplicate_ingested_files(self):
        self.png("a.png")
        tr.ingest_output_folder(self.m, self.out, "過去分", "model")
        res = tr.sync_history(self.m, {"p1": history_item(["a.png"])}, self.out, self.settings)
        self.assertEqual(res["added"], 0)


class TestFileMeta(Base):
    def test_favorite_tags_note_roundtrip_and_cleanup(self):
        self.m.set_file_meta("a.png", favorite=True)
        self.m.set_file_meta("a.png", tags=[" 候補 ", "候補", "", "表情"], note="メモ")
        self.assertEqual(self.m.get_file_meta()["a.png"], {"favorite": True, "tags": ["候補", "表情"], "note": "メモ"})
        self.m.set_file_meta("a.png", favorite=False)                 # 他の項目は残る
        self.assertIn("a.png", self.m.get_file_meta())
        self.m.set_file_meta("a.png", tags=[], note="")               # 何も無くなったら行ごと消える
        self.assertEqual(self.m.get_file_meta(), {})

    def test_validation(self):
        for kw in ({"tags": "x"}, {"tags": [1]}, {"tags": ["a"] * 0 + ["x" * 51]},
                   {"tags": [str(i) for i in range(51)]}, {"note": "x" * 5001}, {"note": 3}):
            with self.assertRaises(gm.ManagerError, msg=str(kw)):
                self.m.set_file_meta("a.png", **kw)

    def test_v2_database_gets_file_meta_table(self):
        import sqlite3
        p = self.work / "v2"; p.mkdir()
        con = sqlite3.connect(p / gm.DB_NAME); con.executescript(gm.SCHEMA); con.close()
        with gm.Manager(p) as m2:
            m2.set_file_meta("x.png", favorite=True)
            self.assertTrue(m2.get_file_meta()["x.png"]["favorite"])


class TestGallery(Base):
    def test_scan_lists_all_media_and_skips_managed_dirs(self):
        for n in ("a.png", "sub/b.jpg", "c.mp4", "d.mp3", "e.txt", "_favorites/copy.png", "_gen_tracker/x.png"):
            self.img(n)
        got = {i["path"]: i for i in tr.scan_media(self.out)}
        self.assertEqual(set(got), {"a.png", "sub/b.jpg", "c.mp4", "d.mp3"})
        self.assertEqual((got["sub/b.jpg"]["subfolder"], got["sub/b.jpg"]["kind"], got["sub/b.jpg"]["ext"]),
                         ("sub", "image", "jpg"))
        self.assertEqual((got["c.mp4"]["kind"], got["d.mp3"]["kind"]), ("video", "audio"))

    def test_safe_rel_path(self):
        self.img("sub/a.png")
        (self.work / "secret.png").write_bytes(b"s")
        os.makedirs(self.work / "output2"); (self.work / "output2" / "evil.png").write_bytes(b"e")
        self.assertIsNotNone(tr.safe_rel_path(self.out, "sub/a.png"))
        for bad in ("../secret.png", "sub/../../secret.png", "..\\secret.png", "/etc/x.png", "a\x00.png",
                    "sub/a.txt", "", None, 5, "../output2/evil.png"):
            self.assertIsNone(tr.safe_rel_path(self.out, bad), msg=repr(bad))
        if hasattr(os, "symlink"):  # シンボリックリンク経由の脱出
            try:
                os.symlink(self.work / "secret.png", self.out / "link.png")
            except OSError:
                return
            self.assertIsNone(tr.safe_rel_path(self.out, "link.png"))

    def test_payload_joins_runs_meta_and_counts_unrecorded(self):
        self.img("a.png"); self.img("b.jpg"); self.img("c.mp4")
        tr.sync_history(self.m, {"p1": history_item(["a.png"], labels={"experiment": "e", "condition": "lora=0.6"})},
                        self.out, self.settings)
        self.m.set_file_meta("b.jpg", favorite=True, tags=["候補"], note="良い")
        pl = tr.gallery_payload(self.m, self.out)
        by = {i["path"]: i for i in pl["items"]}
        self.assertEqual(by["a.png"]["run"]["condition"], "lora=0.6")
        self.assertAlmostEqual(by["a.png"]["run"]["duration_sec"], 5.0)
        self.assertIsNone(by["b.jpg"]["run"])
        self.assertEqual((by["b.jpg"]["favorite"], by["b.jpg"]["tags"], by["b.jpg"]["note"]), (True, ["候補"], "良い"))
        self.assertFalse(by["c.mp4"]["favorite"])
        self.assertEqual(pl["conditions"], ["lora=0.6"])
        self.assertEqual(pl["unrecorded_png"], 0)            # a.png は記録済み。jpg/mp4 は数えない

    def test_update_file_condition_on_recorded_and_unrecorded(self):
        self.img("a.png"); self.img("b.jpg")
        tr.sync_history(self.m, {"p1": history_item(["a.png"])}, self.out, self.settings)
        tr.update_file(self.m, self.out, "a.png", {"condition": "新しい条件", "favorite": True}, self.settings)
        tr.update_file(self.m, self.out, "b.jpg", {"condition": "手動"}, self.settings)   # 記録が無い jpg
        by = {i["path"]: i for i in tr.gallery_payload(self.m, self.out)["items"]}
        self.assertEqual(by["a.png"]["run"]["condition"], "新しい条件")
        self.assertTrue(by["a.png"]["favorite"])
        self.assertEqual(by["b.jpg"]["run"]["condition"], "手動")
        self.assertEqual(len(self.m.list_runs()), 2)               # 記録済みには新規作成しない
        tr.update_file(self.m, self.out, "b.jpg", {"condition": ""}, self.settings)       # 空にすると解除
        self.assertEqual({i["path"]: i for i in tr.gallery_payload(self.m, self.out)["items"]}["b.jpg"]["run"]["condition"], "")

    def test_update_file_rejects_bad_input(self):
        self.img("a.png")
        for rel in ("../x.png", "a.txt", None):
            with self.assertRaises(gm.ManagerError):
                tr.update_file(self.m, self.out, rel, {"favorite": True}, self.settings)
        with self.assertRaises(gm.ManagerError):
            tr.update_file(self.m, self.out, "a.png", {"condition": "x" * 201}, self.settings)
        self.assertEqual(self.m.get_file_meta(), {})

    @unittest.skipIf(Image is None, "Pillow が無い")
    def test_unrecorded_png_condition_reads_embedded_settings_and_info(self):
        p = self.out / "z.png"
        info = PngImagePlugin.PngInfo()
        g = json.loads(json.dumps(GRAPH)); g["9"] = {"class_type": "Note", "_meta": {"title": "メモ帳"}, "inputs": {"text": "別の文"}}
        info.add_text("prompt", json.dumps(g)); Image.new("RGB", (2, 2)).save(p, pnginfo=info)
        self.assertEqual(tr.gallery_payload(self.m, self.out)["unrecorded_png"], 1)
        tr.update_file(self.m, self.out, "z.png", {"condition": "手動A"}, self.settings)
        self.assertEqual(self.m.list_runs()[0]["settings"]["steps"], 25)
        self.assertEqual(tr.gallery_payload(self.m, self.out)["unrecorded_png"], 0)
        i = tr.read_png_info(str(p))
        self.assertEqual((i["ok"], i["positive"], i["negative"], i["model"]), (True, "a cat", "blurry", "sdxl/base_v1.safetensors"))
        self.assertEqual(i["other_texts"], [{"label": "メモ帳", "text": "別の文"}])
        self.assertEqual(i["loras"], [{"name": "style.safetensors", "strength": 0.7}])
        self.assertEqual((i["sampler"]["steps"], i["sampler"]["seed"]), (25, 7))

    @unittest.skipIf(Image is None, "Pillow が無い")
    def test_read_png_info_errors_are_messages_not_exceptions(self):
        Image.new("RGB", (2, 2)).save(self.out / "plain.png")
        (self.out / "broken.png").write_bytes(b"not a png")
        self.assertFalse(tr.read_png_info(str(self.out / "plain.png"))["ok"])
        self.assertFalse(tr.read_png_info(str(self.out / "broken.png"))["ok"])


class _FakeResp:
    def __init__(self, data, status=200):
        self.data, self.status = data, status


class _FakeRoute:
    def __init__(self, method, path, handler):
        self.method, self.path, self.handler = method, path, handler


class _FakeRoutes(list):
    def _reg(self, method):
        def outer(path):
            def deco(fn):
                self.append(_FakeRoute(method, path, fn))
                return fn
            return deco
        return outer

    def __init__(self):
        super().__init__()
        self.get, self.post = self._reg("GET"), self._reg("POST")


class _FakeRequest:
    """ハンドラが使う request.json() と request.rel_url.query だけを持つ模擬。"""
    def __init__(self, data=None, query=None, raw=None):
        self._data, self._raw = data, raw
        self.rel_url = types.SimpleNamespace(query=query or {})

    async def json(self):
        if self._raw is not None:
            raise ValueError("not json")
        return self._data


class TestRoutes(unittest.IsolatedAsyncioTestCase):
    """ルートのハンドラを直接呼んで検証する。aiohttp が無い環境でも動くよう、
    無ければ必要最小限の模擬(json_response / RouteTableDef)に差し替える。
    実物の aiohttp でのルーティング自体は CI(aiohttp あり)と実機で確認する。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.out = Path(self._tmp.name) / "output"
        self.out.mkdir()
        (self.out / "a.png").write_bytes(b"x")
        history = {"p1": history_item(["a.png"]), "p2": history_item(outcome="error", start=T0 + 100)}
        self.handlers = []
        outer = self

        class Q:
            def get_history(self, max_items=None):
                return dict(history)

        if aiohttp is None:
            web_mod = types.ModuleType("aiohttp.web")
            web_mod.json_response = lambda data, status=200: _FakeResp(data, status)
            pkg = types.ModuleType("aiohttp"); pkg.web = web_mod
            sys.modules["aiohttp"], sys.modules["aiohttp.web"] = pkg, web_mod
            routes, app = _FakeRoutes(), {}
            app_ns = types.SimpleNamespace(on_startup=[], on_cleanup=[])
            app = app_ns
        else:
            routes, app = web.RouteTableDef(), web.Application()
        self.app = app
        inst = types.SimpleNamespace(routes=routes, prompt_queue=Q(), app=app,
                                     add_on_prompt_handler=lambda f: outer.handlers.append(f))
        srv = types.ModuleType("server"); srv.PromptServer = types.SimpleNamespace(instance=inst)
        fp = types.ModuleType("folder_paths"); fp.get_output_directory = lambda: str(self.out)
        sys.modules["server"], sys.modules["folder_paths"] = srv, fp
        for k in [k for k in sys.modules if k.startswith("gen_manager_pkg")]:
            del sys.modules[k]
        spec = importlib.util.spec_from_file_location("gen_manager_pkg", os.path.join(ROOT, "__init__.py"),
                                                      submodule_search_locations=[ROOT])
        self.pkg = importlib.util.module_from_spec(spec)
        sys.modules["gen_manager_pkg"] = self.pkg
        spec.loader.exec_module(self.pkg)
        self.table = {(r.method.upper(), r.path): r.handler for r in routes}

    def tearDown(self):
        self._tmp.cleanup()

    async def call(self, method, path, json_body=None, query=None, raw=None):
        h = self.table[(method, path)]
        resp = await h(_FakeRequest(json_body, query, raw))
        data = resp.data if isinstance(resp, _FakeResp) else json.loads(resp.text)
        return resp.status, data

    async def test_all_routes_are_registered(self):
        names = {p for _, p in self.table}
        self.assertEqual(names, {f"/gen_tracker/{n}" for n in
                                 ("state", "settings", "sync", "files", "file_update", "file_info", "ingest")})

    async def test_full_flow(self):
        st, r = await self.call("POST", "/gen_tracker/sync")
        self.assertEqual((st, r["ok"], r["added"]), (200, True, 2))
        self.assertEqual((await self.call("POST", "/gen_tracker/sync"))[1]["added"], 0)

        _, d = await self.call("GET", "/gen_tracker/files")
        self.assertEqual([i["path"] for i in d["items"]], ["a.png"])
        item = d["items"][0]
        self.assertEqual(item["run"]["condition"], "base_v1")                    # 失敗した実行は画像が無いので出ない
        self.assertAlmostEqual(item["run"]["duration_sec"], 5.0)

        st, _ = await self.call("POST", "/gen_tracker/file_update",
                                {"path": "a.png", "favorite": True, "tags": ["候補"], "note": "最高", "condition": "A"})
        self.assertEqual(st, 200)
        _, d = await self.call("GET", "/gen_tracker/files")
        it = d["items"][0]
        self.assertEqual((it["favorite"], it["tags"], it["note"], it["run"]["condition"]), (True, ["候補"], "最高", "A"))
        self.assertEqual(d["conditions"], ["A"])

    async def test_bad_requests_return_json_errors(self):
        st, r = await self.call("POST", "/gen_tracker/settings", {"poll_seconds": -1})
        self.assertEqual((st, r["ok"]), (400, False))
        self.assertEqual((await self.call("POST", "/gen_tracker/file_update", raw=b"x"))[0], 400)
        self.assertEqual((await self.call("POST", "/gen_tracker/file_update", ["list"]))[0], 400)
        for bad in ({"path": "../x.png", "favorite": True}, {"path": "a.txt"}, {"favorite": True},
                    {"path": "a.png", "tags": "x"}, {"path": "a.png", "note": "x" * 6000}):
            st, r = await self.call("POST", "/gen_tracker/file_update", bad)
            self.assertEqual((st, r["ok"]), (400, False), msg=str(bad))
        self.assertEqual((await self.call("GET", "/gen_tracker/file_info", query={"path": "../x.png"}))[0], 400)
        self.assertEqual((await self.call("GET", "/gen_tracker/file_info", query={"path": "nope.png"}))[0], 404)

    async def test_file_info_route(self):
        if Image is None:
            self.skipTest("Pillow なし")
        p = self.out / "i.png"
        info = PngImagePlugin.PngInfo(); info.add_text("prompt", json.dumps(GRAPH))
        Image.new("RGB", (2, 2)).save(p, pnginfo=info)
        _, r = await self.call("GET", "/gen_tracker/file_info", query={"path": "i.png"})
        self.assertEqual((r["ok"], r["positive"]), (True, "a cat"))
        self.assertFalse((await self.call("GET", "/gen_tracker/file_info", query={"path": "a.png"}))[1]["ok"])

    async def test_settings_and_state(self):
        _, r = await self.call("POST", "/gen_tracker/settings", {"current_condition": "条件X"})
        self.assertEqual(r["settings"]["current_condition"], "条件X")
        _, st = await self.call("GET", "/gen_tracker/state")
        self.assertEqual(st["settings"]["current_condition"], "条件X")
        self.assertIn("_gen_tracker", st["db_path"])

    async def test_prompt_handler_stamps_labels_and_never_breaks_queue(self):
        await self.call("POST", "/gen_tracker/settings", {"current_experiment": "E", "current_condition": "C"})
        data = self.handlers[0]({"prompt": {}})
        self.assertEqual(data["extra_data"][tr.LABEL_KEY], {"experiment": "E", "condition": "C"})
        self.assertEqual(self.handlers[0]({"prompt": {}, "extra_data": "weird"})["prompt"], {})

    async def test_background_poll_starts_syncs_and_stops(self):
        await self.call("POST", "/gen_tracker/settings", {"poll_seconds": 2})
        app = {}
        await self.pkg._on_startup(app)
        task = app["gen_tracker_task"]
        self.assertFalse(task.done())
        await asyncio.sleep(2.4)                       # 1周期ぶん待つと自動で同期される
        _, d = await self.call("GET", "/gen_tracker/files")
        self.assertEqual(d["items"][0]["run"]["condition"], "base_v1")
        await self.pkg._on_cleanup(app)
        await asyncio.sleep(0.05)
        self.assertTrue(task.cancelled() or task.done())

    async def test_ingest_route(self):
        if Image is None:
            self.skipTest("Pillow なし")
        p = self.out / "z.png"
        info = PngImagePlugin.PngInfo(); info.add_text("prompt", json.dumps(GRAPH))
        Image.new("RGB", (2, 2)).save(p, pnginfo=info)
        _, r = await self.call("POST", "/gen_tracker/ingest", {"experiment": "過去"})
        self.assertEqual((r["ok"], r["added"]), (True, 1))
        _, d = await self.call("GET", "/gen_tracker/files")
        by = {i["path"]: i for i in d["items"]}
        self.assertEqual(by["z.png"]["run"]["condition"], "base_v1")
        self.assertIsNone(by["a.png"]["run"])            # 設定の入っていない画像は取り込まれない


if __name__ == "__main__":
    unittest.main(verbosity=1)
