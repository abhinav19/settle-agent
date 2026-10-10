package com.settleagent.producer;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import java.math.BigDecimal;
import java.time.Instant;
import java.time.LocalDate;
import java.util.ArrayList;
import java.util.List;
import java.util.UUID;
import java.util.function.UnaryOperator;
import java.util.stream.Stream;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.Arguments;
import org.junit.jupiter.params.provider.MethodSource;
import tools.jackson.databind.JsonNode;

class SettlementFailureEventTest {

  private static SettlementFailureEvent with(UnaryOperator<Builder> change) {
    return change.apply(new Builder()).build();
  }

  /** Mutable builder so each invalid case changes exactly one field. */
  static final class Builder {
    String schemaVersion = "1.0";
    UUID eventId = UUID.randomUUID();
    Instant emittedAt = Instant.parse("2026-10-10T12:00:00Z");
    String tradeId = "T-1";
    String isin = TestSupport.VALID_ISIN;
    LocalDate settlementDate = LocalDate.of(2026, 10, 9);
    String bic = "DEUTDEFF";
    String reason = "SSIM";
    BigDecimal quantity = new BigDecimal("1000");
    BigDecimal cash = new BigDecimal("1.50");
    String currency = "USD";
    String raw = "msg";

    SettlementFailureEvent build() {
      return new SettlementFailureEvent(
          schemaVersion,
          eventId,
          emittedAt,
          tradeId,
          isin,
          settlementDate,
          bic,
          reason,
          quantity,
          cash,
          currency,
          raw);
    }
  }

  static Stream<Arguments> invalid() {
    return Stream.of(
        Arguments.of(
            "schema version",
            (UnaryOperator<Builder>)
                b -> {
                  b.schemaVersion = "2.0";
                  return b;
                }),
        Arguments.of(
            "bad ISIN check digit",
            (UnaryOperator<Builder>)
                b -> {
                  b.isin = "US0378331006";
                  return b;
                }),
        Arguments.of(
            "lowercase ISIN",
            (UnaryOperator<Builder>)
                b -> {
                  b.isin = "us0378331005";
                  return b;
                }),
        Arguments.of(
            "short ISIN",
            (UnaryOperator<Builder>)
                b -> {
                  b.isin = "US037833";
                  return b;
                }),
        Arguments.of(
            "bad BIC",
            (UnaryOperator<Builder>)
                b -> {
                  b.bic = "BAD";
                  return b;
                }),
        Arguments.of(
            "unknown reason",
            (UnaryOperator<Builder>)
                b -> {
                  b.reason = "XXXX";
                  return b;
                }),
        Arguments.of(
            "zero quantity",
            (UnaryOperator<Builder>)
                b -> {
                  b.quantity = BigDecimal.ZERO;
                  return b;
                }),
        Arguments.of(
            "negative quantity",
            (UnaryOperator<Builder>)
                b -> {
                  b.quantity = new BigDecimal("-5");
                  return b;
                }),
        Arguments.of(
            "negative cash",
            (UnaryOperator<Builder>)
                b -> {
                  b.cash = new BigDecimal("-0.01");
                  return b;
                }),
        Arguments.of(
            "lowercase currency",
            (UnaryOperator<Builder>)
                b -> {
                  b.currency = "usd";
                  return b;
                }),
        Arguments.of(
            "blank trade id",
            (UnaryOperator<Builder>)
                b -> {
                  b.tradeId = " ";
                  return b;
                }),
        Arguments.of(
            "long trade id",
            (UnaryOperator<Builder>)
                b -> {
                  b.tradeId = "T".repeat(65);
                  return b;
                }),
        Arguments.of(
            "empty raw message",
            (UnaryOperator<Builder>)
                b -> {
                  b.raw = "";
                  return b;
                }));
  }

  @ParameterizedTest(name = "rejects {0}")
  @MethodSource("invalid")
  void refusesToBeConstructedInvalid(String why, UnaryOperator<Builder> change) {
    assertThatThrownBy(() -> with(change)).isInstanceOf(IllegalArgumentException.class);
  }

  @Test
  void nullsAreRejected() {
    assertThatThrownBy(
            () ->
                with(
                    b -> {
                      b.eventId = null;
                      return b;
                    }))
        .isInstanceOf(NullPointerException.class);
    assertThatThrownBy(
            () ->
                with(
                    b -> {
                      b.emittedAt = null;
                      return b;
                    }))
        .isInstanceOf(NullPointerException.class);
  }

  @Test
  void isinChecksumAcceptsRealIsinsAndRejectsTypos() {
    assertThat(SettlementFailureEvent.isinChecksumOk("US0378331005")).isTrue();
    assertThat(SettlementFailureEvent.isinChecksumOk("GB0002634946")).isTrue();
    assertThat(SettlementFailureEvent.isinChecksumOk("US0378331004")).isFalse();
  }

  @Test
  void serialisesToExactlyTheContractFieldsInOrder() throws Exception {
    JsonNode json = EventJson.mapper().readTree(EventJson.toJson(TestSupport.event()));
    List<String> keys = new ArrayList<>(json.propertyNames());
    assertThat(keys)
        .containsExactly(
            "schema_version",
            "event_id",
            "emitted_at",
            "trade_id",
            "isin",
            "settlement_date",
            "counterparty_bic",
            "reason_code",
            "quantity",
            "cash_amount",
            "currency",
            "raw_message");
  }

  @Test
  void decimalsAreJsonStringsAndNeverScientific() throws Exception {
    JsonNode json = EventJson.mapper().readTree(EventJson.toJson(TestSupport.event()));
    assertThat(json.get("quantity").isString()).isTrue();
    assertThat(json.get("quantity").asString()).isEqualTo("1000");
    assertThat(json.get("cash_amount").asString()).isEqualTo("185000.50"); // scale preserved

    SettlementFailureEvent scientific =
        with(
            b -> {
              b.quantity = new BigDecimal("1E+3");
              return b;
            });
    assertThat(EventJson.toJson(scientific)).contains("\"quantity\":\"1000\"").doesNotContain("E+");
  }

  @Test
  void timestampsAndDatesAreIso8601() throws Exception {
    JsonNode json = EventJson.mapper().readTree(EventJson.toJson(TestSupport.event()));
    assertThat(json.get("emitted_at").asString()).isEqualTo("2026-10-10T12:00:00Z");
    assertThat(json.get("settlement_date").asString()).isEqualTo("2026-10-09");
    assertThat(json.get("event_id").asString()).hasSize(36);
  }

  @Test
  void roundTripsThroughJson() {
    SettlementFailureEvent e = TestSupport.event();
    assertThat(EventJson.fromJson(EventJson.toJson(e))).isEqualTo(e);
  }

  @Test
  void readsTheFixtureTimestampFormat() {
    // fixtures were written by Python with an explicit +00:00 offset rather than 'Z'
    String json =
        EventJson.toJson(TestSupport.event())
            .replace("2026-10-10T12:00:00Z", "2026-10-10T12:00:00+00:00");
    assertThat(EventJson.fromJson(json).emittedAt())
        .isEqualTo(Instant.parse("2026-10-10T12:00:00Z"));
  }

  @Test
  void deserialisingAnInvalidMessageFails() {
    String bad =
        EventJson.toJson(TestSupport.event()).replace(TestSupport.VALID_ISIN, "US0378331006");
    assertThatThrownBy(() -> EventJson.fromJson(bad))
        .hasRootCauseInstanceOf(IllegalArgumentException.class);
  }
}
