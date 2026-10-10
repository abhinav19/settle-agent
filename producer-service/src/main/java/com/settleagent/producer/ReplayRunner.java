package com.settleagent.producer;

import java.time.Duration;
import java.util.List;
import java.util.function.IntConsumer;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.ApplicationArguments;
import org.springframework.boot.ApplicationRunner;

/**
 * Replay mode: publishes the stored fixtures deterministically (original event ids), optionally
 * throttled, then exits (non-zero if anything could not be published). Used by integration tests,
 * the evaluation harness and demos.
 */
public class ReplayRunner implements ApplicationRunner {

  private static final Logger log = LoggerFactory.getLogger(ReplayRunner.class);

  private final List<SettlementFailureEvent> fixtures;
  private final EventPublisher publisher;
  private final ProducerProperties props;
  private final Sleeper sleeper;
  private final IntConsumer exitHandler;

  public ReplayRunner(
      List<SettlementFailureEvent> fixtures,
      EventPublisher publisher,
      ProducerProperties props,
      Sleeper sleeper,
      IntConsumer exitHandler) {
    this.fixtures = fixtures;
    this.publisher = publisher;
    this.props = props;
    this.sleeper = sleeper;
    this.exitHandler = exitHandler;
  }

  @Override
  public void run(ApplicationArguments args) {
    int failures = replay();
    if (props.exitAfterReplay()) {
      exitHandler.accept(failures == 0 ? 0 : 1);
    }
  }

  /**
   * @return the number of events that could not be published
   */
  int replay() {
    int from = Math.min(Math.max(props.offset(), 0), fixtures.size());
    int to = props.limit() > 0 ? Math.min(from + props.limit(), fixtures.size()) : fixtures.size();
    List<SettlementFailureEvent> batch = fixtures.subList(from, to);
    Duration gap =
        props.rate() > 0 ? Duration.ofNanos((long) (1_000_000_000L / props.rate())) : null;
    log.info("replaying {} event(s) to {}", batch.size(), props.topic());
    int failures = 0;
    for (SettlementFailureEvent event : batch) {
      try {
        publisher.publish(event);
      } catch (PublishFailedException e) {
        failures++;
        log.error("replay: {}", e.getMessage());
      }
      if (gap != null) {
        try {
          sleeper.sleep(gap);
        } catch (InterruptedException e) {
          Thread.currentThread().interrupt();
          break;
        }
      }
    }
    publisher.flush();
    log.info("replay finished: {} published, {} failed", batch.size() - failures, failures);
    return failures;
  }
}
