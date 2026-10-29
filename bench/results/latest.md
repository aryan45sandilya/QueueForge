# Benchmark 20261025T224436Z (synthetic)

| config | slots | API submit jobs/s | drain completed jobs/s | drain s | backlog wait p50 / p95 / p99 ms | exec p50 / p95 ms | retry rate | failure rate |
|---|---|---|---|---|---|---|---|---|
| 1x4 | 4 | 50.37 | 58.2 | 34.366 | 35826.0 / 39321.0 / 39395.29 | 32.0 / 53.0 | 6.0% | 0.0% |
| 1x16 | 16 | 43.21 | 59.9 | 33.388 | 38528.5 / 45469.0 / 46324.2 | 33.0 / 49.05 | 6.0% | 0.0% |
| 2x16 | 32 | 42.74 | 70.9 | 28.21 | 37247.0 / 45929.3 / 46314.67 | 41.0 / 85.0 | 6.0% | 0.0% |

Environment: {"platform": "Windows-11-10.0.26200-SP0", "processor": "Intel64 Family 6 Model 140 Stepping 2, GenuineIntel", "logical_cpus": 4, "physical_cpus": 2, "memory_gb": 7.7, "python": "3.13.1", "postgres": "16.8", "redis": "5.0.14.1", "note": "API, scheduler, workers, PostgreSQL and Redis all ran on this single machine."}
