# Baseline Results — DeepSeek-V4-Flash (GT-OFF)

Measured GT-off baseline floors for DeepSeek-V4-Flash. The GT-on arm must beat these.

## SWE-bench-Live Lite (300) — **29% baseline**

| | |
|---|---|
| Model | `deepseek/deepseek-v4-flash` (DeepSeek-direct) |
| Sampling | temperature=1.0, top_p=1.0, thinking **disabled** |
| Agent | OpenHands CodeActAgent, max_iterations=100, GT **off** |
| Eval | official Microsoft SWE-bench-Live harness, `--split lite --namespace starryzhang` |
| **Resolved** | **87 / 291 graded = 29.9%** &nbsp; (·/300 = 29.0%) |
| Ungraded | 9 (harness/infra, not model — see below) |
| Runs | 26701121318 original, 26714872036 recovery |
| Date | 2026-05-31 |

### Resolved tasks (87) — by repo

- **conan-io** (8): `conan-17123`, `conan-17129`, `conan-17183`, `conan-17266`, `conan-17300`, `conan-17382`, `conan-17532`, `conan-17967`
- **deepset-ai** (6): `haystack-8489`, `haystack-8609`, `haystack-8725`, `haystack-8969`, `haystack-8973`, `haystack-8997`
- **matplotlib** (5): `matplotlib-28933`, `matplotlib-29249`, `matplotlib-29258`, `matplotlib-29285`, `matplotlib-29388`
- **pvlib** (5): `pvlib-python-2249`, `pvlib-python-2292`, `pvlib-python-2341`, `pvlib-python-2393`, `pvlib-python-2400`
- **pdm-project** (4): `pdm-3237`, `pdm-3255`, `pdm-3419`, `pdm-3420`
- **instructlab** (3): `instructlab-2526`, `instructlab-3060`, `instructlab-3118`
- **joke2k** (3): `faker-2155`, `faker-2173`, `faker-2190`
- **pydata** (3): `xarray-9760`, `xarray-9971`, `xarray-9974`
- **python-babel** (3): `babel-1141`, `babel-1179`, `babel-1194`
- **reflex-dev** (3): `reflex-4087`, `reflex-4371`, `reflex-4427`
- **sissbruecker** (3): `linkding-984`, `linkding-989`, `linkding-995`
- **streamlink** (3): `streamlink-6242`, `streamlink-6361`, `streamlink-6439`
- **yt-dlp** (3): `yt-dlp-11425`, `yt-dlp-11880`, `yt-dlp-12684`
- **beeware** (2): `briefcase-2075`, `briefcase-2088`
- **dynaconf** (2): `dynaconf-1241`, `dynaconf-1249`
- **ipython** (2): `ipython-14695`, `ipython-14822`
- **koxudaxi** (2): `datamodel-code-generator-2259`, `datamodel-code-generator-2349`
- **kozea** (2): `weasyprint-2300`, `weasyprint-2303`
- **pylint-dev** (2): `pylint-10089`, `pylint-10240`
- **pytorch** (2): `torchtune-1806`, `torchtune-2139`
- **run-llama** (2): `llama_deploy-356`, `llama_deploy-384`
- **sphinx-doc** (2): `sphinx-12975`, `sphinx-13261`
- **amoffat** (1): `sh-744`
- **beancount** (1): `beancount-931`
- **beetbox** (1): `beets-5495`
- **cyclotruc** (1): `gitingest-94`
- **encode** (1): `starlette-2812`
- **hiyouga** (1): `llama-factory-7505`
- **icloud-photos-downloader** (1): `icloud_photos_downloader-1060`
- **jazzband** (1): `tablib-613`
- **pybamm-team** (1): `pybamm-4644`
- **pypa** (1): `twine-1225`
- **python-control** (1): `python-control-1111`
- **qtile** (1): `qtile-5154`
- **scrapy-plugins** (1): `scrapy-splash-324`
- **shapely** (1): `shapely-2226`
- **sympy** (1): `sympy-27462`
- **theoehrly** (1): `fast-f1-699`
- **wemake-services** (1): `wemake-python-styleguide-3117`

### Ungraded (9) — infrastructure, not model

- `conan-io__conan-17092`
- `conan-io__conan-17117`
- `conan-io__conan-17326`
- `conan-io__conan-17408`
- `conan-io__conan-17514`
- `conan-io__conan-17708`
- `conan-io__conan-17923`
- `matplotlib__matplotlib-29007`
- `pallets__flask-5626`

_7 conan = un-runnable in harness (no inference record → `incomplete`); matplotlib-29007 + flask-5626 = heavy-repo grinders, cancelled. Re-running infra failures (no recorded verdict) is recovery, not re-rolling._

## DeepSWE (113) — baseline in progress

- Smoke (5 tasks, stock mini-swe-agent, no GT, deepseek-v4-flash, thinking disabled): **0/5 resolved** (verified real — agent produced patches, verifier failed all; DeepSWE shuts out the Flash baseline).
- Per-task: ~23 min, **12-37M input tokens** (100-300 agent calls, full-history re-send). Full 113 ≈ ~$30-40 + ~4-5 h.
- Full 113 baseline pending DeepSeek credit top-up (~$11 remaining < ~$35 needed).
