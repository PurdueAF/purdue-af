# Contributing to documentation

This website is rendered from Markdown documentation in the
[Purdue AF GitHub repository](https://github.com/PurdueAF/purdue-af/tree/main/docs).

To contribute to the documentation, please **open a pull request** — or simply
click the "edit" icon at the top of any page to edit it directly on GitHub.

## How the site is built

* The documentation is written in **Markdown** and built with
  [Zensical](https://zensical.org/), a static site generator from the authors of
  Material for MkDocs.
* The site configuration (navigation, theme, Markdown extensions) lives in a
  single file: `zensical.toml`.
* On every push to the `main` branch that touches the docs, a GitHub Actions
  workflow rebuilds the site and deploys it to **GitHub Pages**.

## Previewing changes locally

```shell
pip install zensical

cd docs
zensical serve
```

The site will be available at `http://localhost:8000` and rebuilt automatically
as you edit the files.

## Style conventions

* One page per topic; cross-link related pages liberally.
* Use admonitions (`!!! note`, `!!! warning`, `!!! tip`) for asides, and
  collapsible blocks (`??? note`) for long optional content.
* Always state explicitly whether a feature applies to Purdue users, CERN/FNAL
  users, or everyone.
* Prefer copy-pasteable code blocks over screenshots of terminals.
