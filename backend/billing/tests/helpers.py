"""Shared test data for billing: synthetic values only, never anything real."""

# Synthetic seller: a documentation IBAN (valid check digits, not a real account) and made-up ids.
SELLER = {
    "BILLING_SELLER_NAME": "Example Seller",
    "BILLING_SELLER_ADDRESS": "Example Street 1|12345 Example City",
    "BILLING_SELLER_TAX_NUMBER": "00/000/00000",
    "BILLING_SELLER_VAT_ID": "DE000000000",
    "BILLING_SELLER_EMAIL": "billing@example.test",
    "BILLING_SELLER_IBAN": "DE89 3704 0044 0532 0130 00",
    "BILLING_SELLER_BIC": "COBADEFFXXX",
    "BILLING_SELLER_BANK": "Example Bank",
    "BILLING_SMALL_BUSINESS": "false",
}
