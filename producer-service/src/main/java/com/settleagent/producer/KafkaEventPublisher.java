package com.settleagent.producer;

import java.time.Clock;
import java.time.Duration;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.kafka.core.KafkaTemplate;

/**
 * Publishes to Kafka keyed by {@code trade_id} (ordering per trade). Each event is acknowledged
 * (acks=all, idempotent producer) before {@link #publish} returns; transient failures are retried
 * with exponential backoff, then surfaced as {@link PublishFailedException}.
 */
public class KafkaEventPublisher implements EventPublisher {

  private static final Logger log = LoggerFactory.getLogger(KafkaEventPublisher.class);

  private final KafkaTemplate<String, String> template;
  private final ProducerProperties props;
  private final PublishStats stats;
  private final Sleeper sleeper;
  private final Clock clock;

  public KafkaEventPublisher(
      KafkaTemplate<String, String> template,
      ProducerProperties props,
      PublishStats stats,
      Sleeper sleeper,
      Clock clock) {
    this.template = template;
    this.props = props;
    this.stats = stats;
    this.sleeper = sleeper;
    this.clock = clock;
  }

  @Override
  public void publish(SettlementFailureEvent event) {
    String json = EventJson.toJson(event); // a serialization bug is not transient: let it throw
    Exception last = null;
    for (int attempt = 1; attempt <= props.maxAttempts(); attempt++) {
      try {
        template
            .send(props.topic(), event.tradeId(), json)
            .get(props.sendTimeout().toMillis(), TimeUnit.MILLISECONDS);
        stats.success(clock.instant());
        return;
      } catch (InterruptedException e) {
        Thread.currentThread().interrupt();
        stats.failure();
        throw new PublishFailedException("interrupted while publishing " + event.eventId(), e);
      } catch (ExecutionException | TimeoutException | RuntimeException e) {
        last = e;
        log.warn(
            "publish of {} failed (attempt {}/{}): {}",
            event.eventId(),
            attempt,
            props.maxAttempts(),
            e.toString());
        if (attempt < props.maxAttempts()) {
          stats.retry();
          pause(backoff(attempt));
        }
      }
    }
    stats.failure();
    throw new PublishFailedException(
        "gave up publishing " + event.eventId() + " after " + props.maxAttempts() + " attempts",
        last);
  }

  /** base * 2^(attempt-1), capped at maxBackoff. */
  Duration backoff(int attempt) {
    long millis = props.baseBackoff().toMillis() << Math.min(attempt - 1, 20);
    return Duration.ofMillis(Math.min(millis, props.maxBackoff().toMillis()));
  }

  private void pause(Duration d) {
    try {
      sleeper.sleep(d);
    } catch (InterruptedException e) {
      Thread.currentThread().interrupt();
      throw new PublishFailedException("interrupted during backoff", e);
    }
  }

  @Override
  public void flush() {
    template.flush();
  }
}
