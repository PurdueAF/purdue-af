# Contributing to the docs

This website is rendered from Markdown documentation in the
[Purdue AF GitHub repository](https://github.com/PurdueAF/purdue-af/tree/main/docs).

To contribute to the documentation, please **open a pull request** — or simply
click the "edit" icon at the top of any page to edit it directly on GitHub.

## How the site is built

* The documentation is written in **Markdown** and built with
  [Zensical](https://zensical.org/), a static site generator from the authors of
  Material for MkDocs.
* The site configuration (navigation, theme, Markdown extensions) lives in a
  single file: `zensical.toml`. The navigation has one section per topic, with
  the topic's overview page first.
* A new page is added in two places: `nav` in `zensical.toml`, and the
  list of pages in `flux/experimental/kustomization.yaml`, which is what AI
  agents read through the facility's tools.
* On every push to the `main` branch that touches `docs/` or `pixi/` (the
  software page embeds the environment manifest), a GitHub Actions workflow
  rebuilds the site and deploys it to **GitHub Pages**.

## Previewing changes locally

```shell
pip install zensical

cd docs
zensical serve
```

The site will be available at `http://localhost:8000` and rebuilt automatically
as you edit the files.

## Style conventions

The pages are read by people and by AI agents, which get a page as Markdown
(`/llms.txt` lists them) and choose it by its name and title.

* One page per topic, with a short `#` title that says what the page is for. It
  is the page's label in the sidebar, so `nav` lists pages without labels.
  Never rename a page: its name is its address, on the site and for agents.
* Open with what the page covers and who it applies to — Purdue users, CERN/FNAL
  users, or everyone.
* State a fact on one page and link to it from the others.
* Give complete, copy-pasteable commands in code blocks, and quote error
  messages exactly.
* Keep the main answer in the page's text: tabs are for per-platform variants,
  collapsible blocks (`??? note`) for optional detail, admonitions for asides.
