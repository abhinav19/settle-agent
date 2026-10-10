package com.settleagent.producer;

import static org.assertj.core.api.Assertions.assertThat;

import java.time.Clock;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Set;
import java.util.UUID;
import org.junit.jupiter.api.Test;

class SettlementEventGeneratorTest {

  private static final Clock NOW =
      Clock.fixed(Instant.parse("2026-10-10T12:00:00Z"), ZoneOffset.UTC);
  private static final List<SettlementFailureEvent> FIXTURES = TestSupport.fixtures();

  /**
   * Cross-language parity: the Java generator and the Python generator must render the SAME
   * MT548-style text for the same trade. Any drift in either implementation fails here.
   */
  @Test
  void rawMessageFormatMatchesThePythonGeneratorForAll300Fixtures() {
    for (SettlementFailureEvent fixture : FIXTURES) {
      assertThat(SettlementEventGenerator.buildRawMessage(fixture))
          .as("raw_message for %s", fixture.tradeId())
          .isEqualTo(fixture.rawMessage());
    }
  }

  @Test
  void replayEmitsTheFixtureUnchanged() {
    SettlementEventGenerator generator = new SettlementEventGenerator(1, NOW);
    assertThat(generator.replay(FIXTURES.get(0))).isSameAs(FIXTURES.get(0));
  }

  @Test
  void freshEventsGetNewIdsAndTimestampsButKeepTheTradeReferences() {
    SettlementEventGenerator generator = new SettlementEventGenerator(1, NOW);
    SettlementFailureEvent template = FIXTURES.get(7);
    SettlementFailureEvent fresh = generator.fresh(template);
    assertThat(fresh.eventId()).isNotEqualTo(template.eventId());
    assertThat(fresh.emittedAt()).isEqualTo(NOW.instant());
    // references must still resolve in the MCP servers' reference data
    assertThat(fresh.tradeId()).isEqualTo(template.tradeId());
    assertThat(fresh.isin()).isEqualTo(template.isin());
    assertThat(fresh.counterpartyBic()).isEqualTo(template.counterpartyBic());
    assertThat(fresh.reasonCode()).isEqualTo(template.reasonCode());
    assertThat(fresh.quantity()).isEqualByComparingTo(template.quantity());
    assertThat(fresh.settlementDate()).isEqualTo(template.settlementDate());
    assertThat(fresh.rawMessage()).isEqualTo(SettlementEventGenerator.buildRawMessage(fresh));
  }

  @Test
  void isDeterministicForASeed() {
    List<String> a = generate(99, 50);
    assertThat(generate(99, 50)).isEqualTo(a);
    assertThat(generate(100, 50)).isNotEqualTo(a);
  }

  @Test
  void eventIdsAreUniqueVersion4UuidsEvenWhenDeterministic() {
    SettlementEventGenerator generator = new SettlementEventGenerator(5, NOW);
    Set<UUID> ids = new HashSet<>();
    for (int i = 0; i < 5000; i++) {
      UUID id = generator.nextEventId();
      assertThat(id.version()).isEqualTo(4);
      assertThat(id.variant()).isEqualTo(2);
      assertThat(ids.add(id)).isTrue();
    }
  }

  @Test
  void pickingCoversTheWholeFixtureSet() {
    SettlementEventGenerator generator = new SettlementEventGenerator(3, NOW);
    Set<String> trades = new HashSet<>();
    for (int i = 0; i < 6000; i++) {
      trades.add(generator.pick(FIXTURES).tradeId());
    }
    assertThat(trades.size()).isGreaterThan(280);
  }

  private static List<String> generate(long seed, int n) {
    SettlementEventGenerator generator = new SettlementEventGenerator(seed, NOW);
    List<String> out = new ArrayList<>();
    for (int i = 0; i < n; i++) {
      out.add(EventJson.toJson(generator.fresh(generator.pick(FIXTURES))));
    }
    return out;
  }
}
