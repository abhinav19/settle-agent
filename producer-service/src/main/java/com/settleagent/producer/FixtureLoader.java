package com.settleagent.producer;

import java.io.IOException;
import java.io.UncheckedIOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import tools.jackson.databind.JsonNode;

/**
 * Loads the seeded synthetic scenarios ({@code data/synthetic/dataset.json}). Events built from
 * these resolve in the MCP servers' reference data, which is why the producer samples fixtures
 * instead of inventing trades.
 */
public final class FixtureLoader {

  private FixtureLoader() {}

  public static List<SettlementFailureEvent> load(Path datasetFile) {
    if (!Files.isRegularFile(datasetFile)) {
      throw new IllegalStateException(
          "fixtures not found at " + datasetFile.toAbsolutePath() + " (run `make seed`)");
    }
    try {
      JsonNode root = EventJson.mapper().readTree(Files.readString(datasetFile));
      JsonNode scenarios = root.get("scenarios");
      if (scenarios == null || !scenarios.isArray() || scenarios.isEmpty()) {
        throw new IllegalStateException(datasetFile + " has no scenarios");
      }
      List<SettlementFailureEvent> events = new ArrayList<>();
      for (JsonNode scenario : scenarios) {
        events.add(
            EventJson.mapper().treeToValue(scenario.get("event"), SettlementFailureEvent.class));
      }
      return List.copyOf(events);
    } catch (IOException e) {
      throw new UncheckedIOException("cannot read " + datasetFile, e);
    }
  }
}
