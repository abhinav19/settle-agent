package com.settleagent.producer;

import static org.assertj.core.api.Assertions.assertThat;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.Properties;
import java.util.UUID;
import org.apache.kafka.clients.consumer.ConsumerConfig;
import org.apache.kafka.clients.consumer.ConsumerRecord;
import org.apache.kafka.clients.consumer.KafkaConsumer;
import org.apache.kafka.common.serialization.StringDeserializer;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.kafka.test.context.EmbeddedKafka;

/** The whole service, in replay mode, against a real (embedded) Kafka broker. */
@SpringBootTest(
    webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT,
    properties = {
      "producer.mode=replay",
      "producer.limit=30",
      "producer.exit-after-replay=false",
      "spring.kafka.bootstrap-servers=${spring.embedded.kafka.brokers}"
    })
@EmbeddedKafka(partitions = 1, topics = "settlement.failures")
class ProducerIntegrationTest {

  @Value("${local.server.port}")
  int port;

  @Test
  void publishesTheFixturesAsContractValidMessagesKeyedByTradeId() {
    List<ConsumerRecord<String, String>> records = consume(30);

    assertThat(records).hasSize(30);
    List<SettlementFailureEvent> fixtures = TestSupport.fixtures().subList(0, 30);
    for (int i = 0; i < 30; i++) {
      ConsumerRecord<String, String> record = records.get(i);
      assertThat(TestSupport.contractErrors(record.value())).as(record.key()).isEmpty();
      assertThat(record.key()).isEqualTo(fixtures.get(i).tradeId());
      assertThat(EventJson.fromJson(record.value())).isEqualTo(fixtures.get(i));
    }
  }

  @Test
  void healthEndpointReportsTheProducerAsUpWithItsCounters() throws Exception {
    HttpResponse<String> response = get("/actuator/health");
    assertThat(response.statusCode()).isEqualTo(200);
    assertThat(response.body()).contains("\"status\":\"UP\"").contains("\"publisher\"");
    assertThat(response.body()).contains("\"sent\":30").contains("\"mode\":\"replay\"");
  }

  @Test
  void exposesLivenessAndReadinessProbes() throws Exception {
    assertThat(get("/actuator/health/liveness").body()).contains("UP");
    assertThat(get("/actuator/health/readiness").body()).contains("UP");
  }

  private HttpResponse<String> get(String path) throws Exception {
    return HttpClient.newHttpClient()
        .send(
            HttpRequest.newBuilder(URI.create("http://localhost:" + port + path)).build(),
            HttpResponse.BodyHandlers.ofString());
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
