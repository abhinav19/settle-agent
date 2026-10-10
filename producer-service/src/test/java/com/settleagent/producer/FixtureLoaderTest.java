package com.settleagent.producer;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.Set;
import java.util.stream.Collectors;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

class FixtureLoaderTest {

  @Test
  void loadsAll300SeededScenariosAsValidEvents() {
    List<SettlementFailureEvent> events = TestSupport.fixtures();
    assertThat(events).hasSize(300);
    assertThat(events.stream().map(SettlementFailureEvent::eventId).distinct()).hasSize(300);
    Set<String> codes =
        events.stream().map(SettlementFailureEvent::reasonCode).collect(Collectors.toSet());
    assertThat(codes)
        .containsExactlyInAnyOrder("LACK", "MONY", "NMAT", "CMIS", "SSIM", "DENO", "LATE", "CALD");
  }

  @Test
  void aMissingFileExplainsHowToCreateIt(@TempDir Path dir) {
    assertThatThrownBy(() -> FixtureLoader.load(dir.resolve("nope.json")))
        .isInstanceOf(IllegalStateException.class)
        .hasMessageContaining("make seed");
  }

  @Test
  void aFileWithoutScenariosIsRejected(@TempDir Path dir) throws Exception {
    Path file = Files.writeString(dir.resolve("empty.json"), "{\"scenarios\": []}");
    assertThatThrownBy(() -> FixtureLoader.load(file)).hasMessageContaining("no scenarios");
  }

  @Test
  void aCorruptedFixtureFailsLoudlyRatherThanEmittingBadEvents(@TempDir Path dir) throws Exception {
    String bad = "{\"scenarios\": [{\"event\": {\"schema_version\": \"1.0\"}}]}";
    Path file = Files.writeString(dir.resolve("bad.json"), bad);
    assertThatThrownBy(() -> FixtureLoader.load(file)).isInstanceOf(RuntimeException.class);
  }
}
