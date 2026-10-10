package com.settleagent.producer;

import com.networknt.schema.InputFormat;
import com.networknt.schema.JsonSchema;
import com.networknt.schema.JsonSchemaFactory;
import com.networknt.schema.SchemaValidatorsConfig;
import com.networknt.schema.SpecVersion;
import com.networknt.schema.ValidationMessage;
import java.io.IOException;
import java.math.BigDecimal;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.time.Instant;
import java.time.LocalDate;
import java.util.List;
import java.util.Set;
import java.util.UUID;

/** Shared builders. Paths are relative to producer-service/ (Maven's working directory). */
final class TestSupport {

  static final Path FIXTURES = Path.of("../data/synthetic/dataset.json");
  static final Path SCHEMA = Path.of("../contracts/settlement_failure_event.schema.json");
  static final Path GOLDEN = Path.of("../contracts/java-events.golden.jsonl");
  static final String VALID_ISIN = "US0378331005";

  private static final JsonSchema CONTRACT = loadContract();

  private TestSupport() {}

  private static JsonSchema loadContract() {
    try {
      SchemaValidatorsConfig config =
          SchemaValidatorsConfig.builder().formatAssertionsEnabled(true).build();
      return JsonSchemaFactory.getInstance(SpecVersion.VersionFlag.V202012)
          .getSchema(Files.readString(SCHEMA), config);
    } catch (IOException e) {
      throw new IllegalStateException(e);
    }
  }

  /** Validation errors of a JSON message against the committed contract schema (empty = valid). */
  static Set<ValidationMessage> contractErrors(String json) {
    return CONTRACT.validate(json, InputFormat.JSON);
  }

  static ProducerProperties props(int maxAttempts, Duration sendTimeout) {
    return new ProducerProperties(
        "replay",
        "settlement.failures",
        FIXTURES.toString(),
        0,
        0,
        0,
        42,
        true,
        maxAttempts,
        Duration.ofMillis(200),
        Duration.ofSeconds(5),
        sendTimeout,
        3);
  }

  static SettlementFailureEvent event() {
    return event("T-0001", "SSIM");
  }

  static SettlementFailureEvent event(String tradeId, String reason) {
    SettlementFailureEvent shell =
        new SettlementFailureEvent(
            "1.0",
            UUID.fromString("11111111-2222-4333-8444-555555555555"),
            Instant.parse("2026-10-10T12:00:00Z"),
            tradeId,
            VALID_ISIN,
            LocalDate.of(2026, 10, 9),
            "DEUTDEFF",
            reason,
            new BigDecimal("1000"),
            new BigDecimal("185000.50"),
            "USD",
            "x");
    return withRaw(shell, SettlementEventGenerator.buildRawMessage(shell));
  }

  static SettlementFailureEvent withRaw(SettlementFailureEvent e, String raw) {
    return new SettlementFailureEvent(
        e.schemaVersion(),
        e.eventId(),
        e.emittedAt(),
        e.tradeId(),
        e.isin(),
        e.settlementDate(),
        e.counterpartyBic(),
        e.reasonCode(),
        e.quantity(),
        e.cashAmount(),
        e.currency(),
        raw);
  }

  static List<SettlementFailureEvent> fixtures() {
    return FixtureLoader.load(FIXTURES);
  }
}
