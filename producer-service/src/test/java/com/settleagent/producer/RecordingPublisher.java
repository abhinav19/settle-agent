package com.settleagent.producer;

import java.util.List;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.function.Predicate;

/** Test double: records what it was asked to publish; can be told to fail some events. */
class RecordingPublisher implements EventPublisher {

  final List<SettlementFailureEvent> published = new CopyOnWriteArrayList<>();
  final AtomicInteger flushes = new AtomicInteger();
  Predicate<SettlementFailureEvent> failWhen = e -> false;

  @Override
  public void publish(SettlementFailureEvent event) {
    if (failWhen.test(event)) {
      throw new PublishFailedException("simulated failure for " + event.eventId(), null);
    }
    published.add(event);
  }

  @Override
  public void flush() {
    flushes.incrementAndGet();
  }
}
