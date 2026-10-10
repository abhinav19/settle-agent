package com.settleagent.producer;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.times;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CompletableFuture;
import org.apache.kafka.common.KafkaException;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.kafka.core.KafkaTemplate;
import org.springframework.kafka.support.SendResult;

class KafkaEventPublisherTest {

  private static final Clock CLOCK =
      Clock.fixed(Instant.parse("2026-10-10T12:00:00Z"), ZoneOffset.UTC);

  private KafkaTemplate<String, String> template;
  private PublishStats stats;
  private List<Duration> sleeps;
  private KafkaEventPublisher publisher;

  @BeforeEach
  @SuppressWarnings("unchecked")
  void setUp() {
    template = mock(KafkaTemplate.class);
    stats = new PublishStats();
    sleeps = new ArrayList<>();
    publisher = publisher(5, Duration.ofSeconds(2));
  }

  private KafkaEventPublisher publisher(int attempts, Duration timeout) {
    return new KafkaEventPublisher(
        template, TestSupport.props(attempts, timeout), stats, sleeps::add, CLOCK);
  }

  private static CompletableFuture<SendResult<String, String>> ok() {
    return CompletableFuture.completedFuture(null);
  }

  private static CompletableFuture<SendResult<String, String>> failure() {
    return CompletableFuture.failedFuture(new KafkaException("broker unavailable"));
  }

  @Test
  void sendsTheJsonKeyedByTradeIdToTheConfiguredTopic() {
    when(template.send(anyString(), anyString(), anyString())).thenReturn(ok());
    SettlementFailureEvent event = TestSupport.event("T-77", "LATE");

    publisher.publish(event);

    verify(template).send("settlement.failures", "T-77", EventJson.toJson(event));
    assertThat(stats.sent()).isEqualTo(1);
    assertThat(stats.failed()).isZero();
    assertThat(stats.lastSuccess()).isEqualTo(CLOCK.instant());
    assertThat(sleeps).isEmpty();
  }

  @Test
  void retriesTransientFailuresWithExponentialBackoffThenSucceeds() {
    when(template.send(anyString(), anyString(), anyString()))
        .thenReturn(failure(), failure(), ok());

    publisher.publish(TestSupport.event());

    verify(template, times(3)).send(anyString(), anyString(), anyString());
    assertThat(sleeps).containsExactly(Duration.ofMillis(200), Duration.ofMillis(400));
    assertThat(stats.retries()).isEqualTo(2);
    assertThat(stats.sent()).isEqualTo(1);
    assertThat(stats.consecutiveFailures()).isZero(); // success resets the streak
  }

  @Test
  void givesUpAfterTheConfiguredAttemptsAndSaysSo() {
    when(template.send(anyString(), anyString(), anyString())).thenReturn(failure());

    assertThatThrownBy(() -> publisher(3, Duration.ofSeconds(1)).publish(TestSupport.event()))
        .isInstanceOf(PublishFailedException.class)
        .hasMessageContaining("after 3 attempts")
        .hasRootCauseInstanceOf(KafkaException.class);

    verify(template, times(3)).send(eq("settlement.failures"), anyString(), anyString());
    assertThat(stats.failed()).isEqualTo(1);
    assertThat(stats.sent()).isZero();
    assertThat(stats.consecutiveFailures()).isEqualTo(1);
  }

  @Test
  void aBrokerThatNeverAnswersTimesOutInsteadOfHangingForever() {
    when(template.send(anyString(), anyString(), anyString()))
        .thenReturn(new CompletableFuture<>());
    KafkaEventPublisher quick = publisher(2, Duration.ofMillis(30));

    long started = System.nanoTime();
    assertThatThrownBy(() -> quick.publish(TestSupport.event()))
        .isInstanceOf(PublishFailedException.class);
    assertThat(Duration.ofNanos(System.nanoTime() - started)).isLessThan(Duration.ofSeconds(3));
    verify(template, times(2)).send(anyString(), anyString(), anyString());
  }

  @Test
  void anExceptionThrownBySendItselfIsAlsoRetried() {
    when(template.send(anyString(), anyString(), anyString()))
        .thenThrow(new IllegalStateException("producer closed"))
        .thenReturn(ok());

    publisher.publish(TestSupport.event());

    assertThat(stats.retries()).isEqualTo(1);
    assertThat(stats.sent()).isEqualTo(1);
  }

  @Test
  void backoffDoublesAndIsCappedAtTheMaximum() {
    assertThat(publisher.backoff(1)).isEqualTo(Duration.ofMillis(200));
    assertThat(publisher.backoff(2)).isEqualTo(Duration.ofMillis(400));
    assertThat(publisher.backoff(3)).isEqualTo(Duration.ofMillis(800));
    assertThat(publisher.backoff(10)).isEqualTo(Duration.ofSeconds(5)); // capped
    assertThat(publisher.backoff(60)).isEqualTo(Duration.ofSeconds(5)); // and no overflow
  }

  @Test
  void interruptionStopsPublishingAndPreservesTheInterruptFlag() {
    when(template.send(anyString(), anyString(), anyString()))
        .thenReturn(new CompletableFuture<>());
    Thread.currentThread().interrupt();
    try {
      assertThatThrownBy(() -> publisher.publish(TestSupport.event()))
          .isInstanceOf(PublishFailedException.class)
          .hasMessageContaining("interrupted");
      assertThat(Thread.currentThread().isInterrupted()).isTrue();
    } finally {
      Thread.interrupted(); // do not leak the flag into other tests
    }
    assertThat(stats.failed()).isEqualTo(1);
  }

  @Test
  void flushDelegatesToTheTemplate() {
    publisher.flush();
    verify(template).flush();
    verify(template, never()).send(anyString(), anyString(), anyString());
  }
}
