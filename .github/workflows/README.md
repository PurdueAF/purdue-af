# CI/CD at a glance

Every commit goes through one pipeline ([ci.yml](ci.yml)). Nothing reaches
users unless every step passed for that exact commit. Automation stops at
the pre-release channel — putting things into production is always a human
decision (see [RELEASING.md](../../RELEASING.md)).

```mermaid
%%{init: {"flowchart": {"wrappingWidth": 380}}}%%
flowchart TD
    PR([commit pushed / PR opened]) --> CHK
    CHK["<b>checks</b><br/>formatting · unit tests · config validation"] --> PB
    PB["<b>pixi-base</b> — the Jupyter environment<br/>rebuild lockfile · check all imports"] --> IMG
    IMG["<b>container images</b> — incl. the Purdue AF image<br/>rebuilt only when their files change;<br/>otherwise reuse the already-tested build"] --> PG
    IMG --> E2E
    PG["<b>pixi-global</b> — shared analysis environment<br/>rebuild lockfile · import check in the AF image"] --> OK
    E2E["<b>e2e</b> — JupyterHub in a throwaway cluster<br/>log in · spawn the real AF image · CVMFS"] --> OK
    OK{{"<b>ci-ok</b> — did every step above pass?"}}
    OK -->|yes, and this is main| PUB
    PUB["<b>publish</b><br/>images get :latest / :pre-release tags;<br/>the main-validated branch advances"]
    PUB --> EXP["<b>Flux: experimental</b><br/>auto-deploys from main-validated<br/>(incl. pixi-global-sync → /work)"]
    REL["<b>release-image / release-platform</b><br/>a person decides when + which version"] --> PROD
    PROD["<b>Flux: production</b><br/>runs released versions only"]
    OK -.->|only fully-tested commits<br/>can be released| REL
```
