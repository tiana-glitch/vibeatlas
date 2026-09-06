import unittest

from server import _knowledge_graph_projection


class SourceGraphProjectionTests(unittest.TestCase):
    def test_archived_source_is_marked_and_its_points_are_hidden(self):
        nodes = [
            {
                "id": "source-a",
                "title": "材料 A",
                "path": "07-素材附件/来源摘要/a/source-a.md",
                "type": "source-summary",
                "summary": "摘要",
                "modified": "2026-09-06",
                "metadata": {
                    "source_status": "archived",
                    "graph_status": "hidden",
                    "source_document": "a.md",
                    "source_file": "a.md",
                    "source_path": "07-素材附件/原始资料/a.md",
                },
            },
            {
                "id": "point-a",
                "title": "保留点",
                "path": "08-知识点/a/point-a.md",
                "type": "knowledge-point",
                "summary": "保留点",
                "modified": "2026-09-06",
                "metadata": {
                    "source_summary": "source-a",
                    "source_status": "archived",
                    "graph_status": "active",
                },
            },
            {
                "id": "point-b",
                "title": "活动点",
                "path": "08-知识点/b/point-b.md",
                "type": "knowledge-point",
                "summary": "活动点",
                "modified": "2026-09-06",
                "metadata": {
                    "source_summary": "source-b",
                    "graph_status": "active",
                },
            },
            {
                "id": "source-b",
                "title": "材料 B",
                "path": "07-素材附件/来源摘要/b/source-b.md",
                "type": "source-summary",
                "summary": "摘要",
                "modified": "2026-09-06",
                "metadata": {"source_status": "active", "source_document": "b.md"},
            },
        ]
        projection = _knowledge_graph_projection(nodes, [
            {"source": "point-a", "target": "point-b"},
        ])

        self.assertEqual({node["id"] for node in projection["knowledge_nodes"]}, {"point-b"})
        archived = next(source for source in projection["sources"] if source["id"] == "source-a")
        self.assertTrue(archived["archived"])
        self.assertEqual(archived["status"], "archived")
        self.assertEqual(archived["source_status"], "archived")
        self.assertEqual(archived["graph_status"], "hidden")
        self.assertEqual(archived["point_ids"], [])
        self.assertEqual(archived["all_point_ids"], ["point-a"])
        self.assertEqual(archived["total_count"], 1)
        self.assertEqual(projection["knowledge_edges"], [])

    def test_legacy_active_source_keeps_existing_shape_and_defaults(self):
        node = {
            "id": "source",
            "title": "材料",
            "path": "07-素材附件/来源摘要/source.md",
            "type": "source-summary",
            "summary": "摘要",
            "modified": "2026-09-06",
            "metadata": {"source_document": "source.md"},
        }
        projection = _knowledge_graph_projection([node], [])
        source = projection["sources"][0]
        self.assertEqual(source["status"], "active")
        self.assertEqual(source["source_status"], "active")
        self.assertEqual(source["graph_status"], "active")
        self.assertFalse(source["archived"])

    def test_archived_summary_hides_legacy_point_without_point_status(self):
        nodes = [
            {
                "id": "legacy-source",
                "title": "旧来源",
                "path": "07-素材附件/来源摘要/legacy/legacy-source.md",
                "type": "source-summary",
                "summary": "摘要",
                "modified": "2026-09-06",
                "metadata": {"source_status": "archived", "source_file": "legacy.md"},
            },
            {
                "id": "legacy-point",
                "title": "历史知识点",
                "path": "08-知识点/legacy/legacy-point.md",
                "type": "knowledge-point",
                "summary": "历史知识点",
                "modified": "2026-09-06",
                "metadata": {"source_summary": "[[legacy-source]]", "graph_status": "active"},
            },
        ]
        projection = _knowledge_graph_projection(nodes, [])
        self.assertEqual(projection["knowledge_nodes"], [])
        source = projection["sources"][0]
        self.assertEqual(source["all_point_ids"], ["legacy-point"])
        self.assertEqual(source["point_ids"], [])


if __name__ == "__main__":
    unittest.main()
