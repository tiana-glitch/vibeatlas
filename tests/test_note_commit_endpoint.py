import json
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from note_knowledge import NoteOrchestrator
from server import build_server


def _init_vault(root: Path) -> None:
    (root / "00-收件箱").mkdir()
    (root / "00-知识库说明.md").write_text(
        "# 说明\n\n使用 [[文件名]]。\n"
        "每次写成一篇知识笔记，按来源和按知识点更新目录，唯一一行。\n",
        encoding="utf-8",
    )
    (root / "01-知识库目录.md").write_text(
        "---\ntype: index\nupdated: 2026-09-02\n---\n\n# 知识库目录\n",
        encoding="utf-8",
    )
    (root / "02-更新流水账.md").write_text(
        "---\ntype: log\nupdated: 2026-09-02\n---\n\n# 更新流水账\n\n"
        "| 时间 | 动作 | 笔记 | 来源 | 状态 |\n| --- | --- | --- | --- | --- |\n",
        encoding="utf-8",
    )


class NoteCommitEndpointTests(unittest.TestCase):
    def test_commit_rejects_ambiguous_or_non_string_markdown(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            _init_vault(root)
            http_server = build_server(port=0, directory=root, note_orchestrator=NoteOrchestrator())
            thread = threading.Thread(target=http_server.serve_forever, daemon=True)
            thread.start()
            base = "http://%s:%d" % http_server.server_address

            def post(path, payload):
                request = Request(
                    base + path,
                    data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                try:
                    with urlopen(request, timeout=3) as response:
                        return response.status, json.loads(response.read().decode("utf-8"))
                except HTTPError as error:
                    return error.code, json.loads(error.read().decode("utf-8"))

            try:
                status, payload = post(
                    "/api/notes/commit",
                    {"run_id": "note_input_1", "archive_markdown": "# approved", "markdown": "# other"},
                )
                self.assertEqual(status, 400)
                self.assertEqual(payload["error"]["code"], "ambiguous_markdown")
                status, payload = post(
                    "/api/notes/commit",
                    {"run_id": "note_input_1", "archive_markdown": {"text": "# invalid"}},
                )
                self.assertEqual(status, 400)
                self.assertEqual(payload["error"]["code"], "invalid_markdown")
                self.assertFalse(list((root / "00-收件箱").glob("*.md")))
            finally:
                http_server.shutdown()
                http_server.server_close()
                thread.join(timeout=3)

    def test_process_propose_commit_and_idempotent_retry(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            _init_vault(root)
            http_server = build_server(port=0, directory=root, note_orchestrator=NoteOrchestrator())
            thread = threading.Thread(target=http_server.serve_forever, daemon=True)
            thread.start()
            base = "http://%s:%d" % http_server.server_address

            def post(path, payload):
                request = Request(
                    base + path,
                    data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(request, timeout=3) as response:
                    return response.status, json.loads(response.read().decode("utf-8"))

            try:
                status, processed = post(
                    "/api/notes/process",
                    {"ocr_text": "会议决定先验证用户问题，再设计功能。", "user_note": "产品会议"},
                )
                self.assertEqual(status, 201)
                run_id = processed["run_id"]
                self.assertFalse(list((root / "00-收件箱").glob("*.md")))

                status, proposal = post("/api/notes/propose", {"run_id": run_id})
                self.assertEqual(status, 200)
                self.assertEqual(proposal["status"], "awaiting_review")
                self.assertIn("source_run_id", proposal["proposal"]["markdown"])

                status, committed = post(
                    "/api/notes/commit",
                    {
                        "run_id": run_id,
                        "archive_markdown": proposal["proposal"]["markdown"],
                        "title": proposal["note"]["title"],
                        "source_label": "产品会议",
                        "expected_index_sha256": proposal["proposal"]["index_sha256"],
                        "expected_log_sha256": proposal["proposal"]["log_sha256"],
                        "note_name": proposal["note"]["name"],
                    },
                )
                self.assertEqual(status, 201)
                self.assertFalse(committed["idempotent"])
                note_path = root / committed["note"]["path"]
                self.assertTrue(note_path.is_file())
                self.assertIn("[[%s]]" % committed["note"]["name"], (root / "01-知识库目录.md").read_text(encoding="utf-8"))
                log = (root / "02-更新流水账.md").read_text(encoding="utf-8")
                self.assertEqual(log.count("[[%s]]" % committed["note"]["name"]), 1)

                status, retry = post("/api/notes/commit", {"run_id": run_id})
                self.assertEqual(status, 200)
                self.assertTrue(retry["idempotent"])
                self.assertEqual(retry["transaction"]["log_rows_added"], 0)
            finally:
                http_server.shutdown()
                http_server.server_close()
                thread.join(timeout=3)

    def test_id_only_retry_survives_restart_without_run_storage(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            _init_vault(root)

            def start():
                instance = build_server(port=0, directory=root, note_orchestrator=NoteOrchestrator())
                worker = threading.Thread(target=instance.serve_forever, daemon=True)
                worker.start()
                return instance, worker, "http://%s:%d" % instance.server_address

            def post(base, path, payload):
                request = Request(
                    base + path,
                    data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(request, timeout=3) as response:
                    return response.status, json.loads(response.read().decode("utf-8"))

            first_server, first_thread, first_base = start()
            try:
                _, processed = post(
                    first_base,
                    "/api/notes/process",
                    {"ocr_text": "重启后仍可识别已提交笔记。"},
                )
                run_id = processed["run_id"]
                _, committed = post(first_base, "/api/notes/commit", {"run_id": run_id})
                self.assertFalse(committed["idempotent"])
            finally:
                first_server.shutdown()
                first_server.server_close()
                first_thread.join(timeout=3)

            second_server, second_thread, second_base = start()
            try:
                status, retry = post(second_base, "/api/notes/commit", {"run_id": run_id})
                self.assertEqual(status, 200)
                self.assertTrue(retry["idempotent"])
                self.assertEqual(retry["note"], committed["note"])
            finally:
                second_server.shutdown()
                second_server.server_close()
                second_thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
