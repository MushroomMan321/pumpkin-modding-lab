package dev.psb.neoforge;

import net.minecraft.core.BlockPos;
import net.minecraft.server.level.ServerLevel;
import net.minecraft.world.level.block.Block;
import net.minecraft.world.level.block.Blocks;
import net.minecraft.world.level.block.state.BlockState;

/** One configured benchmark run in one level. */
public final class Bench {
    public enum Variant { BATCHED, BLOCK_ENTITY }

    private final ServerLevel level;
    private final Variant variant;
    private final int n;
    private final int s;
    private final int ratePermille;
    private final int writesPerTick;
    private final Grid grid; // BATCHED only

    private boolean running;
    private long stopAt = -1;
    private long completed;
    private boolean inTick;

    private final BlockState lime = Blocks.WOOL.lime().defaultBlockState();
    private final BlockState white = Blocks.WOOL.white().defaultBlockState();

    Bench(ServerLevel level, Variant variant, int n, int ratePermille) {
        this.level = level;
        this.variant = variant;
        this.n = n;
        this.s = Grid.side(n);
        this.ratePermille = ratePermille;
        this.writesPerTick = Grid.writesPerTick(n, ratePermille);
        this.grid = variant == Variant.BATCHED ? new Grid(n, ratePermille) : null;
    }

    public int n() { return n; }
    public int side() { return s; }
    public int writesPerTick() { return writesPerTick; }
    public long currentTick() { return completed; }
    public Variant variant() { return variant; }

    boolean blockEntityTickActive() { return inTick && variant == Variant.BLOCK_ENTITY; }

    /** Places the grid. Uses UPDATE_CLIENTS only, so setup does not cascade neighbour updates. */
    void place() {
        BlockState machine = variant == Variant.BLOCK_ENTITY
                ? PsbMod.MACHINE.get().defaultBlockState()
                : Blocks.STONE.defaultBlockState();
        for (int i = 0; i < n; i++) {
            int x = Grid.X0 + i % s, z = Grid.Z0 + i / s;
            BlockPos mp = new BlockPos(x, Grid.Y_MACHINE, z);
            level.setBlock(mp, machine, Block.UPDATE_CLIENTS);
            level.setBlock(new BlockPos(x, Grid.Y_DISPLAY, z), white, Block.UPDATE_CLIENTS);
            if (variant == Variant.BLOCK_ENTITY) {
                if (!(level.getBlockEntity(mp) instanceof MachineBlockEntity be)) {
                    throw new IllegalStateException("machine block entity missing at " + mp + " (is the chunk loaded?)");
                }
                be.reset(i);
            }
        }
    }

    void start() { running = true; stopAt = -1; }
    void stop() { running = false; stopAt = -1; }
    void runFor(long ticks) { running = true; stopAt = completed + ticks; }
    boolean running() { return running; }

    /** ServerTickEvent.Pre. */
    void preTick() {
        if (!running) return;
        inTick = true;
        if (variant == Variant.BATCHED) {
            grid.step(this::setDisplay);
        }
    }

    /** ServerTickEvent.Post. Block entities ticked in between, inside the level tick. */
    void postTick() {
        if (!inTick) return;
        inTick = false;
        completed++;
        if (stopAt >= 0 && completed >= stopAt) stop();
    }

    void setDisplay(int machine, boolean isLime) {
        BlockPos p = new BlockPos(Grid.X0 + machine % s, Grid.Y_DISPLAY, Grid.Z0 + machine / s);
        level.setBlock(p, isLime ? lime : white, Block.UPDATE_ALL);
    }

    long energySum() {
        if (variant == Variant.BATCHED) return grid.energySum();
        long sum = 0;
        for (int i = 0; i < n; i++) {
            BlockPos p = new BlockPos(Grid.X0 + i % s, Grid.Y_MACHINE, Grid.Z0 + i / s);
            if (level.getBlockEntity(p) instanceof MachineBlockEntity be) {
                sum += be.energyAfter(completed) * (i + 1L);
            }
        }
        return sum;
    }

    String status() {
        return String.format("psb status tick=%d n=%d rate=%d energy_sum=%016x writes=%d variant=%s running=%s",
                completed, n, ratePermille, energySum(), completed * writesPerTick,
                variant.name().toLowerCase(), running);
    }
}
