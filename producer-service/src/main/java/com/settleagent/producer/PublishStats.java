package com.settleagent.producer;

import java.time.Instant;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicLong;
import java.util.concurrent.atomic.AtomicReference;

/** Thread-safe publish counters; the health indicator reads them. */
public final class PublishStats {

  private final AtomicLong sent = new AtomicLong();
  private final AtomicLong failed = new AtomicLong();
  private final AtomicLong retries = new AtomicLong();
  private final AtomicInteger consecutiveFailures = new AtomicInteger();
  private final AtomicReference<Instant> lastSuccess = new AtomicReference<>();

  void success(Instant now) {
    sent.incrementAndGet();
    consecutiveFailures.set(0);
    lastSuccess.set(now);
  }

  void failure() {
    failed.incrementAndGet();
    consecutiveFailures.incrementAndGet();
  }

  void retry() {
    retries.incrementAndGet();
  }

  public long sent() {
    return sent.get();
  }

  public long failed() {
    return failed.get();
  }

  public long retries() {
    return retries.get();
  }

  public int consecutiveFailures() {
    return consecutiveFailures.get();
  }

  public Instant lastSuccess() {
    return lastSuccess.get();
  }
}
