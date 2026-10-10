package com.settleagent.producer;

import static org.assertj.core.api.Assertions.assertThat;

import java.time.Duration;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Properties;
import java.util.Set;
import java.util.UUID;
import org.apache.kafka.clients.consumer.ConsumerConfig;
import org.apache.kafka.clients.consumer.ConsumerRecord;
import org.apache.kafka.clients.consumer.KafkaConsumer;
import org.apache.kafka.common.serialization.StringDeserializer;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.context.SmartLifecycle;
import org.springframework.kafka.test.context.EmbeddedKafka;

/** Stream mode against a real broker: new events keep arriving, are valid, and stop on shutdown. */
@SpringBootTest(
    properties = {
      "producer.mode=stream",
      "producer.rate=200",
      "spring.kafka.bootstrap-servers=${spring.embedded.kafka.brokers}"
    })
@EmbeddedKafka(partitions = 1, topics = "settlement.failures")
class ProducerStreamIntegrationTest {

  @Autowired StreamRunner runner;
  @Autowired PublishStats stats;

  @Test
  void streamsValidFreshEventsContinuouslyAndStopsGracefully() {
    List<ConsumerRecord<String, String>> records = consume(50);

    assertThat(records.size()).isGreaterThanOrEqualTo(50);
    Set<String> eventIds = new HashSet<>();
    for (ConsumerRecord<String, String> r : records) {
      assertThat(TestSupport.contractErrors(r.value())).isEmpty();
      assertThat(eventIds.add(EventJson.fromJson(r.value()).eventId().toString()))
          .as("every streamed event is new")
          .isTrue();
    }
    assertThat(runner).isInstanceOf(SmartLifecycle.class);
    assertThat(runner.isRunning()).isTrue();
    assertThat(stats.sent()).isGreaterThanOrEqualTo(50);
    assertThat(stats.consecutiveFailures()).isZero();

    runner.stop(); // what the application does on SIGTERM
    long sentAtStop = stats.sent();
    sleep(150);
    assertThat(stats.sent()).as("nothing is published after stop").isEqualTo(sentAtStop);
    assertThat(runner.isRunning()).isFalse();
  }

  private static void sleep(long millis) {
    try {
      Thread.sleep(millis);
    } catch (InterruptedException e) {
      Thread.currentThread().interrupt();
    }
  }

  private static List<ConsumerRecord<String, String>> consume(int expected) {
    Properties props = new Properties();
    props.put(
        ConsumerConfig.BOOTSTRAP_SERVERS_CONFIG,
        System.getProperty("spring.embedded.kafka.brokers"));
    props.put(ConsumerConfig.GROUP_ID_CONFIG, "it-" + UUID.randomUUID());
    props.put(ConsumerConfig.AUTO_OFFSET_RESET_CONFIG, "earliest");
    props.put(ConsumerConfig.KEY_DESERIALIZER_CLASS_CONFIG, StringDeserializer.class);
    props.put(ConsumerConfig.VALUE_DESERIALIZER_CLASS_CONFIG, StringDeserializer.class);
    List<ConsumerRecord<String, String>> out = new ArrayList<>();
    try (KafkaConsumer<String, String> consumer = new KafkaConsumer<>(props)) {
      consumer.subscribe(List.of("settlement.failures"));
      long deadline = System.nanoTime() + Duration.ofSeconds(30).toNanos();
      while (out.size() < expected && System.nanoTime() < deadline) {
        consumer.poll(Duration.ofMillis(200)).forEach(out::add);
      }
    }
    return out;
  }
}
