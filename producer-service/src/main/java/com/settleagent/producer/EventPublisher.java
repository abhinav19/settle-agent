package com.settleagent.producer;

/** Sends events somewhere reliable. */
public interface EventPublisher {
  /** Blocks until the broker acknowledged the event, retrying with backoff. */
  void publish(SettlementFailureEvent event) throws PublishFailedException;

  /** Push out anything buffered (called on shutdown). */
  void flush();
}
