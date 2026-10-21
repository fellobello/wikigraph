"""Offline tests: a fake Wikipedia with a handful of pages exercises the whole pipeline."""

import argparse
import unittest
import urllib.parse

import wikigraph as wg


def a(title, text=None):
    return f'<a href="/wiki/{urllib.parse.quote(title.replace(" ", "_"))}">{text or title}</a>'


MATH = ('<span class="mwe-math-element"><span class="mwe-math-mathml-inline" style="display:none">'
        '<math alttext="{\\displaystyle \\theta - \\eta \\nabla f}"><mi>x</mi></math></span>'
        '<img alt="{\\displaystyle \\theta - \\eta \\nabla f}" src="x.svg"></span>')

# title -> (short description, lead html)
PAGES = {
    "Machine learning": ("Field of study in artificial intelligence",
        f"<p>Machine learning studies {a('Gradient descent')} and {a('Linear regression')}.</p>"),
    "Gradient descent": ("Optimization algorithm",
        f'<div class="hatnote">Not to be confused with {a("Hill climbing")}.</div>'
        f'<table class="infobox"><tr><td>{a("Infobox junk")}</td></tr></table>'
        f"<p>Gradient descent (developed by {a('Augustin-Louis Cauchy')}) is an iterative method that "
        f"updates {MATH} to minimise a {a('Differentiable function')} using its {a('Gradient')}"
        f'<sup class="reference">{a("Cite junk")}</sup>.</p>'
        f"<p>See {a('List of optimization algorithms')}.</p>"),
    "Linear regression": ("Statistical modeling method",
        f"<p>Linear regression fits a {a('Function (mathematics)', 'function')} by minimising a "
        f"{a('Loss function')} with {a('Gradient descent')}.</p>"),
    "Gradient": ("Multivariate derivative",
        f"<p>The gradient is the vector of {a('Partial derivative', 'partial derivatives')} of a "
        f"{a('Differentiable function')}.</p>"),
    "Differentiable function": ("Function whose derivative exists everywhere",
        f"<p>A differentiable {a('Function (mathematics)', 'function')} has a "
        f"{a('Partial derivative')} at each point.</p>"),
    # A cycle: Partial derivative <-> Derivative. Partial derivative is cited by more pages, so the
    # heuristic treats it as the more general one and drops Derivative -> Partial derivative.
    "Partial derivative": ("Derivative with respect to one variable",
        f"<p>A partial derivative is a {a('Derivative')} of a {a('Function (mathematics)', 'function')}.</p>"),
    "Derivative": ("Instantaneous rate of change",
        f"<p>The derivative of a {a('Function (mathematics)', 'function')} generalises the "
        f"{a('Partial derivative')}; it is a {a('Limit (mathematics)', 'limit')}.</p>"),
    "Loss function": ("Function measuring error",
        f"<p>A loss function is a {a('Function (mathematics)', 'function')} with a {a('Derivative')}.</p>"),
    "Function (mathematics)": ("Association of one output to each input",
        f"<p>A function relates each {a('Element (mathematics)', 'element')} of a "
        f"{a('Set (mathematics)', 'set')} to exactly one element of another set.</p>"),
    "Limit (mathematics)": ("Value a function approaches",
        f"<p>A limit of a {a('Function (mathematics)', 'function')} uses the "
        f"{a('Absolute value')} and {a('Real number')}s.</p>"),
    "Set (mathematics)": ("Collection of distinct objects", "<p>A set is a collection of elements.</p>"),
    "Element (mathematics)": ("Member of a set", f"<p>An element belongs to a {a('Set (mathematics)', 'set')}.</p>"),
    "Absolute value": ("Distance from zero", f"<p>The absolute value of a {a('Real number')}.</p>"),
    "Real number": ("Number on the continuum", f"<p>A real number is a limit of {a('Rational number')}s.</p>"),
    "Rational number": ("Quotient of integers", "<p>A rational number is a fraction.</p>"),
    "Augustin-Louis Cauchy": ("French mathematician (1789–1857)", "<p>Cauchy.</p>"),
    "List of optimization algorithms": ("", "<p>A list.</p>"),
    "Hill climbing": ("Optimization technique", "<p>Hill climbing.</p>"),
}
REDIRECTS = {"Gradient Descent": "Gradient descent", "Element": "Element (mathematics)"}


class FakeClient(wg.WikiClient):
    def __init__(self):
        self.lang = "en"
        self.requests = 0
        self.cache_hits = 0
        self.parsed = []

    def api(self, params):
        self.requests += 1
        if params["action"] == "parse":
            title = REDIRECTS.get(params["page"], params["page"])
            if title not in PAGES:
                return {"error": {"code": "missingtitle"}}
            self.parsed.append(title)
            return {"parse": {"title": title, "text": PAGES[title][1]}}
        titles = params["titles"].split("|")
        if params.get("prop") == "extracts":
            return {"query": {"pages": [{"title": t, "extract": f"{t} extract."} for t in titles]}}
        redirects = [{"from": t, "to": REDIRECTS[t]} for t in titles if t in REDIRECTS]
        pages = []
        for t in titles:
            t = REDIRECTS.get(t, t)
            if t in PAGES:
                pages.append({"title": t, "description": PAGES[t][0]})
            else:
                pages.append({"title": t, "missing": True})
        return {"query": {"redirects": redirects, "pages": pages}}


def opts(**over):
    o = argparse.Namespace(topics=["Machine learning"], max_nodes=100, max_depth=25, max_prereqs=8,
                           lead_paragraphs=1, min_support=2, max_seeds=60, max_categories=14,
                           hub=False, auto_hub=True, terminals=list(wg.DEFAULT_TERMINALS), stop=[],
                           keep_transitive=False)
    vars(o).update(over)
    return o


def build(curation=None, **over):
    client = FakeClient()
    o = opts(**over)
    cur = wg.load_curation(None)
    cur.update(curation or {})
    crawler = wg.Crawler(client, o, cur, log=lambda *a: None).run(o.topics)
    return wg.build_graph(crawler, o, client), client


class TestParsing(unittest.TestCase):
    def test_lead_parser_skips_noise_and_keeps_math(self):
        blocks = wg.parse_lead(PAGES["Gradient descent"][1])
        self.assertEqual(len(blocks), 2)
        links = blocks[0]["links"]
        self.assertEqual([t for t, _ in links],
                         ["Augustin-Louis Cauchy", "Differentiable function", "Gradient"])
        self.assertTrue(links[0][1], "link inside parentheses is flagged")
        self.assertIn("$\\theta - \\eta \\nabla f$", blocks[0]["text"])
        self.assertNotIn("Cite junk", blocks[0]["text"])

    def test_classify(self):
        self.assertEqual(wg.classify("X", {"title": "X", "description": "French mathematician (1789–1857)"}), "person")
        self.assertEqual(wg.classify("X", {"title": "X", "description": "Branch of mathematics"}), "field")
        self.assertEqual(wg.classify("List of things", {"title": "List of things"}), "list_or_outline")
        self.assertIsNone(wg.classify("Gradient", {"title": "Gradient", "description": "Multivariate derivative"}))


class TestBuild(unittest.TestCase):
    def setUp(self):
        self.graph, self.client = build()
        self.by_title = {n["title"]: n for n in self.graph["nodes"]}

    def test_field_topic_is_used_as_hub(self):
        self.assertEqual(self.graph["meta"]["hubs"], ["Machine learning"])
        self.assertNotIn("Machine learning", self.by_title)
        self.assertEqual(self.graph["meta"]["seeds"], ["Gradient descent", "Linear regression"])

    def test_noise_is_filtered_and_logged(self):
        for t in ("Augustin-Louis Cauchy", "List of optimization algorithms", "Hill climbing", "Infobox junk"):
            self.assertNotIn(t, self.by_title)
        self.assertNotIn("Augustin-Louis Cauchy", self.client.parsed, "parenthetical links are not followed")

    def test_is_dag_with_levels_and_terminals(self):
        ids = {n["id"]: n for n in self.graph["nodes"]}
        for n in self.graph["nodes"]:
            for p in n["from"]:
                self.assertLess(ids[p]["_level"], n["_level"])
                self.assertIn(n["id"], ids[p]["to"])
        self.assertEqual(self.by_title["Set (mathematics)"]["_level"], 0)
        self.assertTrue(self.by_title["Set (mathematics)"]["_terminal"])
        self.assertGreater(self.by_title["Gradient descent"]["_level"], self.by_title["Gradient"]["_level"])

    def test_cycle_broken_toward_more_cited_concept(self):
        removed = self.graph["review"]["cycle_edges_removed"]
        self.assertEqual(removed, [["Derivative", "Partial derivative"]])
        pd, d = self.by_title["Partial derivative"], self.by_title["Derivative"]
        self.assertIn(pd["id"], d["from"])

    def test_transitive_reduction(self):
        # Differentiable function cites Function directly, but also via Partial derivative -> Derivative -> Function.
        df, fn = self.by_title["Differentiable function"], self.by_title["Function (mathematics)"]
        self.assertNotIn(fn["id"], df["from"])
        self.assertGreater(self.graph["meta"]["transitive_edges_removed"], 0)

    def test_metrics_present(self):
        for n in self.graph["nodes"]:
            for k in ("_pagerank", "_betweenness_centrality", "_descendant_ratio", "_reachability_ratio"):
                self.assertIn(k, n)
        top = max(self.graph["nodes"], key=lambda n: n["_betweenness_centrality"])
        self.assertGreater(top["_betweenness_centrality"], 0)
        self.assertEqual(self.by_title["Function (mathematics)"]["label"], "Function")


class TestBudgetAndCuration(unittest.TestCase):
    def test_budget_keeps_most_cited(self):
        graph, client = build(max_nodes=4)
        self.assertTrue(graph["meta"]["budget_hit"])
        titles = {n["title"] for n in graph["nodes"]}
        self.assertIn("Function (mathematics)", titles, "a multiply-cited concept survives as a stub")
        self.assertLessEqual(len(client.parsed), 5)

    def test_curation(self):
        graph, _ = build({"exclude": ["Loss function"], "terminal": ["Derivative"],
                          "add_edges": [["Gradient", "Linear regression"]],
                          "categories": {"Gradient": "Calculus"},
                          "include": ["Hill climbing"]}, keep_transitive=True)
        by = {n["title"]: n for n in graph["nodes"]}
        self.assertNotIn("Loss function", by)
        self.assertFalse(by["Derivative"]["_expanded"])
        self.assertIn(by["Gradient"]["id"], by["Linear regression"]["from"])
        self.assertEqual(by["Gradient"]["category"], "Calculus")


if __name__ == "__main__":
    unittest.main()
