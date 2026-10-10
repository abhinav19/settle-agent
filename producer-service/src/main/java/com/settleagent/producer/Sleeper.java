package com.settleagent.producer;

import java.time.Duration;

/** Injectable so retry/backoff tests run instantly and exactly. */
@FunctionalInterface
public interface Sleeper {
  void sleep(Duration duration) throws InterruptedException;

  Sleeper REAL = d -> Thread.sleep(d.toMillis());
}
