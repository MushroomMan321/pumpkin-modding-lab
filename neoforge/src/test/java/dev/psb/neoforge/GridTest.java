package dev.psb.neoforge;

import static org.junit.jupiter.api.Assertions.assertEquals;

import org.junit.jupiter.api.Test;

class GridTest {
    // Copied from workload/src/lib.rs GOLDENS: n, rate_permille, ticks, energy_sum, writes.
    private static final long[][] GOLDENS = {
            {1, 0, 10, 0x000000000000000aL, 0},
            {37, 300, 200, 0x00000000000256beL, 2200},
            {1000, 10, 1000, 0x000000001f3d063eL, 10000},
            {10000, 10, 1000, 0x0000000c33c1d925L, 100000},
            {10000, 100, 1000, 0x0000000c33c1d925L, 1000000},
            {50000, 10, 200, 0x000000482ca137d8L, 100000},
    };

    @Test
    void goldensMatch() {
        for (long[] g : GOLDENS) {
            Grid grid = new Grid((int) g[0], (int) g[1]);
            for (long t = 0; t < g[2]; t++) grid.step((m, lime) -> {});
            assertEquals(g[3], grid.energySum(), "energy_sum n=" + g[0] + " rate=" + g[1]);
            assertEquals(g[4], grid.writes(), "writes n=" + g[0] + " rate=" + g[1]);
        }
    }

    @Test
    void blockEntityModelMatchesBatched() {
        // Same double-buffer scheme as MachineBlockEntity, run in a deliberately scrambled order.
        int n = 1000, rate = 10, ticks = 300;
        int s = Grid.side(n);
        long[][] buf = new long[n][2];
        for (int i = 0; i < n; i++) buf[i][0] = i % 97;
        int[] order = new int[n];
        for (int i = 0; i < n; i++) order[i] = (int) ((i * 7919L) % n);
        for (int t = 0; t < ticks; t++) {
            int cur = t & 1, nxt = cur ^ 1;
            for (int i : order) Grid.stepCell(i, n, s, buf, cur, nxt);
        }
        long sum = 0;
        for (int i = 0; i < n; i++) sum += buf[i][ticks & 1] * (i + 1L);

        Grid grid = new Grid(n, rate);
        for (int t = 0; t < ticks; t++) grid.step((m, lime) -> {});
        assertEquals(grid.energySum(), sum);
    }
}
