package com.settleagent.producer;

import tools.jackson.databind.json.JsonMapper;

/** The single place events become JSON, so the wire format is defined exactly once. */
public final class EventJson {

  private static final JsonMapper MAPPER = JsonMapper.builder().build();

  private EventJson() {}

  public static String toJson(SettlementFailureEvent event) {
    return MAPPER.writeValueAsString(event);
  }

  public static SettlementFailureEvent fromJson(String json) {
    return MAPPER.readValue(json, SettlementFailureEvent.class);
  }

  public static JsonMapper mapper() {
    return MAPPER;
  }
}
