package dev.psb.neoforge;

/**
 * Java port of {@code workload/src/lib.rs}. Must reproduce its GOLDENS exactly (see GridTest).
 * Arithmetic uses {@code long} with two's-complement wrap, which gives the same bits as Rust's
 * wrapping {@code u64}. Energy values never go negative, so {@code /} matches unsigned division.
 */
public final class Grid {
    public static final int X0 = 0;
    public static final int Z0 = 0;
    public static final int Y_MACHINE = 200;
    public static final int Y_DISPLAY = 201;

    /** Receives one world write: machine index and whether the display becomes lime. */
    @FunctionalInterface
    public interface WriteSink {
        void write(int machine, boolean lime);
    }

    private final int n;
    private final int s;
    private final int ratePermille;
    private long tick;
    private long[] energy;
    private long[] next;

    public Grid(int n, int ratePermille) {
        if (n <= 0) throw new IllegalArgumentException("n must be positive");
        if (ratePermille < 0 || ratePermille > 1000) throw new IllegalArgumentException("rate_permille must be 0..1000");
        this.n = n;
        this.s = side(n);
        this.ratePermille = ratePermille;
        this.energy = new long[n];
        this.next = new long[n];
        for (int i = 0; i < n; i++) energy[i] = i % 97;
    }

    public int n() { return n; }
    public int side() { return s; }
    public int ratePermille() { return ratePermille; }
    public long tick() { return tick; }

    public static int side(int n) {
        int s = 1;
        while ((long) s * s < n) s++;
        return s;
    }

    public static int writesPerTick(int n, int ratePermille) {
        return (int) ((long) n * ratePermille / 1000);
    }

    public int writesPerTick() { return writesPerTick(n, ratePermille); }

    public int x(int i) { return X0 + i % s; }
    public int z(int i) { return Z0 + i / s; }

    public void step(WriteSink sink) {
        java.util.Arrays.fill(next, 0L);
        for (int i = 0; i < n; i++) {
            long e = energy[i];
            long share = e / 8;
            long deg = 0;
            if (i >= s) { next[i - s] += share; deg++; }
            if (i + s < n) { next[i + s] += share; deg++; }
            if (i % s != 0) { next[i - 1] += share; deg++; }
            if ((i + 1) % s != 0 && i + 1 < n) { next[i + 1] += share; deg++; }
            next[i] += e - share * deg + 1;
        }
        long[] t = energy; energy = next; next = t;
        emitWrites(tick, n, writesPerTick(), sink);
        tick++;
    }

    /** The writes for tick {@code t}, in spec order. Shared with the block-entity variant. */
    public static void emitWrites(long t, int n, int w, WriteSink sink) {
        for (int k = 0; k < w; k++) {
            long g = t * w + k;
            sink.write((int) Long.remainderUnsigned(g, n), Long.divideUnsigned(g, n) % 2 == 0);
        }
    }

    public long energySum() {
        long sum = 0;
        for (int i = 0; i < n; i++) sum += energy[i] * (i + 1L);
        return sum;
    }

    public long writes() { return tick * writesPerTick(); }

    /** The same per-cell update on plain arrays, used by {@code GridTest} to check the block-entity double-buffer scheme. Keep in sync with MachineBlockEntity.step. */
    static void stepCell(int i, int n, int s, long[][] buf, int cur, int nxt) {
        long e = buf[i][cur];
        long share = e / 8;
        long deg = 0;
        if (i >= s) { buf[i - s][nxt] += share; deg++; }
        if (i + s < n) { buf[i + s][nxt] += share; deg++; }
        if (i % s != 0) { buf[i - 1][nxt] += share; deg++; }
        if ((i + 1) % s != 0 && i + 1 < n) { buf[i + 1][nxt] += share; deg++; }
        buf[i][nxt] += e - share * deg + 1;
        buf[i][cur] = 0;
    }
}
