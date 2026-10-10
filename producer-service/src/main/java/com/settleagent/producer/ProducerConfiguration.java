package com.settleagent.producer;

import java.time.Clock;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.boot.context.properties.EnableConfigurationProperties;
import org.springframework.boot.health.contributor.HealthIndicator;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.kafka.core.KafkaTemplate;

@Configuration
@EnableConfigurationProperties(ProducerProperties.class)
public class ProducerConfiguration {

  @Bean
  Clock clock() {
    return Clock.systemUTC();
  }

  @Bean
  PublishStats publishStats() {
    return new PublishStats();
  }

  @Bean
  EventPublisher eventPublisher(
      KafkaTemplate<String, String> template,
      ProducerProperties props,
      PublishStats stats,
      Clock clock) {
    return new KafkaEventPublisher(template, props, stats, Sleeper.REAL, clock);
  }

  /** Reported at {@code /actuator/health} as the {@code publisher} component. */
  @Bean("publisher")
  HealthIndicator publisherHealth(PublishStats stats, ProducerProperties props) {
    return new PublisherHealthIndicator(stats, props);
  }

  @Bean
  @ConditionalOnProperty(name = "producer.mode", havingValue = "replay")
  ReplayRunner replayRunner(
      EventPublisher publisher,
      ProducerProperties props,
      @Autowired org.springframework.context.ConfigurableApplicationContext context) {
    return new ReplayRunner(
        FixtureLoader.load(props.fixturesPath()),
        publisher,
        props,
        Sleeper.REAL,
        code -> System.exit(org.springframework.boot.SpringApplication.exit(context, () -> code)));
  }

  @Bean
  @ConditionalOnProperty(name = "producer.mode", havingValue = "stream")
  StreamRunner streamRunner(EventPublisher publisher, ProducerProperties props, Clock clock) {
    return new StreamRunner(
        FixtureLoader.load(props.fixturesPath()),
        new SettlementEventGenerator(props.seed(), clock),
        publisher,
        props);
  }
}
