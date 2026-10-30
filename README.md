# Wikigraph

Builds a prerequisite graph of any topic from Wikipedia and lets you explore it in 2D or 3D.

[![Wikigraph 3D explorer showing the machine-learning graph](docs/screenshot-3d.png)](https://fellobello.github.io/wikigraph/)

**[Live demo](https://fellobello.github.io/wikigraph/)**: click any concept to see its definition,
prerequisites, dependents and a learning path. [`ml.json`](ml.json) is a 413-concept graph built from
"Machine learning"; drop it on the explorer page to view it.

## Quick start

Needs Python 3.9+ and access to `*.wikipedia.org`.

```
python3 wikigraph.py build "Machine learning" -o ml.json --contact you@example.com
python3 wikigraph.py review ml.json    # lists what a human should check
python3 -m http.server                 # then open http://localhost:8000/explorer.html
```

In the explorer, press 2 or 3 to switch views. In 3D, drag to rotate, right-drag to pan and scroll
to zoom. The 3D view loads three.js from cdnjs, so it needs an internet connection.

## How it stays finite

Following every Wikipedia link never ends, so the builder follows only links in each article's
**definition** (the first lead paragraph, outside parentheses). Those links point at more basic ideas,
so the crawl narrows toward foundations.

| Problem | Fix |
| --- | --- |
| Noise (people, places, years, "List of…") | Filtered by short description and title; logged in `review.filtered` |
| Broad fields ("Statistics") | Filtered out; a field used as the topic becomes a **hub** whose links are the seeds |
| Budget | `--max-nodes` caps expansion, most-cited concepts first; `--min-support` drops rare leaves |
| Bottomless regress | Terminal concepts (set, equality, natural number…) are never expanded; add more with `--terminal` |
| Cycles | Keeps edges from the more-cited concept to the less-cited one; dropped edges are listed for review |
| Hairball | Transitive reduction removes A→C when A→B→C exists (metrics are computed first) |

Each node gets a level (its longest prerequisite chain), betweenness centrality, PageRank and closure sizes.

## Curation

`curation.json` (see `curation.example.json`) excludes, includes, merges, adds edges and more. The
explorer's **Review** tab writes it for you. Rebuilds reuse `.wikigraph-cache/`, so they're fast.

## Output

`{meta, review, nodes}`, with nodes in the Palindrome format plus `title`, `url` and `_level`.
`--array` writes a bare node list for the Palindrome 3D explorer.

## Tests

`python3 -m unittest test_wikigraph` runs the full pipeline against a small fake Wikipedia.

## Credits

- Inspired by The Palindrome's "Map of Machine Learning" video, from the post
  [I Built the Knowledge Graph of Machine Learning](https://thepalindrome.org/p/i-built-the-knowledge-graph-of-machine),
  and its repository [the-palindrome/ml-knowledge-graph](https://github.com/the-palindrome/ml-knowledge-graph).
- `sample-ml.json` is derived from that repository's graph, MIT License, Copyright (c) 2026 Palindrome Labs.
  The full licence text is in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
- The 3D view uses [three.js](https://threejs.org) (MIT), loaded from cdnjs.
- Concept text and links come from [Wikipedia](https://en.wikipedia.org), available under CC BY-SA.

## Licence

Copyright (c) 2026 Steven Cochrane. All rights reserved. The code in this repository is published
for viewing; no licence is granted to reuse it. Third-party material keeps its own licence (see
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)).
