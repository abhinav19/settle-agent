package com.settleagent.producer;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.List;
import org.junit.jupiter.api.Test;

class StreamRunnerTest {

  private static final List<SettlementFailureEvent> FIXTURES = TestSupport.fixtures();
  private static final Clock CLOCK =
      Clock.fixed(Instant.parse("2026-10-10T12:00:00Z"), ZoneOffset.UTC);

  private final RecordingPublisher publisher = new RecordingPublisher();

  private StreamRunner runner(double rate) {
    ProducerProperties props =
        new ProducerProperties(
            "stream",
            "t",
            "x",
            0,
            0,
            rate,
            3,
            false,
            3,
            Duration.ofMillis(10),
            Duration.ofSeconds(1),
            Duration.ofSeconds(1),
            3);
    return new StreamRunner(FIXTURES, new SettlementEventGenerator(3, CLOCK), publisher, props);
  }

  @Test
  void periodIsTheReciprocalOfTheRate() {
    assertThat(StreamRunner.periodNanos(1)).isEqualTo(1_000_000_000L);
    assertThat(StreamRunner.periodNanos(4)).isEqualTo(250_000_000L);
    assertThat(StreamRunner.periodNanos(1e12)).isEqualTo(1L); // never zero (would be rejected)
  }

  @Test
  void aNonPositiveRateIsRejectedAtConstruction() {
    assertThatThrownBy(() -> runner(0)).isInstanceOf(IllegalArgumentException.class);
    assertThatThrownBy(() -> runner(-3)).isInstanceOf(IllegalArgumentException.class);
  }

  @Test
  void tickDoesNothingUntilStarted() {
    assertThat(runner(10).tick()).isFalse();
    assertThat(publisher.published).isEmpty();
  }

  @Test
  void aTickPublishesAFreshEventForAKnownTrade() throws Exception {
    StreamRunner runner = runner(0.001); // effectively one manual tick
    runner.start();
    try {
      assertThat(runner.tick()).isTrue();
    } finally {
      runner.stop();
    }
    SettlementFailureEvent sent = publisher.published.get(publisher.published.size() - 1);
    assertThat(FIXTURES.stream().map(SettlementFailureEvent::tradeId)).contains(sent.tradeId());
    assertThat(FIXTURES.stream().map(SettlementFailureEvent::eventId))
        .doesNotContain(sent.eventId());
  }

  @Test
  void aFailedPublishNeverKillsTheStream() {
    StreamRunner runner = runner(0.001);
    runner.start();
    try {
      publisher.failWhen = e -> true;
      assertThat(runner.tick()).isFalse(); // swallowed and reported, not thrown
      publisher.failWhen = e -> false;
      assertThat(runner.tick()).isTrue(); // the next tick still works
    } finally {
      runner.stop();
    }
  }

  @Test
  void streamsAtRoughlyTheRequestedRateThenStopsCleanlyAndFlushes() throws Exception {
    StreamRunner runner = runner(500); // one event every 2 ms
    runner.start();
    assertThat(runner.isRunning()).isTrue();
    long deadline = System.nanoTime() + Duration.ofSeconds(5).toNanos();
    while (publisher.published.size() < 20 && System.nanoTime() < deadline) {
      Thread.sleep(5);
    }
    runner.stop();

    assertThat(publisher.published.size()).isGreaterThanOrEqualTo(20);
    assertThat(runner.isRunning()).isFalse();
    assertThat(publisher.flushes.get()).isGreaterThanOrEqualTo(1);
    int afterStop = publisher.published.size();
    Thread.sleep(100);
    assertThat(publisher.published).hasSize(afterStop); // nothing is emitted after stop
    assertThat(publisher.published.stream().map(SettlementFailureEvent::eventId).distinct())
        .hasSameSizeAs(publisher.published); // every streamed event is new
  }

  @Test
  void startAndStopAreIdempotent() {
    StreamRunner runner = runner(100);
    runner.stop(); // stopping a never-started runner is harmless
    runner.start();
    runner.start();
    runner.stop();
    runner.stop();
    assertThat(runner.isRunning()).isFalse();
  }
}
