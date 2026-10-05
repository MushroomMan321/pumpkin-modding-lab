package dev.psb.neoforge;

import com.mojang.brigadier.CommandDispatcher;
import com.mojang.brigadier.arguments.IntegerArgumentType;
import com.mojang.brigadier.context.CommandContext;
import com.mojang.logging.LogUtils;
import java.util.Locale;
import java.util.Set;
import net.minecraft.commands.CommandSourceStack;
import net.minecraft.commands.Commands;
import net.minecraft.core.registries.Registries;
import net.minecraft.network.chat.Component;
import net.minecraft.world.level.block.entity.BlockEntityType;
import net.neoforged.bus.api.EventPriority;
import net.neoforged.bus.api.IEventBus;
import net.neoforged.fml.common.Mod;
import net.neoforged.neoforge.common.NeoForge;
import net.neoforged.neoforge.event.RegisterCommandsEvent;
import net.neoforged.neoforge.event.server.ServerStoppingEvent;
import net.neoforged.neoforge.event.tick.ServerTickEvent;
import net.neoforged.neoforge.registries.DeferredBlock;
import net.neoforged.neoforge.registries.DeferredHolder;
import net.neoforged.neoforge.registries.DeferredRegister;
import org.slf4j.Logger;

/**
 * Pumpkin Server Bench, NeoForge side. Variant is chosen at launch with
 * {@code -Dpsb.variant=batched|block_entity} (default batched).
 */
@Mod(PsbMod.MODID)
public final class PsbMod {
    public static final String MODID = "psb";
    private static final Logger LOGGER = LogUtils.getLogger();

    static final DeferredRegister.Blocks BLOCKS = DeferredRegister.createBlocks(MODID);
    static final DeferredRegister<BlockEntityType<?>> BLOCK_ENTITIES =
            DeferredRegister.create(Registries.BLOCK_ENTITY_TYPE, MODID);

    static final DeferredBlock<MachineBlock> MACHINE =
            BLOCKS.registerBlock("machine", MachineBlock::new, p -> p.strength(1.0f));
    static final DeferredHolder<BlockEntityType<?>, BlockEntityType<MachineBlockEntity>> MACHINE_BE =
            BLOCK_ENTITIES.register("machine", () -> new BlockEntityType<>(MachineBlockEntity::new, Set.of(MACHINE.get())));

    private static final Bench.Variant VARIANT = Bench.Variant.valueOf(
            System.getProperty("psb.variant", "batched").toUpperCase(Locale.ROOT));
    private static final Probe PROBE = new Probe();
    private static Bench bench;

    public PsbMod(IEventBus modBus) {
        BLOCKS.register(modBus);
        BLOCK_ENTITIES.register(modBus);
        NeoForge.EVENT_BUS.addListener(EventPriority.HIGHEST, (ServerTickEvent.Pre e) -> {
            PROBE.begin();
            if (bench != null) bench.preTick();
        });
        NeoForge.EVENT_BUS.addListener(EventPriority.LOWEST, (ServerTickEvent.Post e) -> {
            if (bench != null) bench.postTick();
            PROBE.end();
        });
        NeoForge.EVENT_BUS.addListener((ServerStoppingEvent e) -> bench = null);
        NeoForge.EVENT_BUS.addListener((RegisterCommandsEvent e) -> registerCommands(e.getDispatcher()));
        LOGGER.info("psb loaded, variant={}", VARIANT);
    }

    static Bench bench() { return bench; }

    private static void registerCommands(CommandDispatcher<CommandSourceStack> d) {
        d.register(Commands.literal("psb")
                .requires(Commands.hasPermission(Commands.LEVEL_GAMEMASTERS))
                .then(Commands.literal("setup")
                        .then(Commands.argument("n", IntegerArgumentType.integer(1, 1_000_000))
                                .then(Commands.argument("rate_permille", IntegerArgumentType.integer(0, 1000))
                                        .executes(PsbMod::setup))))
                .then(Commands.literal("start").executes(c -> withBench(c, b -> { b.start(); return "psb started"; })))
                .then(Commands.literal("stop").executes(c -> withBench(c, b -> { b.stop(); return "psb stopped"; })))
                .then(Commands.literal("run")
                        .then(Commands.argument("ticks", IntegerArgumentType.integer(1))
                                .executes(c -> withBench(c, b -> {
                                    b.runFor(IntegerArgumentType.getInteger(c, "ticks"));
                                    return "psb running";
                                }))))
                .then(Commands.literal("status").executes(c -> withBench(c, Bench::status))));
        d.register(Commands.literal("probe")
                .requires(Commands.hasPermission(Commands.LEVEL_GAMEMASTERS))
                .then(Commands.literal("reset").executes(c -> reply(c, () -> { PROBE.reset(); return "probe reset"; })))
                .then(Commands.literal("arm")
                        .then(Commands.argument("ticks", IntegerArgumentType.integer(1))
                                .executes(c -> reply(c, () -> {
                                    int t = IntegerArgumentType.getInteger(c, "ticks");
                                    PROBE.arm(t);
                                    return "probe armed ticks=" + t;
                                }))))
                .then(Commands.literal("dump").executes(c -> reply(c, PROBE::dump))));
    }

    private static int setup(CommandContext<CommandSourceStack> c) {
        int n = IntegerArgumentType.getInteger(c, "n");
        int rate = IntegerArgumentType.getInteger(c, "rate_permille");
        return reply(c, () -> {
            Bench b = new Bench(c.getSource().getServer().overworld(), VARIANT, n, rate);
            bench = null;
            b.place();
            bench = b;
            return String.format("psb setup n=%d rate=%d side=%d variant=%s", n, rate, b.side(),
                    VARIANT.name().toLowerCase(Locale.ROOT));
        });
    }

    private static int withBench(CommandContext<CommandSourceStack> c, java.util.function.Function<Bench, String> f) {
        if (bench == null) {
            c.getSource().sendFailure(Component.literal("psb not set up"));
            return 0;
        }
        return reply(c, () -> f.apply(bench));
    }

    private static int reply(CommandContext<CommandSourceStack> c, java.util.function.Supplier<String> f) {
        String msg = f.get();
        c.getSource().sendSuccess(() -> Component.literal(msg), false);
        return 1;
    }
}
