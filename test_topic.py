import json
import tempfile
import unittest
from pathlib import Path

import topic


class TopicTests(unittest.TestCase):
    def test_slugify(self):
        self.assertEqual(topic.slugify("Cybernetics"), "cybernetics")
        self.assertEqual(topic.slugify("Linear algebra"), "linear-algebra")
        self.assertEqual(topic.slugify("C++ (language)"), "c-language")
        self.assertEqual(topic.slugify("!!!"), "topic")

    def test_index_lists_graphs_but_not_curation_files(self):
        with tempfile.TemporaryDirectory() as d:
            old, topic.GRAPHS = topic.GRAPHS, Path(d)
            try:
                g = {"meta": {"topics": ["Cybernetics"]}, "nodes": [{"id": "a"}, {"id": "b"}]}
                (Path(d) / "cybernetics.json").write_text(json.dumps(g))
                (Path(d) / "cybernetics.curation.json").write_text("{}")
                (Path(d) / "bare.json").write_text(json.dumps([{"id": "x"}]))
                idx = topic.write_index()
            finally:
                topic.GRAPHS = old
        self.assertEqual(idx, [
            {"file": "graphs/bare.json", "topic": "bare", "nodes": 1},
            {"file": "graphs/cybernetics.json", "topic": "Cybernetics", "nodes": 2},
        ])


if __name__ == "__main__":
    unittest.main()
