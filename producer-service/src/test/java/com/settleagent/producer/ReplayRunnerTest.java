package com.settleagent.producer;

import static org.assertj.core.api.Assertions.assertThat;

import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import org.junit.jupiter.api.Test;

class ReplayRunnerTest {

  private static final List<SettlementFailureEvent> FIXTURES = TestSupport.fixtures();

  private final RecordingPublisher publisher = new RecordingPublisher();
  private final List<Duration> sleeps = new ArrayList<>();
  private final List<Integer> exits = new ArrayList<>();

  private ReplayRunner runner(int offset, int limit, double rate, boolean exit) {
    ProducerProperties props =
        new ProducerProperties(
            "replay",
            "t",
            "x",
            offset,
            limit,
            rate,
            1,
            exit,
            3,
            Duration.ofMillis(10),
            Duration.ofSeconds(1),
            Duration.ofSeconds(1),
            3);
    return new ReplayRunner(FIXTURES, publisher, props, sleeps::add, exits::add);
  }

  @Test
  void replaysEveryFixtureUnchangedAndInOrder() {
    assertThat(runner(0, 0, 0, false).replay()).isZero();
    assertThat(publisher.published).containsExactlyElementsOf(FIXTURES);
    assertThat(publisher.flushes).hasValue(1);
  }

  @Test
  void honoursOffsetAndLimit() {
    runner(10, 5, 0, false).replay();
    assertThat(publisher.published).containsExactlyElementsOf(FIXTURES.subList(10, 15));
  }

  @Test
  void clampsAnOutOfRangeWindowInsteadOfFailing() {
    runner(290, 100, 0, false).replay();
    assertThat(publisher.published).hasSize(10);
    publisher.published.clear();
    runner(1_000, 5, 0, false).replay();
    assertThat(publisher.published).isEmpty();
    runner(-5, 2, 0, false).replay();
    assertThat(publisher.published).hasSize(2);
  }

  @Test
  void throttlesToTheConfiguredRate() {
    runner(0, 4, 50, false).replay();
    assertThat(sleeps).hasSize(4).containsOnly(Duration.ofMillis(20)); // 50/s
  }

  @Test
  void countsFailuresButKeepsGoing() {
    String poison = FIXTURES.get(2).tradeId();
    publisher.failWhen = e -> e.tradeId().equals(poison);
    int failures = runner(0, 6, 0, false).replay();
    assertThat(failures)
        .isEqualTo(
            (int) FIXTURES.subList(0, 6).stream().filter(e -> e.tradeId().equals(poison)).count());
    assertThat(publisher.published).hasSize(6 - failures);
  }

  @Test
  void exitsZeroOnSuccessAndNonZeroOnAnyFailure() throws Exception {
    runner(0, 3, 0, true).run(null);
    assertThat(exits).containsExactly(0);

    exits.clear();
    publisher.failWhen = e -> true;
    runner(0, 3, 0, true).run(null);
    assertThat(exits).containsExactly(1);
  }

  @Test
  void staysAliveWhenToldNotToExit() throws Exception {
    runner(0, 3, 0, false).run(null);
    assertThat(exits).isEmpty();
  }

  @Test
  void replayingTwiceReusesTheSameEventIdsSoTheConsumerCanDeduplicate() {
    runner(0, 5, 0, false).replay();
    runner(0, 5, 0, false).replay();
    assertThat(publisher.published.stream().map(SettlementFailureEvent::eventId).distinct())
        .hasSize(5);
    assertThat(publisher.published).hasSize(10);
  }
}
