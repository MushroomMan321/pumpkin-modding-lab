package dev.psb.neoforge;

import net.minecraft.core.BlockPos;
import net.minecraft.world.level.Level;
import net.minecraft.world.level.block.entity.BlockEntity;
import net.minecraft.world.level.block.state.BlockState;

/**
 * The "how real mods do it" variant: one ticking block entity per machine, pushing energy into
 * its neighbours' block entities. Double-buffered by tick parity so the result does not depend
 * on the order the level ticks block entities in, and so it matches {@link Grid} bit for bit.
 */
public final class MachineBlockEntity extends BlockEntity {
    private final long[] buf = new long[2];
    private final MachineBlockEntity[] neighbours = new MachineBlockEntity[4];
    private int neighbourCount = -1;
    private int index = -1;
    private long lastTicked = -1;

    public MachineBlockEntity(BlockPos pos, BlockState state) {
        super(PsbMod.MACHINE_BE.get(), pos, state);
    }

    /** Called by setup after the block is placed. */
    void reset(int index) {
        this.index = index;
        this.buf[0] = index % 97;
        this.buf[1] = 0;
        this.neighbourCount = -1;
        this.lastTicked = -1;
    }

    long energyAfter(long ticks) {
        return buf[(int) (ticks & 1)];
    }

    public static void serverTick(Level level, BlockPos pos, BlockState state, MachineBlockEntity be) {
        Bench bench = PsbMod.bench();
        if (bench == null || !bench.blockEntityTickActive() || be.index < 0) return;
        long t = bench.currentTick();
        if (be.lastTicked == t) return;
        be.lastTicked = t;
        be.step(level, bench, t);
    }

    private void step(Level level, Bench bench, long t) {
        if (neighbourCount < 0) resolveNeighbours(level, bench);
        int cur = (int) (t & 1), nxt = cur ^ 1;
        long e = buf[cur];
        long share = e / 8;
        for (int k = 0; k < neighbourCount; k++) neighbours[k].buf[nxt] += share;
        buf[nxt] += e - share * neighbourCount + 1;
        buf[cur] = 0;

        int w = bench.writesPerTick();
        if (w > 0) {
            int n = bench.n();
            long start = Long.remainderUnsigned(t * w, n);
            long k = Math.floorMod(index - start, (long) n);
            if (k < w) {
                long g = t * w + k;
                bench.setDisplay(index, Long.divideUnsigned(g, n) % 2 == 0);
            }
        }
    }

    /** Caches neighbour block entities once, like energy mods that cache their adjacent handlers. */
    private void resolveNeighbours(Level level, Bench bench) {
        int n = bench.n(), s = bench.side(), i = index;
        int count = 0;
        int[] candidates = new int[4];
        if (i >= s) candidates[count++] = i - s;
        if (i + s < n) candidates[count++] = i + s;
        if (i % s != 0) candidates[count++] = i - 1;
        if ((i + 1) % s != 0 && i + 1 < n) candidates[count++] = i + 1;
        for (int k = 0; k < count; k++) {
            int j = candidates[k];
            BlockPos p = new BlockPos(Grid.X0 + j % s, Grid.Y_MACHINE, Grid.Z0 + j / s);
            if (!(level.getBlockEntity(p) instanceof MachineBlockEntity nb)) {
                throw new IllegalStateException("missing machine block entity at " + p);
            }
            neighbours[k] = nb;
        }
        neighbourCount = count;
    }
}
