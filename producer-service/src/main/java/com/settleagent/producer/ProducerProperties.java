package com.settleagent.producer;

import java.nio.file.Path;
import java.time.Duration;
import org.springframework.boot.context.properties.ConfigurationProperties;
import org.springframework.boot.context.properties.bind.DefaultValue;

/** {@code producer.*} configuration (12-factor: every value can come from the environment). */
@ConfigurationProperties("producer")
public record ProducerProperties(
    /* idle | replay | stream */
    @DefaultValue("idle") String mode,
    @DefaultValue("settlement.failures") String topic,
    @DefaultValue("../data/synthetic/dataset.json") String fixtures,
    /* replay: skip the first N fixtures / stop after N (0 = all) */
    @DefaultValue("0") int offset,
    @DefaultValue("0") int limit,
    /* events per second: replay throttle (0 = unthrottled) / stream rate */
    @DefaultValue("0") double rate,
    @DefaultValue("42") long seed,
    @DefaultValue("true") boolean exitAfterReplay,
    @DefaultValue("5") int maxAttempts,
    @DefaultValue("200ms") Duration baseBackoff,
    @DefaultValue("5s") Duration maxBackoff,
    @DefaultValue("10s") Duration sendTimeout,
    @DefaultValue("5") int unhealthyAfterFailures) {

  /** Plain string binding: Spring's Path converter would treat a relative path as a resource. */
  public Path fixturesPath() {
    return Path.of(fixtures);
  }
}
