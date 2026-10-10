package com.settleagent.producer;

import static org.assertj.core.api.Assertions.assertThat;

import java.nio.file.Files;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.List;
import org.junit.jupiter.api.Test;
import tools.jackson.databind.JsonNode;

class ReasonCodeCatalogTest {

  @Test
  void offersExactlyTheEightSeedCodes() {
    assertThat(Arrays.stream(ReasonCodeCatalog.values()).map(ReasonCodeCatalog::code))
        .containsExactlyInAnyOrder("LACK", "MONY", "NMAT", "CMIS", "SSIM", "DENO", "LATE", "CALD");
  }

  /** The Python enum is the source of truth: the generated schema lists its values. */
  @Test
  void matchesTheCodesInTheGeneratedContractSchema() throws Exception {
    JsonNode schema = EventJson.mapper().readTree(Files.readString(TestSupport.SCHEMA));
    List<String> inSchema = new ArrayList<>();
    schema.get("$defs").get("ReasonCode").get("enum").forEach(n -> inSchema.add(n.asString()));
    List<String> inJava =
        new ArrayList<>(Arrays.stream(ReasonCodeCatalog.values()).map(Enum::name).toList());
    Collections.sort(inSchema);
    Collections.sort(inJava);
    assertThat(inJava).isEqualTo(inSchema);
  }

  @Test
  void looksCodesUpSafely() {
    assertThat(ReasonCodeCatalog.fromCode("SSIM")).contains(ReasonCodeCatalog.SSIM);
    assertThat(ReasonCodeCatalog.fromCode("ssim")).isEmpty(); // case-sensitive, like the consumer
    assertThat(ReasonCodeCatalog.fromCode(null)).isEmpty();
    assertThat(ReasonCodeCatalog.isKnown("XXXX")).isFalse();
    assertThat(ReasonCodeCatalog.CALD.meaning()).contains("Settlement date");
  }
}
