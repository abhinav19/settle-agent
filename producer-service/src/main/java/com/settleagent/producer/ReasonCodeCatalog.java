package com.settleagent.producer;

import java.util.Arrays;
import java.util.Optional;

/**
 * The failure reason codes the producer may emit, modeled on ISO 20022 semt.018 / MT548 failing
 * reasons. This MUST stay in step with the Python {@code ReasonCode} enum (the contract test fails
 * if it drifts) and be reconciled with the official ISO 20022 external code lists.
 */
public enum ReasonCodeCatalog {
  LACK("Insufficient securities"),
  MONY("Insufficient cash"),
  NMAT("Unmatched / economic detail mismatch"),
  CMIS("Counterparty instruction missing"),
  SSIM("Standing settlement instruction mismatch"),
  DENO("Denomination / quantity issue"),
  LATE("Late instruction"),
  CALD("Settlement date on market holiday");

  private final String meaning;

  ReasonCodeCatalog(String meaning) {
    this.meaning = meaning;
  }

  public String code() {
    return name();
  }

  public String meaning() {
    return meaning;
  }

  public static Optional<ReasonCodeCatalog> fromCode(String code) {
    return Arrays.stream(values()).filter(r -> r.name().equals(code)).findFirst();
  }

  public static boolean isKnown(String code) {
    return fromCode(code).isPresent();
  }
}
