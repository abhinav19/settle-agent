package com.settleagent.producer;

import com.fasterxml.jackson.annotation.JsonFormat;
import com.fasterxml.jackson.annotation.JsonProperty;
import java.math.BigDecimal;
import java.time.Instant;
import java.time.LocalDate;
import java.util.Objects;
import java.util.UUID;
import java.util.regex.Pattern;

/**
 * Kafka message contract {@code SettlementFailureEvent} v1.0 (see {@code
 * contracts/settlement_failure_event.schema.json}, generated from the Python model).
 *
 * <p>The constructor enforces the same rules as the Python consumer, so the producer cannot emit a
 * message the pipeline would send to the dead-letter queue. Decimals serialize as JSON strings (no
 * float rounding); timestamps are ISO-8601 UTC.
 */
public record SettlementFailureEvent(
    @JsonProperty("schema_version") String schemaVersion,
    @JsonProperty("event_id") UUID eventId,
    @JsonProperty("emitted_at") Instant emittedAt,
    @JsonProperty("trade_id") String tradeId,
    @JsonProperty("isin") String isin,
    @JsonProperty("settlement_date") LocalDate settlementDate,
    @JsonProperty("counterparty_bic") String counterpartyBic,
    @JsonProperty("reason_code") String reasonCode,
    @JsonProperty("quantity") @JsonFormat(shape = JsonFormat.Shape.STRING) BigDecimal quantity,
    @JsonProperty("cash_amount") @JsonFormat(shape = JsonFormat.Shape.STRING) BigDecimal cashAmount,
    @JsonProperty("currency") String currency,
    @JsonProperty("raw_message") String rawMessage) {

  public static final String SCHEMA_VERSION = "1.0";
  private static final Pattern ISIN = Pattern.compile("^[A-Z]{2}[A-Z0-9]{9}[0-9]$");
  private static final Pattern BIC = Pattern.compile("^[A-Z]{6}[A-Z0-9]{2}([A-Z0-9]{3})?$");
  private static final Pattern CURRENCY = Pattern.compile("^[A-Z]{3}$");

  public SettlementFailureEvent {
    if (!SCHEMA_VERSION.equals(schemaVersion)) {
      throw invalid("schema_version must be " + SCHEMA_VERSION + ", was " + schemaVersion);
    }
    Objects.requireNonNull(eventId, "event_id");
    Objects.requireNonNull(emittedAt, "emitted_at");
    Objects.requireNonNull(settlementDate, "settlement_date");
    if (tradeId == null || tradeId.isBlank() || tradeId.length() > 64) {
      throw invalid("trade_id must be 1-64 characters");
    }
    if (isin == null || !ISIN.matcher(isin).matches() || !isinChecksumOk(isin)) {
      throw invalid("invalid ISIN: " + isin);
    }
    if (counterpartyBic == null || !BIC.matcher(counterpartyBic).matches()) {
      throw invalid("invalid BIC: " + counterpartyBic);
    }
    if (reasonCode == null || !ReasonCodeCatalog.isKnown(reasonCode)) {
      throw invalid("unknown reason_code: " + reasonCode);
    }
    if (quantity == null || quantity.signum() <= 0) {
      throw invalid("quantity must be > 0");
    }
    if (cashAmount == null || cashAmount.signum() < 0) {
      throw invalid("cash_amount must be >= 0");
    }
    if (currency == null || !CURRENCY.matcher(currency).matches()) {
      throw invalid("invalid currency: " + currency);
    }
    if (rawMessage == null || rawMessage.isEmpty()) {
      throw invalid("raw_message must not be empty");
    }
    // Plain decimal text only: BigDecimal.toString() can switch to scientific notation (1E+3)
    // for negative scales, which the consumer would not parse as the intended number.
    quantity = plain(quantity);
    cashAmount = plain(cashAmount);
  }

  private static BigDecimal plain(BigDecimal value) {
    return value.scale() < 0 ? value.setScale(0) : value;
  }

  private static IllegalArgumentException invalid(String message) {
    return new IllegalArgumentException("invalid SettlementFailureEvent: " + message);
  }

  /** ISO 6166 check digit: Luhn over the digits obtained by expanding letters to 10..35. */
  static boolean isinChecksumOk(String isin) {
    StringBuilder digits = new StringBuilder();
    for (char c : isin.toCharArray()) {
      digits.append(Character.digit(c, 36));
    }
    int sum = 0;
    boolean doubleIt = false;
    for (int i = digits.length() - 1; i >= 0; i--) {
      int n = digits.charAt(i) - '0';
      if (doubleIt) {
        n *= 2;
        if (n > 9) {
          n -= 9;
        }
      }
      sum += n;
      doubleIt = !doubleIt;
    }
    return sum % 10 == 0;
  }
}
