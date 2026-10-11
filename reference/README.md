# Reference scripts (read-only)

Original scripts the TRACE target ETL is ported from. They are kept unmodified for comparison and are **not** deployed
or imported by anything in this repo.

| File | Source | Commit | Original path |
|---|---|---|---|
| `aqi_hourly_colab.py` | https://github.com/vaibhavidhumal4/Air-Quality-ETL-Pipeline | `f844b295ce839fd5eeea0dc37d65b14e7dfa5e6d` (2026-10-10) | `Multi-Year Air Quality ETL Pipeline/data_pipeline.py` |

The script is a Google Colab notebook export: it uses `google.colab` auth and a top-level `await`, so it does not run
as a plain Python file. Data comes from the Open-Meteo Air Quality API (CAMS / Copernicus model data).
