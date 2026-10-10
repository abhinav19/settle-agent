package com.settleagent.producer;

import java.time.Clock;
import java.time.format.DateTimeFormatter;
import java.util.Random;
import java.util.UUID;

/**
 * Builds synthetic settlement-failure events.
 *
 * <ul>
 *   <li><b>replay</b>: fixtures are emitted exactly as stored (same event ids), so re-running a
 *       replay is an idempotent no-op for the consumer;
 *   <li><b>stream</b>: a fixture is re-issued as a NEW failure — fresh event id and timestamp,
 *       regenerated MT548-style message — keeping its trade/ISIN/BIC so references still resolve.
 * </ul>
 *
 * Deterministic for a given seed and clock.
 */
public final class SettlementEventGenerator {

  private static final DateTimeFormatter SWIFT_DATE = DateTimeFormatter.ofPattern("yyyyMMdd");

  private final Random rng;
  private final Clock clock;

  public SettlementEventGenerator(long seed, Clock clock) {
    this.rng = new Random(seed);
    this.clock = clock;
  }

  /** The fixture, unchanged. */
  public SettlementFailureEvent replay(SettlementFailureEvent fixture) {
    return fixture;
  }

  /** A new failure event derived from a fixture's trade. */
  public SettlementFailureEvent fresh(SettlementFailureEvent template) {
    SettlementFailureEvent shell =
        new SettlementFailureEvent(
            SettlementFailureEvent.SCHEMA_VERSION,
            nextEventId(),
            clock.instant(),
            template.tradeId(),
            template.isin(),
            template.settlementDate(),
            template.counterpartyBic(),
            template.reasonCode(),
            template.quantity(),
            template.cashAmount(),
            template.currency(),
            "pending");
    return new SettlementFailureEvent(
        shell.schemaVersion(),
        shell.eventId(),
        shell.emittedAt(),
        shell.tradeId(),
        shell.isin(),
        shell.settlementDate(),
        shell.counterpartyBic(),
        shell.reasonCode(),
        shell.quantity(),
        shell.cashAmount(),
        shell.currency(),
        buildRawMessage(shell));
  }

  /** A random fixture (seeded), for stream mode. */
  public SettlementFailureEvent pick(java.util.List<SettlementFailureEvent> fixtures) {
    return fixtures.get(rng.nextInt(fixtures.size()));
  }

  /** A random-but-reproducible version-4 UUID. */
  UUID nextEventId() {
    long msb = (rng.nextLong() & 0xFFFFFFFFFFFF0FFFL) | 0x0000000000004000L;
    long lsb = (rng.nextLong() & 0x3FFFFFFFFFFFFFFFL) | 0x8000000000000000L;
    return new UUID(msb, lsb);
  }

  /** Synthetic MT548-style status text. Matches the Python generator's format byte for byte. */
  public static String buildRawMessage(SettlementFailureEvent e) {
    return ":16R:GENL\n"
        + ":20C::SEME//"
        + e.tradeId()
        + "\n"
        + ":23G:INST\n"
        + ":16S:GENL\n"
        + ":16R:TRADDET\n"
        + ":98A::SETT//"
        + e.settlementDate().format(SWIFT_DATE)
        + "\n"
        + ":35B:ISIN "
        + e.isin()
        + "\n"
        + ":16S:TRADDET\n"
        + ":16R:SETTRAN\n"
        + ":36B::SETT//UNIT/"
        + e.quantity().toPlainString()
        + "\n"
        + ":19A::SETT//"
        + e.currency()
        + e.cashAmount().toPlainString()
        + "\n"
        + ":95P::REAG//"
        + e.counterpartyBic()
        + "\n"
        + ":16S:SETTRAN\n"
        + ":16R:STAT\n"
        + ":25D::IPRC//PENF\n"
        + ":24B::PENF//"
        + e.reasonCode()
        + "\n"
        + ":16S:STAT\n"
        + "-- SYNTHETIC MESSAGE (not a real MT548) --";
  }
}
