# claims.json

```json
{
  "claims": [
    {
      "id": "C01",
      "sentence": "S0004",
      "page": 1,
      "type": "calculation",
      "quote": "operating profit rose 14.8% to EUR 186 million",
      "verdict": "wrong",
      "explanation": "Operating profit rose from EUR 165 million to EUR 186 million. That is 12.7%.",
      "correction": "operating profit rose 12.7% to EUR 186 million",
      "calc": { "expr": "(186-165)/165*100", "stated": 14.8, "decimals": 1 },
      "evidence": [
        { "kind": "document", "page": 1, "quote": "Operating profit 186 165" }
      ]
    },
    {
      "id": "C02",
      "sentence": "S0007",
      "page": 1,
      "type": "external",
      "quote": "answered within 60 days, as Article 12 of the GDPR requires",
      "verdict": "wrong",
      "explanation": "Article 12(3) sets one month.",
      "correction": "answered within one month, as Article 12 of the GDPR requires",
      "evidence": [
        {
          "kind": "source",
          "url": "https://gdpr-info.eu/art-12-gdpr/",
          "title": "GDPR Article 12",
          "date": "2016",
          "quote": "without undue delay and in any event within one month of receipt of the request"
        }
      ]
    }
  ],
  "skipped": [
    { "sentence": "S0011", "reason": "table row used as evidence in C01" }
  ]
}
```

## Rules the gate enforces

- `quote` must appear verbatim on `page`. Typography, line breaks and hyphenation are folded. Words and digits are not.
- A `source` quote must appear in the snapshot from `fetch`.
- Every number in `calc.expr` must appear in the claim quote or a verified evidence quote.
- `wrong` or `inconsistent` with a calc that matches the stated value fails. So does `confirmed` with one that does not match.
- Every verdict except `unverified` needs verified evidence or a calculation.
- `wrong`, `inconsistent` and `misleading` need a `correction`.

`calc` tests the claim as stated. To show the maths behind a correction, put it in `explanation`.

## Verdicts

| Verdict | Use when |
|---|---|
| `confirmed` | Evidence supports the claim as worded |
| `wrong` | Evidence contradicts it |
| `inconsistent` | The document contradicts itself |
| `misleading` | Literally defensible, but the reader gets a false picture |
| `outdated` | True once, a newer figure replaces it |
| `unverified` | You found no proof either way |
