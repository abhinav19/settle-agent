package com.settleagent.producer;

import static org.assertj.core.api.Assertions.assertThat;

import com.networknt.schema.ValidationMessage;
import java.io.IOException;
import java.nio.file.Files;
import java.time.Clock;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.List;
import java.util.Set;
import org.junit.jupiter.api.Test;

/**
 * The cross-language contract. The Python Pydantic model is the source of truth; its generated JSON
 * Schema ({@code contracts/settlement_failure_event.schema.json}) is validated against here, and a
 * golden file of Java output is committed for the Python tests to parse with the real strict model
 * (which also checks what a schema cannot, like ISIN check digits).
 */
class EventContractTest {

  private static final Clock CLOCK =
      Clock.fixed(Instant.parse("2026-10-10T12:00:00Z"), ZoneOffset.UTC);

  private static Set<ValidationMessage> validate(String json) {
    return TestSupport.contractErrors(json);
  }

  @Test
  void everyReplayedFixtureConformsToTheSchema() {
    for (SettlementFailureEvent fixture : TestSupport.fixtures()) {
      assertThat(validate(EventJson.toJson(fixture))).as(fixture.tradeId()).isEmpty();
    }
  }

  @Test
  void every_streamed_event_conforms_for_many_seeds() {
    List<SettlementFailureEvent> fixtures = TestSupport.fixtures();
    for (long seed = 0; seed < 5; seed++) {
      SettlementEventGenerator generator = new SettlementEventGenerator(seed, CLOCK);
      for (int i = 0; i < 300; i++) {
        String json = EventJson.toJson(generator.fresh(generator.pick(fixtures)));
        assertThat(validate(json)).as("seed %d #%d", seed, i).isEmpty();
      }
    }
  }

  /** A validator that accepts everything would make the tests above meaningless: prove it bites. */
  @Test
  void theSchemaActuallyRejectsBrokenMessages() {
    String good = EventJson.toJson(TestSupport.event());
    assertThat(validate(good)).isEmpty();
    assertThat(validate(good.replace("}", ",\"surprise\":1}"))).as("extra field").isNotEmpty();
    assertThat(validate(good.replace("\"trade_id\":\"T-0001\",", "")))
        .as("missing field")
        .isNotEmpty();
    assertThat(validate(good.replace("11111111-2222-4333-8444-555555555555", "not-a-uuid")))
        .as("bad uuid")
        .isNotEmpty();
    assertThat(validate(good.replace("2026-10-10T12:00:00Z", "yesterday")))
        .as("bad timestamp")
        .isNotEmpty();
    assertThat(validate(good.replace("\"1.0\"", "\"2.0\""))).as("wrong version").isNotEmpty();
    assertThat(validate(good.replace("\"SSIM\"", "\"NOPE\""))).as("unknown reason").isNotEmpty();
    assertThat(validate(good.replace("US0378331005", "us0378331005")))
        .as("isin pattern")
        .isNotEmpty();
  }

  /** What the producer emits must be exactly what the committed golden file records. */
  @Test
  void outputMatchesTheCommittedGoldenFile() throws IOException {
    String actual = goldenSample();
    if (Boolean.getBoolean("update.golden") || !Files.exists(TestSupport.GOLDEN)) {
      Files.writeString(TestSupport.GOLDEN, actual);
    }
    assertThat(actual)
        .as(
            "Java wire output changed: review the diff, run with -Dupdate.golden=true, and make sure"
                + " the Python consumer tests still pass")
        .isEqualTo(Files.readString(TestSupport.GOLDEN));
  }

  /** 25 replayed fixtures + 25 freshly streamed events, fully deterministic. */
  static String goldenSample() {
    List<SettlementFailureEvent> fixtures = TestSupport.fixtures();
    SettlementEventGenerator generator = new SettlementEventGenerator(7, CLOCK);
    List<String> lines = new ArrayList<>();
    for (int i = 0; i < 25; i++) {
      lines.add(EventJson.toJson(generator.replay(fixtures.get(i * 11))));
    }
    for (int i = 0; i < 25; i++) {
      lines.add(EventJson.toJson(generator.fresh(generator.pick(fixtures))));
    }
    return String.join("\n", lines) + "\n";
  }
}
