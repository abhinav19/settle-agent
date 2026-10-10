package com.settleagent.producer;

import java.util.List;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.context.SmartLifecycle;

/**
 * Stream mode (demo): continuously re-issues random fixtures as NEW failures at {@code rate}
 * events/second until the application stops. A failed publish is counted and logged, never allowed
 * to kill the schedule. Stops gracefully: no new events, in-flight send finishes, flush.
 */
public class StreamRunner implements SmartLifecycle {

  private static final Logger log = LoggerFactory.getLogger(StreamRunner.class);

  private final List<SettlementFailureEvent> fixtures;
  private final SettlementEventGenerator generator;
  private final EventPublisher publisher;
  private final ProducerProperties props;
  private ScheduledExecutorService executor;
  private volatile boolean running;

  public StreamRunner(
      List<SettlementFailureEvent> fixtures,
      SettlementEventGenerator generator,
      EventPublisher publisher,
      ProducerProperties props) {
    this.fixtures = fixtures;
    this.generator = generator;
    this.publisher = publisher;
    this.props = props;
    if (props.rate() <= 0) {
      throw new IllegalArgumentException("producer.rate must be > 0 in stream mode");
    }
  }

  /** Nanoseconds between events for a rate in events/second. */
  static long periodNanos(double ratePerSecond) {
    return Math.max(1L, (long) (1_000_000_000L / ratePerSecond));
  }

  /** One scheduled emission. Public for tests; never throws. */
  public boolean tick() {
    if (!running) {
      return false;
    }
    try {
      publisher.publish(generator.fresh(generator.pick(fixtures)));
      return true;
    } catch (RuntimeException e) { // PublishFailedException or a bug: keep the stream alive
      log.error("stream: could not publish ({})", e.toString());
      return false;
    }
  }

  @Override
  public synchronized void start() {
    if (running) {
      return;
    }
    running = true;
    executor = Executors.newSingleThreadScheduledExecutor(r -> new Thread(r, "event-stream"));
    long period = periodNanos(props.rate());
    executor.scheduleAtFixedRate(this::tick, 0, period, TimeUnit.NANOSECONDS);
    log.info("streaming {} event/s to {}", props.rate(), props.topic());
  }

  @Override
  public synchronized void stop() {
    if (!running) {
      return;
    }
    running = false;
    executor.shutdown();
    try {
      if (!executor.awaitTermination(10, TimeUnit.SECONDS)) {
        executor.shutdownNow();
      }
    } catch (InterruptedException e) {
      executor.shutdownNow();
      Thread.currentThread().interrupt();
    }
    publisher.flush();
    log.info("stream stopped");
  }

  @Override
  public boolean isRunning() {
    return running;
  }
}
