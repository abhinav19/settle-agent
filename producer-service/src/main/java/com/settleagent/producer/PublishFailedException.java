package com.settleagent.producer;

/** Publishing gave up after exhausting its retries. */
public class PublishFailedException extends RuntimeException {
  public PublishFailedException(String message, Throwable cause) {
    super(message, cause);
  }
}
