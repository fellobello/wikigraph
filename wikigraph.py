#!/usr/bin/env python3
"""
wikigraph -- build a prerequisite knowledge graph for any topic from Wikipedia.

A page's prerequisites are the concepts linked in its *definition* (the first
paragraphs of its lead section, outside parentheses). The crawl only ever
follows that prerequisite direction, from the seed topics down toward their
foundations, so the graph narrows instead of sprawling. On top of that:

  * filters drop people, places, organisations, works, dates, fields of study,
    disambiguation pages and list/outline pages (every drop is logged);
  * a best-first queue expands the concepts cited by the most pages first, so
    when the node budget runs out it is the peripheral concepts that are cut;
  * a terminal set (logical / set-theoretic bedrock by default) is never expanded;
  * cycles are broken by keeping edges that point from the more widely cited
    concept to the less cited one, then the DAG is transitively reduced;
  * a curation file lets a human exclude, keep, merge, re-wire or recategorise
    nodes. Rebuilds hit the on-disk cache, so iterating on curation is fast.

The output uses the node format of the-palindrome/ml-knowledge-graph
(id, label, to, from, category, definition, long_description, _metrics...),
wrapped as {"meta", "review", "nodes"}; pass --array for a bare node list.

Usage:
    python3 wikigraph.py build "Machine learning" -o ml.json --max-nodes 400
    python3 wikigraph.py build "Gradient descent" "Support vector machine" \\
        --curation curation.json -o ml.json
    python3 wikigraph.py review ml.json
"""

import argparse
import hashlib
import heapq
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict, deque
from html.parser import HTMLParser

VERSION = "0.1"

# The logical and set-theoretic bedrock, as Wikipedia titles. These are kept as
# nodes but never expanded. Titles are resolved through redirects at start-up.
DEFAULT_TERMINALS = [
    "Set (mathematics)", "Element (mathematics)", "Equality (mathematics)",
    "Natural number", "Logical conjunction", "Logical disjunction", "Negation",
    "Material conditional", "Universal quantification", "Existential quantification",
]

NON_ARTICLE_NAMESPACES = {
    "file", "image", "media", "help", "wikipedia", "wp", "category", "template",
    "portal", "special", "talk", "wiktionary", "wikt", "module", "draft", "user",
    "mos", "h", "s", "wikisource", "wikibooks", "commons", "meta", "d", "q",
}

TITLE_FILTERS = [
    (re.compile(r"^(list|outline|index|glossary|timeline|history|bibliography) of\b", re.I), "list_or_outline"),
    (re.compile(r"^\d{1,4}s?( (bc|ad|bce|ce))?$", re.I), "date"),
    (re.compile(r"^\d{1,2}(st|nd|rd|th) century", re.I), "date"),
    (re.compile(r"^(january|february|march|april|may|june|july|august|september|october|november|december) \d{1,2}$", re.I), "date"),
]

# Matched against Wikipedia's short description ("American mathematician", ...).
DESCRIPTION_FILTERS = [
    (re.compile(r"\b(\d{3,4}\s*[–-]\s*\d{2,4}|born \d{3,4}|died \d{3,4})\b"
                r"|\b(mathematician|physicist|scientist|philosopher|engineer|economist|statistician"
                r"|psychologist|researcher|businessman|businesswoman|entrepreneur|politician|author"
                r"|writer|novelist|poet|professor|biologist|chemist|inventor|actor|actress|singer"
                r"|musician|artist|painter|astronomer|logician|neuroscientist|linguist|educator)s?\b", re.I), "person"),
    (re.compile(r"\b(country|city|town|village|capital|province|county|river|island|region of"
                r"|state of|sovereign state|municipality)\b", re.I), "place"),
    (re.compile(r"\b(company|corporation|organi[sz]ation|university|college|institute|agency"
                r"|laboratory|foundation|conference|journal|magazine|newspaper|publisher"
                r"|research center|research centre|nonprofit|non-profit)\b", re.I), "organisation"),
    (re.compile(r"\b(film|novel|album|song|book|television|tv series|video game|website|podcast"
                r"|board game|comic|opera)\b", re.I), "work"),
    (re.compile(r"\b(branch|field|subfield|area|discipline|domain) of\b|\bacademic (discipline|field)\b"
                r"|^(the )?(scientific )?study of\b|\bscience of\b|\bfield of (study|research)\b", re.I), "field"),
    (re.compile(r"\bdisambiguation\b|\btopics referred to by the same term\b", re.I), "disambiguation"),
]

USER_AGENT_TEMPLATE = "wikigraph/{v} (prerequisite knowledge-graph builder; contact: {c})"


# ---------------------------------------------------------------------------
# Wikipedia access
# ---------------------------------------------------------------------------

class WikiClient:
    """MediaWiki Action API client with an on-disk response cache."""

    def __init__(self, lang="en", cache_dir=".wikigraph-cache", contact="not provided", delay=0.15):
        self.endpoint = f"https://{lang}.wikipedia.org/w/api.php"
        self.lang = lang
        self.cache_dir = cache_dir
        self.user_agent = USER_AGENT_TEMPLATE.format(v=VERSION, c=contact)
        self.delay = delay
        self.requests = 0
        self.cache_hits = 0
        self._last = 0.0
        if cache_dir:
            os.makedirs(cache_dir, exist_ok=True)

    def article_url(self, title):
        return f"https://{self.lang}.wikipedia.org/wiki/" + urllib.parse.quote(title.replace(" ", "_"))

    def api(self, params):
        params = {**params, "format": "json", "formatversion": "2"}
        key = hashlib.sha1(json.dumps(params, sort_keys=True).encode()).hexdigest()
        path = os.path.join(self.cache_dir, key[:2], key + ".json") if self.cache_dir else None
        if path and os.path.exists(path):
            self.cache_hits += 1
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        data = self._fetch(params)
        if path:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f)
        return data

    def _fetch(self, params):
        url = self.endpoint + "?" + urllib.parse.urlencode({**params, "maxlag": "5"})
        for attempt in range(6):
            wait = self.delay - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()
            self.requests += 1
            try:
                req = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                if data.get("error", {}).get("code") == "maxlag":
                    raise urllib.error.URLError("maxlag")
                return data
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                status = getattr(exc, "code", None)
                if status is not None and status < 500 and status != 429:
                    raise
                time.sleep(min(60, 2 ** attempt))
        raise RuntimeError(f"Wikipedia API kept failing for {params}")

    def lead(self, title, whole_page=False):
        """Return (canonical_title, html) for a page's lead section, or (None, None)."""
        params = {"action": "parse", "page": title, "prop": "text", "redirects": "1",
                  "disablelimitreport": "1", "disableeditsection": "1"}
        if not whole_page:
            params["section"] = "0"
        data = self.api(params)
        if "error" in data:
            return None, None
        return data["parse"]["title"], data["parse"]["text"]

    def page_info(self, titles):
        """Resolve titles through redirects and fetch short descriptions.

        Returns {input_title: {"title", "description", "disambiguation", "missing"}}.
        """
        out = {}
        titles = list(dict.fromkeys(titles))
        for i in range(0, len(titles), 50):
            chunk = titles[i:i + 50]
            data = self.api({"action": "query", "titles": "|".join(chunk), "redirects": "1",
                             "prop": "description|pageprops", "ppprop": "disambiguation"})
            q = data.get("query", {})
            mapping = {t: t for t in chunk}
            for step in ("normalized", "redirects"):
                hops = {h["from"]: h["to"] for h in q.get(step, [])}
                mapping = {k: hops.get(v, v) for k, v in mapping.items()}
            pages = {p["title"]: p for p in q.get("pages", [])}
            for original, final in mapping.items():
                p = pages.get(final, {"missing": True})
                out[original] = {
                    "title": final,
                    "description": p.get("description", ""),
                    "disambiguation": "disambiguation" in p.get("pageprops", {}),
                    "missing": bool(p.get("missing") or p.get("invalid")),
                }
        return out


# ---------------------------------------------------------------------------
# Lead-section parsing
# ---------------------------------------------------------------------------

VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
             "source", "track", "wbr"}
SKIP_TAGS = {"table", "sup", "style", "script", "figure", "math", "audio", "video"}
SKIP_CLASSES = {"hatnote", "navbox", "infobox", "sidebar", "reflist", "references",
                "mw-references-wrap", "toc", "thumb", "shortdescription", "mw-empty-elt",
                "noprint", "metadata", "ambox", "mw-editsection", "reference", "gallery",
                "side-box", "navigation-not-searchable", "IPA", "rt-commentedText"}


class LeadParser(HTMLParser):
    """Collects paragraph text (math as $tex$) and the article links in each paragraph."""

    def __init__(self, include_lists=False):
        super().__init__(convert_charrefs=True)
        self.include_lists = include_lists
        self.stack = []          # (tag, skips, kind)
        self.blocks = []         # [{"text": str, "links": [(title, in_parens)]}]
        self.cur = None
        self.paren = 0
        self.math_emitted = False

    def _skipping(self):
        return any(skip for _, skip, _ in self.stack)

    def _in_math(self):
        return any(kind == "math" for _, _, kind in self.stack)

    def _emit_math(self, tex):
        if self.math_emitted or not tex or self.cur is None:
            return
        tex = tex.strip()
        m = re.match(r"^\{\\displaystyle\s*(.*)\}$", tex, re.S)
        if m:
            tex = m.group(1).strip()
        self.cur["text"].append(f"${tex}$")
        self.math_emitted = True

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = set((attrs.get("class") or "").split())
        if tag in VOID_TAGS:
            if tag == "img" and self._in_math():
                self._emit_math(attrs.get("alt"))
            return
        kind = None
        skip = tag in SKIP_TAGS or bool(classes & SKIP_CLASSES)
        if "mwe-math-element" in classes:
            kind, skip, self.math_emitted = "math", True, False
        if tag == "math" and self._in_math():
            self._emit_math(attrs.get("alttext"))
        is_block = tag == "p" or (self.include_lists and tag in ("li", "dd"))
        if is_block and self.cur is None and not skip and not self._skipping():
            self.cur = {"text": [], "links": []}
            self.paren = 0
            kind = "block"
        self.stack.append((tag, skip, kind))
        if tag == "a" and self.cur is not None and not self._skipping():
            title = href_to_title(attrs.get("href", ""))
            if title:
                self.cur["links"].append((title, self.paren > 0))

    def handle_endtag(self, tag):
        if tag in VOID_TAGS or not any(t == tag for t, _, _ in self.stack):
            return
        while self.stack:
            t, _, kind = self.stack.pop()
            if kind == "block":
                text = re.sub(r"\s+", " ", "".join(self.cur["text"])).strip()
                if text:
                    self.blocks.append({"text": text, "links": self.cur["links"]})
                self.cur = None
            if t == tag:
                break

    def handle_data(self, data):
        if self.cur is None or self._skipping():
            return
        self.cur["text"].append(data)
        for ch in data:
            if ch in "([":
                self.paren += 1
            elif ch in ")]" and self.paren:
                self.paren -= 1


def href_to_title(href):
    if not href.startswith("/wiki/"):
        return None
    title = urllib.parse.unquote(href[len("/wiki/"):].split("#")[0]).replace("_", " ").strip()
    if not title:
        return None
    if ":" in title and title.split(":", 1)[0].strip().lower() in NON_ARTICLE_NAMESPACES:
        return None
    return title


def parse_lead(html, include_lists=False):
    parser = LeadParser(include_lists=include_lists)
    parser.feed(html)
    parser.close()
    return parser.blocks


def classify(title, info):
    """Return a filter reason for a resolved page, or None if it may be a concept."""
    if info.get("missing"):
        return "missing"
    if info.get("disambiguation"):
        return "disambiguation"
    for pattern, reason in TITLE_FILTERS:
        if pattern.search(info.get("title", title)):
            return reason
    desc = info.get("description", "")
    for pattern, reason in DESCRIPTION_FILTERS:
        if pattern.search(desc):
            return reason
    return None


def looks_like_hub(title, info):
    return bool(re.match(r"^(outline|list|index|glossary) of\b", title, re.I)
                or classify(title, info) == "field")


# ---------------------------------------------------------------------------
# Curation
# ---------------------------------------------------------------------------

def load_curation(path):
    cur = {"exclude": [], "include": [], "terminal": [], "merge": {}, "add_edges": [],
           "remove_edges": [], "categories": {}, "seeds": [], "notes": ""}
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            cur.update(json.load(f))
    return cur


# ---------------------------------------------------------------------------
# Crawl
# ---------------------------------------------------------------------------

class Crawler:
    def __init__(self, client, opts, curation, log=print):
        self.client = client
        self.o = opts
        self.cur = curation
        self.log = log
        self.info = {}                      # any title -> page info
        self.nodes = {}                     # canonical title -> node dict
        self.prereqs = defaultdict(list)    # canonical title -> [canonical prereq titles]
        self.cited_by = defaultdict(set)    # canonical title -> {dependents}
        self.depth = {}
        self.filtered = {}                  # canonical title -> (reason, set(cited_by))
        self.seeds = []
        self.hubs = []
        self.budget_hit = False

    # -- resolution helpers ------------------------------------------------
    def resolve(self, titles):
        need = [t for t in titles if t not in self.info]
        if need:
            for original, res in self.client.page_info(need).items():
                self.info[original] = res
                self.info.setdefault(res["title"], res)
        return {t: self.info[t] for t in titles}

    def canonical(self, title):
        t = self.info.get(title, {}).get("title", title)
        return self.cur["merge"].get(t, t)

    def _setup_lists(self):
        names = list(self.cur["exclude"]) + list(self.cur["include"]) + list(self.cur["terminal"])
        names += self.o.terminals + self.o.stop
        self.resolve(names)
        c = self.canonical
        self.excluded = {c(t) for t in self.cur["exclude"]} | {c(t) for t in self.o.stop}
        self.included = {c(t) for t in self.cur["include"]}
        self.terminals = {c(t) for t in self.o.terminals} | {c(t) for t in self.cur["terminal"]}

    def reject_reason(self, title):
        canon = self.canonical(title)
        if canon in self.included:
            return None
        if canon in self.excluded:
            return "curated_exclude"
        return classify(canon, self.info.get(title, {"title": canon}))

    # -- crawl ---------------------------------------------------------------
    def run(self, topics):
        self._setup_lists()
        topics = list(topics) + list(self.cur.get("seeds", []))
        self.resolve(topics)
        for topic in topics:
            canon = self.canonical(topic)
            info = self.info[topic]
            if info.get("missing"):
                self.log(f"! '{topic}' was not found on Wikipedia, skipping")
                continue
            if self.o.hub or (self.o.auto_hub and looks_like_hub(canon, info)):
                self._expand_hub(canon)
            elif canon not in self.seeds:
                self.seeds.append(canon)
        if not self.seeds:
            raise SystemExit("No usable seed concepts. Try a more specific topic or pass --no-auto-hub.")

        heap, counter = [], 0
        bonus = len(self.seeds) + 10 ** 6   # every seed is expanded before anything else

        def push(title):
            nonlocal counter
            counter += 1
            seed_rank = self.seeds.index(title) if title in self.seeds else None
            prio = (-(bonus - seed_rank) if seed_rank is not None else -len(self.cited_by[title]),
                    self.depth[title], counter)
            heapq.heappush(heap, (prio, title, len(self.cited_by[title])))

        for s in self.seeds:
            self.depth[s] = 0
            push(s)

        expanded = 0
        while heap:
            _, title, support_at_push = heapq.heappop(heap)
            if title in self.nodes or (title not in self.seeds and support_at_push != len(self.cited_by[title])):
                continue
            if expanded >= self.o.max_nodes:
                self.budget_hit = True
                break
            if self.depth[title] > self.o.max_depth:
                continue
            if self._expand(title):
                expanded += 1
                for p in self.prereqs[title]:
                    if p not in self.nodes:
                        push(p)
                if expanded % 25 == 0:
                    self.log(f"  expanded {expanded} concepts, {len(heap)} queued "
                             f"({self.client.requests} requests, {self.client.cache_hits} cached)")
        self._settle_frontier()
        return self

    def _expand_hub(self, title):
        canon, html = self.client.lead(title, whole_page=True)
        if html is None:
            return
        self.hubs.append(canon)
        links = []
        for block in parse_lead(html, include_lists=True):
            links += [t for t, _ in block["links"]]
        links = list(dict.fromkeys(links))
        self.resolve(links)
        added = 0
        for link in links:
            if added >= self.o.max_seeds:
                break
            canon_link = self.canonical(link)
            if canon_link == canon or canon_link in self.seeds:
                continue
            reason = self.reject_reason(link)
            if reason:
                self.filtered.setdefault(canon_link, (reason, set()))[1].add(canon)
                continue
            self.seeds.append(canon_link)
            added += 1
        self.log(f"Hub '{canon}': using {added} linked concepts as seeds")

    def _expand(self, title):
        canon, html = self.client.lead(title)
        if html is None:
            return False
        if canon != title:   # a title we only knew through a redirect
            self.info.setdefault(title, {"title": canon, "description": ""})
        blocks = parse_lead(html)[: self.o.lead_paragraphs + 2]
        text_blocks = [b for b in blocks if len(b["text"]) > 40] or blocks
        desc = self.info.get(title, {}).get("description", "")
        self.nodes[title] = {
            "title": title,
            "description": desc,
            "definition": first_sentences(text_blocks[0]["text"]) if text_blocks else "",
            "long_description": "\n\n".join(b["text"] for b in text_blocks[:3]),
            "expanded": title not in self.terminals,
        }
        if title in self.terminals:
            return True

        candidates = []
        for block in text_blocks[: self.o.lead_paragraphs]:
            for link, in_parens in block["links"]:
                if not in_parens:
                    candidates.append(link)
        candidates = list(dict.fromkeys(candidates))
        self.resolve(candidates)
        kept = []
        for link in candidates:
            canon_link = self.canonical(link)
            if canon_link == title or canon_link in kept:
                continue
            reason = self.reject_reason(link)
            if reason:
                self.filtered.setdefault(canon_link, (reason, set()))[1].add(title)
                continue
            kept.append(canon_link)
            if len(kept) >= self.o.max_prereqs:
                break
        for p in kept:
            self.prereqs[title].append(p)
            self.cited_by[p].add(title)
            self.depth[p] = min(self.depth.get(p, 10 ** 9), self.depth[title] + 1)
        return True

    def _settle_frontier(self):
        """Keep unexpanded concepts cited by several pages as stub leaves; drop the rest."""
        frontier = {p for ps in self.prereqs.values() for p in ps if p not in self.nodes}
        keep = sorted(p for p in frontier
                      if len(self.cited_by[p]) >= self.o.min_support or p in self.terminals)
        self.frontier_dropped = sorted(frontier - set(keep))
        self.frontier_kept = keep
        for t in keep:
            self.nodes[t] = {"title": t, "description": self.info.get(t, {}).get("description", ""),
                             "definition": "", "long_description": "", "expanded": False}
        for chunk_start in range(0, len(keep), 20):
            chunk = keep[chunk_start: chunk_start + 20]
            data = self.client.api({"action": "query", "titles": "|".join(chunk), "prop": "extracts",
                                    "exintro": "1", "explaintext": "1", "exsentences": "2",
                                    "exlimit": "20", "redirects": "1"})
            for page in data.get("query", {}).get("pages", []):
                if page.get("title") in self.nodes:
                    self.nodes[page["title"]]["definition"] = page.get("extract", "")
        for t in list(self.prereqs):
            self.prereqs[t] = [p for p in self.prereqs[t] if p in self.nodes]


def first_sentences(text, limit=420):
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind(".\n"))
    return cut[: end + 1] if end > 80 else cut.rstrip() + "…"


# ---------------------------------------------------------------------------
# Graph post-processing
# ---------------------------------------------------------------------------

def strongly_connected_components(nodes, children):
    """Iterative Tarjan. Returns a list of components (lists of node keys)."""
    index, low, on_stack, stack, comps = {}, {}, set(), [], []
    counter = 0
    for root in nodes:
        if root in index:
            continue
        work = [(root, iter(children[root]))]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            v, it = work[-1]
            advanced = False
            for w in it:
                if w not in index:
                    index[w] = low[w] = counter
                    counter += 1
                    stack.append(w)
                    on_stack.add(w)
                    work.append((w, iter(children[w])))
                    advanced = True
                    break
                if w in on_stack:
                    low[v] = min(low[v], index[w])
            if advanced:
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[v])
            if low[v] == index[v]:
                comp = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    comp.append(w)
                    if w == v:
                        break
                comps.append(comp)
    return comps


class DAG:
    """Prerequisite graph: edge p -> d means p is a prerequisite of d."""

    def __init__(self, keys):
        self.keys = list(keys)
        self.children = {k: [] for k in self.keys}
        self.parents = {k: [] for k in self.keys}

    def add(self, p, d):
        if p != d and p in self.children and d in self.children and d not in self.children[p]:
            self.children[p].append(d)
            self.parents[d].append(p)

    def remove(self, p, d):
        if d in self.children.get(p, []):
            self.children[p].remove(d)
            self.parents[d].remove(p)

    def drop_node(self, k):
        for p in list(self.parents[k]):
            self.remove(p, k)
        for d in list(self.children[k]):
            self.remove(k, d)
        del self.children[k], self.parents[k]
        self.keys.remove(k)

    def edges(self):
        return [(p, d) for p in self.keys for d in self.children[p]]

    def break_cycles(self, generality):
        """Within each cyclic component, order nodes from most to least general and
        drop every edge that points the other way. Returns the removed edges."""
        removed = []
        for comp in strongly_connected_components(self.keys, self.children):
            if len(comp) < 2:
                continue
            members = set(comp)
            order = {k: i for i, k in enumerate(sorted(comp, key=generality))}
            for p in comp:
                for d in list(self.children[p]):
                    if d in members and order[p] > order[d]:
                        self.remove(p, d)
                        removed.append((p, d))
        return removed

    def topo_order(self):
        indeg = {k: len(self.parents[k]) for k in self.keys}
        queue = deque(sorted(k for k in self.keys if indeg[k] == 0))
        order = []
        while queue:
            k = queue.popleft()
            order.append(k)
            for d in self.children[k]:
                indeg[d] -= 1
                if indeg[d] == 0:
                    queue.append(d)
        if len(order) != len(self.keys):
            raise RuntimeError("graph still has a cycle")
        return order

    def levels(self):
        """Longest path from any root: roots are level 0."""
        level = {}
        for k in self.topo_order():
            level[k] = 1 + max((level[p] for p in self.parents[k]), default=-1)
        return level

    def transitive_reduction(self):
        order = self.topo_order()
        bit = {k: 1 << i for i, k in enumerate(order)}
        reach = {}
        for k in reversed(order):
            acc = 0
            for d in self.children[k]:
                acc |= bit[d] | reach[d]
            reach[k] = acc
        removed = 0
        for k in order:
            via_others = 0
            for d in self.children[k]:
                via_others |= reach[d]
            for d in list(self.children[k]):
                if via_others & bit[d]:
                    self.remove(k, d)
                    removed += 1
        return removed

    # -- metrics (computed on the full DAG, before transitive reduction) -----
    def closure_sizes(self, direction):
        order = self.topo_order()
        nbrs = self.children if direction == "down" else self.parents
        seq = reversed(order) if direction == "down" else order
        bit = {k: 1 << i for i, k in enumerate(order)}
        acc = {}
        for k in seq:
            s = 0
            for n in nbrs[k]:
                s |= bit[n] | acc[n]
            acc[k] = s
        return {k: bin(v).count("1") for k, v in acc.items()}

    def betweenness(self):
        """Brandes' algorithm, directed and unweighted, normalised by (n-1)(n-2)."""
        bc = dict.fromkeys(self.keys, 0.0)
        for s in self.keys:
            stack, preds = [], defaultdict(list)
            sigma = defaultdict(float)
            sigma[s] = 1.0
            dist = {s: 0}
            q = deque([s])
            while q:
                v = q.popleft()
                stack.append(v)
                for w in self.children[v]:
                    if w not in dist:
                        dist[w] = dist[v] + 1
                        q.append(w)
                    if dist[w] == dist[v] + 1:
                        sigma[w] += sigma[v]
                        preds[w].append(v)
            delta = defaultdict(float)
            while stack:
                w = stack.pop()
                for v in preds[w]:
                    delta[v] += sigma[v] / sigma[w] * (1 + delta[w])
                if w != s:
                    bc[w] += delta[w]
        n = len(self.keys)
        scale = 1.0 / ((n - 1) * (n - 2)) if n > 2 else 0.0
        return {k: v * scale for k, v in bc.items()}

    def pagerank(self, damping=0.85, iters=100, tol=1e-9):
        n = len(self.keys)
        if not n:
            return {}
        rank = dict.fromkeys(self.keys, 1.0 / n)
        for _ in range(iters):
            dangling = sum(rank[k] for k in self.keys if not self.children[k])
            base = (1 - damping) / n + damping * dangling / n
            nxt = {k: base + damping * sum(rank[p] / len(self.children[p]) for p in self.parents[k])
                   for k in self.keys}
            delta = sum(abs(nxt[k] - rank[k]) for k in self.keys)
            rank = nxt
            if delta < tol:
                break
        return rank


def label_propagation(dag, rounds=30):
    """Community detection on the undirected graph; deterministic."""
    nbrs = {k: dag.children[k] + dag.parents[k] for k in dag.keys}
    label = {k: k for k in dag.keys}
    order = sorted(dag.keys)
    for _ in range(rounds):
        changed = 0
        for k in order:
            if not nbrs[k]:
                continue
            counts = Counter(label[n] for n in nbrs[k])
            best = max(counts.values())
            choice = min(l for l, c in counts.items() if c == best)
            if choice != label[k]:
                label[k] = choice
                changed += 1
        if not changed:
            break
    return label


def node_id(title):
    return hashlib.sha256(title.lower().strip().encode()).hexdigest()[:8]


def display_label(title, taken):
    base = re.sub(r"\s*\([^)]*\)$", "", title).strip() or title
    return base if taken[base.lower()] <= 1 else title


def build_graph(crawler, opts, client):
    cur = crawler.cur
    nodes = crawler.nodes
    dag = DAG(sorted(nodes))
    for d, ps in crawler.prereqs.items():
        for p in ps:
            dag.add(p, d)

    review = {"curated_edges_missing_nodes": []}
    canon = crawler.canonical
    for p, d in cur["remove_edges"]:
        dag.remove(canon(p), canon(d))
    for p, d in cur["add_edges"]:
        cp, cd = canon(p), canon(d)
        if cp in nodes and cd in nodes:
            dag.add(cp, cd)
        else:
            review["curated_edges_missing_nodes"].append([p, d])

    def generality(k):
        # Widely cited, deep-in-the-crawl concepts sort first (most general).
        return (-len(dag.children[k]), -crawler.depth.get(k, 0), k)

    cycle_edges = dag.break_cycles(generality)
    orphans = [k for k in dag.keys if not dag.children[k] and not dag.parents[k] and k not in crawler.seeds]
    for k in orphans:
        dag.drop_node(k)

    n = len(dag.keys)
    level = dag.levels()
    desc = dag.closure_sizes("down")
    pre = dag.closure_sizes("up")
    bc = dag.betweenness()
    pr = dag.pagerank()
    full_edges = len(dag.edges())
    level_hist = Counter(level.values())
    lower, running = {}, 0
    for lv in sorted(level_hist):
        lower[lv] = running
        running += level_hist[lv]

    reduced = dag.transitive_reduction() if not opts.keep_transitive else 0

    community = label_propagation(dag)
    groups = defaultdict(list)
    for k, c in community.items():
        groups[c].append(k)
    ranked = sorted(groups.values(), key=len, reverse=True)
    category_of = {}
    for i, members in enumerate(ranked):
        if i < opts.max_categories and len(members) >= 3:
            name = max(members, key=lambda k: (len(dag.children[k]) + len(dag.parents[k]), desc[k], k))
            name = display_label(name, Counter())
        else:
            name = "Other"
        for k in members:
            category_of[k] = name
    for title, cat in cur["categories"].items():
        if canon(title) in category_of:
            category_of[canon(title)] = cat

    taken = Counter(re.sub(r"\s*\([^)]*\)$", "", k).strip().lower() for k in dag.keys)
    ids = {k: node_id(k) for k in dag.keys}
    out_nodes = []
    for k in sorted(dag.keys, key=lambda k: (level[k], k)):
        node = nodes[k]
        out_nodes.append({
            "id": ids[k],
            "label": display_label(k, taken),
            "title": k,
            "url": client.article_url(k),
            "to": [ids[d] for d in dag.children[k]],
            "from": [ids[p] for p in dag.parents[k]],
            "category": category_of.get(k, "Other"),
            "description": node["description"],
            "definition": node["definition"],
            "long_description": node["long_description"],
            "_level": level[k],
            "_seed": k in crawler.seeds,
            "_expanded": node["expanded"],
            "_terminal": k in crawler.terminals,
            "_pagerank": pr[k],
            "_degree_centrality": (len(dag.children[k]) + len(dag.parents[k])) / (2 * (n - 1)) if n > 1 else 0.0,
            "_betweenness_centrality": bc[k],
            "_descendant_ratio": desc[k] / max(1, n - lower[level[k]] - level_hist[level[k]]),
            "_prerequisite_ratio": pre[k] / max(1, lower[level[k]]),
            "_reachability_ratio": (desc[k] + pre[k]) / n,
            "_descendants": desc[k],
            "_prerequisites": pre[k],
        })

    hub_threshold = max(5, int(0.15 * sum(1 for k in dag.keys if nodes[k]["expanded"])))
    filtered = sorted(((t, r, sorted(by)) for t, (r, by) in crawler.filtered.items()),
                      key=lambda x: (-len(x[2]), x[0]))
    review.update({
        "cycle_edges_removed": [[p, d] for p, d in cycle_edges],
        "possible_hubs": sorted(
            ([k, len(crawler.cited_by[k])] for k in dag.keys if len(crawler.cited_by[k]) >= hub_threshold),
            key=lambda x: -x[1]),
        "filtered": [{"title": t, "reason": r, "cited_by": by[:8], "count": len(by)}
                     for t, r, by in filtered if len(by) >= 2][:300],
        "filtered_counts": dict(Counter(r for _, (r, _) in crawler.filtered.items())),
        "unexpanded_kept": crawler.frontier_kept,
        "unexpanded_dropped": len(crawler.frontier_dropped),
        "orphans_removed": orphans,
        "no_description": [k for k in dag.keys if not nodes[k]["description"]][:200],
    })
    meta = {
        "generator": f"wikigraph {VERSION}",
        "source": f"{client.lang}.wikipedia.org",
        "built": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "topics": opts.topics,
        "hubs": crawler.hubs,
        "seeds": crawler.seeds,
        "budget_hit": crawler.budget_hit,
        "nodes": n,
        "edges": sum(len(x["to"]) for x in out_nodes),
        "edges_before_reduction": full_edges,
        "transitive_edges_removed": reduced,
        "max_level": max(level.values(), default=0),
        "options": {k: getattr(opts, k) for k in ("max_nodes", "max_depth", "max_prereqs",
                                                  "lead_paragraphs", "min_support", "max_seeds")},
    }
    return {"meta": meta, "review": review, "nodes": out_nodes}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_build(opts):
    client = WikiClient(lang=opts.lang, cache_dir=opts.cache, contact=opts.contact, delay=opts.delay)
    curation = load_curation(opts.curation)
    log = (lambda *a, **k: None) if opts.quiet else (lambda *a: print(*a, file=sys.stderr))
    log(f"Crawling {', '.join(opts.topics)} (budget {opts.max_nodes} concepts)")
    crawler = Crawler(client, opts, curation, log=log).run(opts.topics)
    graph = build_graph(crawler, opts, client)
    payload = graph["nodes"] if opts.array else graph
    with open(opts.output, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    m = graph["meta"]
    log(f"Wrote {opts.output}: {m['nodes']} concepts, {m['edges']} prerequisite edges, "
        f"{m['max_level'] + 1} levels ({client.requests} requests, {client.cache_hits} from cache)")
    print_review(graph, out=sys.stderr, brief=True)


def print_review(graph, out=sys.stdout, brief=False):
    r = graph.get("review", {})
    m = graph.get("meta", {})
    p = lambda *a: print(*a, file=out)
    if m.get("budget_hit"):
        p("• The node budget was reached. Raise --max-nodes or narrow the seeds for a fuller graph.")
    if r.get("cycle_edges_removed"):
        p(f"• {len(r['cycle_edges_removed'])} edges were dropped to break cycles. Check that the direction kept is right:")
        for pr, d in r["cycle_edges_removed"][: 8 if brief else None]:
            p(f"    removed: {pr} -> {d}")
    if r.get("possible_hubs"):
        p("• Very widely cited concepts (may be too generic, consider 'exclude' or 'terminal'):")
        for t, c in r["possible_hubs"][: 8 if brief else None]:
            p(f"    {t} (cited by {c})")
    if r.get("filtered"):
        p(f"• {sum(r.get('filtered_counts', {}).values())} linked pages were filtered out "
          f"({', '.join(f'{k}: {v}' for k, v in sorted(r['filtered_counts'].items()))}).")
        p("  The most cited ones (add to 'include' if any is a real concept):")
        for f in r["filtered"][: 8 if brief else 40]:
            p(f"    {f['title']} [{f['reason']}] cited by {f['count']}")
    if r.get("unexpanded_kept"):
        p(f"• {len(r['unexpanded_kept'])} concepts were kept without expanding them (budget/depth); "
          f"{r.get('unexpanded_dropped', 0)} singly-cited ones were dropped.")
    if r.get("curated_edges_missing_nodes"):
        p(f"• Curated edges skipped because a node is missing: {r['curated_edges_missing_nodes']}")
    if brief:
        p("Run `wikigraph.py review <file>` for the full list, or open the file in explorer.html.")


def cmd_review(opts):
    with open(opts.path, encoding="utf-8") as f:
        graph = json.load(f)
    if isinstance(graph, list):
        raise SystemExit("This file is a bare node list (--array); it has no review data.")
    print_review(graph)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="crawl Wikipedia and write a graph")
    b.add_argument("topics", nargs="+", help="Wikipedia article titles to start from")
    b.add_argument("-o", "--output", default="graph.json")
    b.add_argument("--max-nodes", type=int, default=400, help="concepts to expand (the hard budget)")
    b.add_argument("--max-depth", type=int, default=25, help="max prerequisite hops from a seed")
    b.add_argument("--max-prereqs", type=int, default=8, help="prerequisites kept per concept")
    b.add_argument("--lead-paragraphs", type=int, default=1,
                   help="paragraphs of the lead treated as the definition")
    b.add_argument("--min-support", type=int, default=2,
                   help="unexpanded concepts are kept only if this many pages cite them")
    b.add_argument("--max-seeds", type=int, default=60, help="seeds taken from a hub page")
    b.add_argument("--max-categories", type=int, default=14)
    b.add_argument("--hub", action="store_true", help="treat every topic as a hub page")
    b.add_argument("--no-auto-hub", dest="auto_hub", action="store_false",
                   help="never treat field/outline pages as hubs")
    b.add_argument("--terminal", dest="terminals", action="append",
                   help="never expand this title (repeatable; replaces the default bedrock set)")
    b.add_argument("--stop", action="append", default=[], help="exclude this title (repeatable)")
    b.add_argument("--keep-transitive", action="store_true", help="skip transitive reduction")
    b.add_argument("--curation", default="curation.json", help="human edits to apply (if the file exists)")
    b.add_argument("--cache", default=".wikigraph-cache")
    b.add_argument("--lang", default="en")
    b.add_argument("--contact", default="not provided",
                   help="email or URL for the User-Agent, as Wikimedia's API policy asks")
    b.add_argument("--delay", type=float, default=0.15, help="seconds between uncached requests")
    b.add_argument("--array", action="store_true",
                   help="write a bare node list (loadable by the-palindrome/ml-knowledge-graph)")
    b.add_argument("-q", "--quiet", action="store_true")

    r = sub.add_parser("review", help="print the things a human should check")
    r.add_argument("path")

    opts = ap.parse_args(argv)
    if opts.cmd == "build":
        opts.terminals = opts.terminals or list(DEFAULT_TERMINALS)
        cmd_build(opts)
    else:
        cmd_review(opts)


if __name__ == "__main__":
    main()
