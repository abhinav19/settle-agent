package com.settleagent.producer;

import static org.assertj.core.api.Assertions.assertThat;

import java.time.Instant;
import org.junit.jupiter.api.Test;
import org.springframework.boot.health.contributor.Health;
import org.springframework.boot.health.contributor.Status;

class PublisherHealthIndicatorTest {

  private final PublishStats stats = new PublishStats();
  private final PublisherHealthIndicator indicator =
      new PublisherHealthIndicator(stats, TestSupport.props(3, java.time.Duration.ofSeconds(1)));

  @Test
  void isUpWithDetailsBeforeAnythingHappens() {
    Health health = indicator.health();
    assertThat(health.getStatus()).isEqualTo(Status.UP);
    assertThat(health.getDetails()).containsEntry("sent", 0L).containsEntry("failed", 0L);
    assertThat(health.getDetails()).doesNotContainKey("lastSuccess");
  }

  @Test
  void goesDownAfterTheConfiguredNumberOfConsecutiveFailuresAndRecovers() {
    stats.failure();
    stats.failure();
    assertThat(indicator.health().getStatus()).isEqualTo(Status.UP); // 2 < 3
    stats.failure();
    Health down = indicator.health();
    assertThat(down.getStatus()).isEqualTo(Status.DOWN);
    assertThat(down.getDetails()).containsEntry("consecutiveFailures", 3);

    stats.success(Instant.parse("2026-10-10T12:00:00Z")); // one good publish clears it
    Health recovered = indicator.health();
    assertThat(recovered.getStatus()).isEqualTo(Status.UP);
    assertThat(recovered.getDetails())
        .containsEntry("lastSuccess", "2026-10-10T12:00:00Z")
        .containsEntry("sent", 1L);
  }
}
