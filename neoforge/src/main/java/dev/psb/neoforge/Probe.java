package dev.psb.neoforge;

import java.util.Arrays;

/**
 * Tick-time recorder. Measures from the first ServerTickEvent.Pre listener to the last
 * ServerTickEvent.Post listener, which brackets the whole level tick including block entities.
 * Output format is shared with the Pumpkin probe plugin so the harness parses both the same way.
 */
public final class Probe {
    private static final int CAPACITY = 1 << 20;
    private final long[] samples = new long[CAPACITY];
    private int count;
    private long tickStart;
    /** Ticks still to record after {@code /probe arm}; negative means unlimited. */
    private long remaining = -1;

    void begin() { tickStart = System.nanoTime(); }

    void end() {
        if (remaining == 0) return;
        if (remaining > 0) remaining--;
        if (count < CAPACITY) samples[count++] = System.nanoTime() - tickStart;
    }

    void reset() { count = 0; remaining = -1; }

    void arm(int ticks) { count = 0; remaining = ticks; }

    /** {@code probe ticks=<n> mean_us=.. p50_us=.. p95_us=.. p99_us=.. max_us=..} */
    String dump() {
        if (count == 0) return "probe ticks=0";
        long[] sorted = Arrays.copyOf(samples, count);
        Arrays.sort(sorted);
        long total = 0;
        for (long v : sorted) total += v;
        return String.format("probe ticks=%d mean_us=%.1f p50_us=%.1f p95_us=%.1f p99_us=%.1f max_us=%.1f",
                count, total / 1000.0 / count, pct(sorted, 50), pct(sorted, 95), pct(sorted, 99),
                sorted[count - 1] / 1000.0);
    }

    private static double pct(long[] sorted, int p) {
        int idx = (int) Math.ceil(p / 100.0 * sorted.length) - 1;
        return sorted[Math.max(0, Math.min(sorted.length - 1, idx))] / 1000.0;
    }
}
