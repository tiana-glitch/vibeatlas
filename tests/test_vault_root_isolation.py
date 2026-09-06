import json
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from urllib.parse import quote

from server import build_server


class VaultRootIsolationTests(unittest.TestCase):
    def test_api_uses_explicit_vault_root_and_static_files_cannot_read_it(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            static = root / "static"
            static.mkdir()
            vault = static / "demo_vault"
            vault.mkdir()
            (static / "index.html").write_text("<h1>demo</h1>", encoding="utf-8")
            (vault / "01-知识库目录.md").write_text("# 目录\n", encoding="utf-8")
            (vault / "02-更新流水账.md").write_text("# 流水账\n", encoding="utf-8")
            (vault / "公开点.md").write_text(
                "---\ntype: knowledge-point\nknowledge_kind: method\n---\n\n# 公开点\n\n演示。\n",
                encoding="utf-8",
            )
            (vault / "来源-来源摘要.md").write_text(
                "---\ntype: source-summary\nsource_document: demo\nsource_file: demo.md\nsource_path: demo.md\n---\n\n# 来源\n",
                encoding="utf-8",
            )
            server = build_server(port=0, directory=static, vault_root=vault)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = "http://%s:%d" % server.server_address

            def get(path):
                with urlopen(base + path, timeout=3) as response:
                    return response.status, response.read()

            try:
                status, body = get("/api/knowledge/graph")
                self.assertEqual(status, 200)
                graph = json.loads(body.decode("utf-8"))
                self.assertEqual([node["id"] for node in graph["knowledge_nodes"]], ["公开点"])
                status, body = get("/index.html")
                self.assertEqual(status, 200)
                self.assertIn(b"demo", body)
                with self.assertRaises(HTTPError) as raised:
                    get("/" + quote("demo_vault/公开点.md", safe="/"))
                self.assertEqual(raised.exception.code, 404)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)

    def test_build_server_without_vault_root_keeps_directory_as_vault(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "01-知识库目录.md").write_text("# 目录\n", encoding="utf-8")
            (root / "02-更新流水账.md").write_text("# 流水账\n", encoding="utf-8")
            (root / "点.md").write_text("---\ntype: knowledge-point\n---\n# 点\n", encoding="utf-8")
            server = build_server(port=0, directory=root)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = "http://%s:%d" % server.server_address
            try:
                with urlopen(base + "/api/knowledge/graph", timeout=3) as response:
                    graph = json.loads(response.read().decode("utf-8"))
                self.assertEqual([node["id"] for node in graph["knowledge_nodes"]], ["点"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
