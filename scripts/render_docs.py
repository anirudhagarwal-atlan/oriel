#!/usr/bin/env python3
"""Render first-party Markdown for direct browser reading, without a web build.

Run with the existing locked connectors environment:
    .venv/bin/python scripts/render_docs.py
    .venv/bin/python scripts/render_docs.py --check

Uses the existing markdown-it-py dependency; installs nothing. Markdown remains
canonical. Raw HTML and remote images are disabled. Simple Mermaid flowcharts
become accessible edge lists; complex diagrams link to their canonical source.
Outputs contain no timestamps, external assets, JavaScript, or network requests.
"""
from __future__ import annotations

import argparse
import hashlib
import html
from html.parser import HTMLParser
from importlib import metadata
import os
from pathlib import Path
import re
import sys
from urllib.parse import quote, unquote, urlsplit, urlunsplit


ROOT = Path(__file__).resolve().parents[1]
RENDERER_VERSION = "4.2.0"
NAVIGATION = (("Quickstart", "docs/quickstart.html"), ("Connectors", "docs/connectors.html"),
              ("Agent setup", "docs/agents.html"), ("Context guide", "docs/context-guide.html"),
              ("FAQ", "docs/faq.html"), ("All documentation", "docs/README.html"))
CSS = """
:root{color-scheme:light;--paper:#f7f7f1;--surface:#fff;--ink:#24332d;--muted:#52635a;--line:#d5ddd4;--accent:#305d47}
*{box-sizing:border-box}html{scroll-padding-top:1.5rem}body{margin:0;background:var(--paper);color:var(--ink);font:16px/1.75 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
a{color:var(--accent);text-underline-offset:.2em}a:hover{text-decoration-thickness:2px}a:focus-visible{outline:3px solid var(--accent);outline-offset:4px}
.skip{position:absolute;left:1rem;top:-5rem;padding:.5rem 1rem;background:var(--surface)}.skip:focus{top:1rem}
.site-header{max-width:1300px;margin:auto;padding:1.4rem 2rem;border-bottom:1px solid var(--line);display:flex;align-items:center;justify-content:space-between;gap:1rem}
.brand{font-size:1.3rem;font-weight:750;letter-spacing:-.035em;text-decoration:none;color:var(--ink)}.site-header span{color:var(--muted);font-size:.8rem}
.layout{max-width:1300px;margin:auto;display:grid;grid-template-columns:220px minmax(0,1fr);gap:3.5rem;padding:2rem}
aside{align-self:start;position:sticky;top:1.5rem;font-size:.88rem}aside nav{display:grid;gap:.25rem}aside a{display:block;padding:.35rem .55rem;border-radius:4px;text-decoration:none}aside a[aria-current=page]{background:#e6eee6;font-weight:650}aside h2{font-size:.72rem;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);margin:1.7rem 0 .5rem}.toc{border-top:1px solid var(--line);margin-top:1rem}.toc a{font-size:.82rem;line-height:1.5;margin-bottom:.2rem}
main{min-width:0;max-width:850px}article{background:var(--surface);padding:2.2rem 2.6rem;border:1px solid var(--line);border-radius:8px}h1,h2,h3,h4{line-height:1.3;letter-spacing:-.025em}h1{font-size:2.3rem;margin:0 0 1.4rem}h2{font-size:1.55rem;margin:2.5rem 0 1rem;padding-top:.25rem}h3{font-size:1.2rem;margin:1.8rem 0 .7rem}h4{font-size:1rem;margin:1.4rem 0 .6rem}p,ul,ol,blockquote,pre,table{margin:1rem 0}li+li{margin-top:.35rem}li p{margin:.45rem 0}blockquote{border-left:3px solid #91aa96;padding:.1rem 1.25rem;margin-left:0;color:var(--muted)}hr{border:0;border-top:1px solid var(--line);margin:2rem 0}
code{font-family:ui-monospace,SFMono-Regular,Consolas,"Liberation Mono",monospace;font-size:.85em;background:#f0f3ee;padding:.12em .3em;border-radius:3px;overflow-wrap:anywhere}pre{background:#1f3029;color:#eff5eb;border-radius:6px;padding:1.1rem 1.3rem;overflow-x:auto;font-size:.88rem;line-height:1.65;tab-size:4}pre code{font-size:inherit;background:none;padding:0;color:inherit;border-radius:0;overflow-wrap:normal}table{border-collapse:collapse;width:100%;font-size:.87rem;display:block;overflow-x:auto}th,td{text-align:left;vertical-align:top;padding:.7rem .9rem;border:1px solid var(--line);min-width:120px}th{background:#eef3eb;font-weight:650}img{max-width:100%;height:auto}footer{color:var(--muted);font-size:.8rem;margin:1.5rem .2rem 2rem;display:flex;justify-content:space-between;gap:1rem;flex-wrap:wrap}footer p{margin:0}
.workflow{margin:1.5rem 0;padding:1.1rem 1.4rem;background:#f2f6ef;border:1px solid var(--line);border-radius:6px}.workflow figcaption{font-weight:650}.workflow ol{padding-left:1.3rem;font-size:.92rem}.workflow small{color:var(--muted)}.visually-hidden{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}
@media(max-width:850px){.layout{grid-template-columns:1fr;gap:1.2rem;padding:1.2rem}aside{position:static}aside nav{display:flex;flex-wrap:wrap;gap:.2rem .5rem}aside .toc{display:none}article{padding:1.5rem}h1{font-size:1.9rem}.site-header{padding:1rem 1.2rem}}@media(max-width:450px){article{padding:1.1rem}.layout{padding:.7rem}.site-header span{display:none}pre{padding:.8rem}}
""".strip()


def relative(target: Path, directory: Path) -> str:
    return quote(Path(os.path.relpath(target, directory)).as_posix(), safe="/._-")


def plain(tokens):
    return "".join(plain(token.children) if token.children else
                   " " if token.type in {"softbreak", "hardbreak"} else
                   token.content if token.type in {"text", "code_inline", "html_inline", "image"} else ""
                   for token in tokens)


def heading_slug(value):
    # Match the heading-link conventions used by GitHub Markdown: punctuation
    # is removed, whitespace becomes '-', and duplicate IDs gain numeric suffixes.
    value = "".join(char for char in value.lower() if char.isalnum() or char in "_ -" or char.isspace())
    return re.sub(r"\s", "-", value)


def local_target(root, source, path):
    decoded = unquote(path)
    if decoded.startswith("/") or "\\" in decoded or any(ord(char) < 32 for char in decoded):
        raise ValueError("Unsafe local documentation path in " + str(source.relative_to(root)))
    target = (source.parent / decoded).resolve()
    if not target.is_relative_to(root):
        raise ValueError("Documentation link leaves the repository in " + str(source.relative_to(root)))
    return target


def rewrite_url(value, root, source, mirrors, *, image=False):
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc:
        if (image or parsed.scheme not in {"http", "https", "mailto"}
                or parsed.username is not None or parsed.password is not None):
            raise ValueError("Unsupported external documentation link in " + str(source.relative_to(root)))
        return value
    if not parsed.path:
        return value
    target = local_target(root, source, parsed.path)
    if image and target.suffix.lower() not in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}:
        raise ValueError("Unsupported documentation image in " + str(source.relative_to(root)))
    if target in mirrors:
        target = mirrors[target]
    return urlunsplit(("", "", relative(target, source.parent), parsed.query, parsed.fragment))


def flowchart(content, source_href):
    """A deliberately small text fallback; never evaluate diagram directives."""
    source_link = f'<a href="{html.escape(source_href, quote=True)}">Diagram source in Markdown</a>'
    fallback = '<p>A diagram accompanies this section. ' + source_link + '.</p>\n'
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    if not lines or not re.fullmatch(r"flowchart (?:LR|RL|TD|TB|BT)", lines[0]):
        return fallback
    node = r"([A-Za-z][A-Za-z0-9_]*)(?:\[([^\]\n]+)\])?"
    edge = re.compile(node + r"\s*(?:-->|-\.\s*([^\n]+?)\s*\.->)\s*" + node)
    names, edges = {}, []
    for line in lines[1:]:
        match = edge.fullmatch(line)
        if match is None:
            return fallback
        left, left_label, relationship, right, right_label = match.groups()
        for key, label in ((left, left_label), (right, right_label)):
            if label:
                if key in names and names[key] != label:
                    return fallback
                names[key] = label
        edges.append((left, right, relationship))
    if not edges or any(left not in names or right not in names for left, right, _ in edges):
        return fallback
    rows = []
    for left, right, relationship in edges:
        note = f' <small>({html.escape(relationship)})</small>' if relationship else ""
        rows.append(f'<li>{html.escape(names[left])} <span aria-hidden="true">→</span>'
                    f' <span class="visually-hidden">to</span> {html.escape(names[right])}{note}</li>')
    return ('<figure class="workflow"><figcaption>Workflow</figcaption><ol>' + "\n".join(rows)
            + '</ol><small>' + source_link + '</small></figure>\n')


def prepare(root):
    try:
        if metadata.version("markdown-it-py") != RENDERER_VERSION:
            raise ValueError("Use the markdown-it-py version in the existing locked connectors environment.")
        from markdown_it import MarkdownIt
    except ImportError:
        raise ValueError("Run with the installed locked connectors environment; this script installs nothing.") from None
    markdown = MarkdownIt("commonmark", {"html": False, "linkify": False, "typographer": False}).enable(["table", "strikethrough"])
    normal_fence = markdown.renderer.rules["fence"]
    def fence(tokens, index, options, env):
        token = tokens[index]
        if token.info.strip() == "mermaid":
            return flowchart(token.content, env["source_href"])
        return normal_fence(tokens, index, options, env)
    markdown.renderer.rules["fence"] = fence
    sources = [root / "README.md", root / "CONTRIBUTING.md", *sorted((root / "docs").glob("*.md"))]
    if any(path.is_symlink() or not path.is_file() for path in sources):
        raise ValueError("Documentation inputs must be regular first-party Markdown files.")
    mirrors = {source: source.with_suffix(".html") for source in sources}
    if root / "docs/index.html" in mirrors.values():
        raise ValueError("The authored landing page cannot be overwritten by a Markdown mirror.")
    pages = {}
    for source in sources:
        content = source.read_text(encoding="utf-8")
        tokens = markdown.parse(content)
        headings, used = [], {"doc-content", "docs-navigation"}
        for index, token in enumerate(tokens):
            if token.type == "heading_open":
                title = plain(tokens[index + 1].children or [])
                base = heading_slug(title) or "section"
                anchor, suffix = base, 0
                while anchor in used:
                    suffix += 1
                    anchor = f"{base}-{suffix}"
                used.add(anchor)
                token.attrSet("id", anchor)
                headings.append((token.tag, title, anchor))
        def rewrite(tokens):
            for token in tokens:
                if token.type in {"link_open", "image"}:
                    key = "href" if token.type == "link_open" else "src"
                    value = token.attrGet(key)
                    if value is not None:
                        token.attrSet(key, rewrite_url(value, root, source, mirrors, image=token.type == "image"))
                if token.children:
                    rewrite(token.children)
        rewrite(tokens)
        title = next((title for level, title, _ in headings if level == "h1"), source.stem)
        body = markdown.renderer.render(tokens, markdown.options, {"source_href": relative(source, source.parent)})
        nav = "\n".join(f'<a href="{relative(root / target, source.parent)}"'
                          + (' aria-current="page"' if mirrors[source] == root / target else '')
                          + f'>{html.escape(label)}</a>' for label, target in NAVIGATION)
        toc = "\n".join(f'<a href="#{html.escape(anchor, quote=True)}">{html.escape(title)}</a>'
                          for level, title, anchor in headings if level == "h2")
        origin = source.relative_to(root).as_posix()
        digest = hashlib.sha256(content.encode()).hexdigest()
        pages[mirrors[source]] = f'''<!doctype html>
<!-- Generated by scripts/render_docs.py from {html.escape(origin)}; source SHA256 {digest}. Edit Markdown, then regenerate. -->
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src 'self'; base-uri 'none'; form-action 'none'">
<title>{html.escape(title)} · Oriel</title>
<style>{CSS}</style>
</head>
<body>
<a class="skip" href="#doc-content">Skip to content</a>
<header class="site-header"><a class="brand" href="{relative(root / 'README.html', source.parent)}">Oriel</a><span>Talk to your tools. Keep the definitions.</span></header>
<div class="layout">
<aside><nav id="docs-navigation" aria-label="Documentation">{nav}</nav>
<div class="toc"><h2>On this page</h2><nav aria-label="Page sections">{toc}</nav></div></aside>
<main id="doc-content"><article>
{body}</article>
<footer><p>Generated from <a href="{relative(source, source.parent)}">{html.escape(origin)}</a>. Markdown is the source.</p><a href="{relative(root / 'docs/index.html', source.parent)}">Project page</a></footer>
</main>
</div>
</body>
</html>
'''
    landing = root / "docs/index.html"
    landing_text = landing.read_text(encoding="utf-8")
    def link(match):
        value = html.unescape(match.group(2))
        new = rewrite_url(value, root, landing, mirrors)
        return match.group(1) + html.escape(new, quote=True) + match.group(3)
    # Only href attributes change in the authored landing. Its styles, script,
    # capture data, displayed commands and interactive behavior stay untouched.
    pages[landing] = re.sub(r'(\bhref=")([^"]*)(")', link, landing_text)
    return pages


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links, self.ids = [], set()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            if attrs["id"] in self.ids:
                raise ValueError("Duplicate generated HTML anchor")
            self.ids.add(attrs["id"])
        for key in ("href", "src"):
            if key in attrs:
                self.links.append(attrs[key])


def validate(root, pages):
    parsed = {}
    for path, content in pages.items():
        parser = Links()
        parser.feed(content)
        parsed[path] = parser
    errors = []
    for path, parser in parsed.items():
        for link in parser.links:
            value = urlsplit(link)
            if value.scheme or value.netloc:
                continue
            target = local_target(root, path, value.path) if value.path else path
            if target not in pages and not target.exists():
                errors.append(f"{path.relative_to(root)}: missing local target {value.path}")
            elif value.fragment and target in parsed and unquote(value.fragment) not in parsed[target].ids:
                errors.append(f"{path.relative_to(root)}: missing anchor {link}")
    return sorted(set(errors))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Verify generated bytes and local links without writing")
    args = parser.parse_args(argv)
    try:
        pages = prepare(ROOT)
        errors = validate(ROOT, pages)
        changed = []
        for path, content in pages.items():
            if path.is_symlink() or (path.exists() and not path.is_file()):
                raise ValueError("Generated documentation targets must be regular files.")
            if not path.exists() or path.read_bytes() != content.encode():
                changed.append(str(path.relative_to(ROOT)))
                if not args.check:
                    path.write_bytes(content.encode())
        if errors:
            print("Documentation link checks failed:\n" + "\n".join(errors), file=sys.stderr)
        if args.check and changed:
            print("Regenerate documentation:\n" + "\n".join(changed), file=sys.stderr)
        if errors or (args.check and changed):
            return 1
        print(f"{'Checked' if args.check else 'Rendered'} {len(pages) - 1} Markdown pages; "
              f"{len(changed)} changed files; local links and anchors verified.")
        return 0
    except (OSError, ValueError, metadata.PackageNotFoundError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
