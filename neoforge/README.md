# NeoForge side

The `psb` mod: both NeoForge variants of the `energy-grid` workload (`batched` and `block_entity`,
chosen with `-Dpsb.variant=...`) and the tick probe. Built from the NeoForge 26.3 ModDevGradle MDK
(see `TEMPLATE_LICENSE.txt`).

```bash
./gradlew build      # also runs GridTest, which checks the Java port against the Rust goldens
```

The jar lands in `build/libs/psb-1.0.0.jar`; the harness copies it into a fresh server for each run.
