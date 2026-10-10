package com.settleagent.producer;

import org.springframework.boot.health.contributor.Health;
import org.springframework.boot.health.contributor.HealthIndicator;

/** DOWN once publishing has failed {@code unhealthyAfterFailures} times in a row. */
public class PublisherHealthIndicator implements HealthIndicator {

  private final PublishStats stats;
  private final ProducerProperties props;

  public PublisherHealthIndicator(PublishStats stats, ProducerProperties props) {
    this.stats = stats;
    this.props = props;
  }

  @Override
  public Health health() {
    Health.Builder builder =
        stats.consecutiveFailures() >= props.unhealthyAfterFailures() ? Health.down() : Health.up();
    builder
        .withDetail("mode", props.mode())
        .withDetail("sent", stats.sent())
        .withDetail("failed", stats.failed())
        .withDetail("retries", stats.retries())
        .withDetail("consecutiveFailures", stats.consecutiveFailures());
    if (stats.lastSuccess() != null) {
      builder.withDetail("lastSuccess", stats.lastSuccess().toString());
    }
    return builder.build();
  }
}
