<div align="center">

# AI Fact Checker for Claude

<!-- badges -->
[![python](https://img.shields.io/badge/made_with-python-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![tests](https://img.shields.io/github/actions/workflow/status/BrickyStudios/ai-fact-checker-claude-skill/test.yml?label=tests)](https://github.com/BrickyStudios/ai-fact-checker-claude-skill/actions/workflows/test.yml)
[![Claude Code plugin](https://img.shields.io/badge/Claude_Code-plugin-D97757?logo=claude&logoColor=white)](#install)
[![Last Commit](https://img.shields.io/github/last-commit/BrickyStudios/ai-fact-checker-claude-skill)](https://github.com/BrickyStudios/ai-fact-checker-claude-skill/commits/main)
[![Stars](https://img.shields.io/github/stars/BrickyStudios/ai-fact-checker-claude-skill?style=flat)](https://github.com/BrickyStudios/ai-fact-checker-claude-skill/stargazers)
[![License](https://img.shields.io/github/license/BrickyStudios/ai-fact-checker-claude-skill)](https://github.com/BrickyStudios/ai-fact-checker-claude-skill/blob/main/LICENSE)

**A free, open-source Claude skill that fact-checks reports, PDFs and AI drafts claim by claim.**

</div>

Ask Claude to check a document and it can still invent a quote or get a sum wrong while checking. This skill splits the job. Claude reads, searches and judges. A small Python script proves what code can prove:

- **Quotes:** every quote must appear verbatim in the document or in a saved snapshot of the source.
- **Numbers:** every calculation is recomputed with exact decimals.
- **Coverage:** every sentence with a number must be checked or skipped with a stated reason.

A finding that fails these checks is downgraded to `unverified`, so a claim without proof can't reach the report as a fact.

![Annotated PDF from the AI fact checker with wrong, inconsistent and misleading claims highlighted](examples/nordhaven/annotated-page.png)

## Install

In Claude Code:

```text
/plugin marketplace add BrickyStudios/ai-fact-checker-claude-skill
/plugin install fact-checker@brickystudios
```

Or copy the skill by hand:

```bash
git clone https://github.com/BrickyStudios/ai-fact-checker-claude-skill
cp -r ai-fact-checker-claude-skill/skills/fact-checker ~/.claude/skills/
```

You need Python 3.10+ and, ideally, [uv](https://docs.astral.sh/uv/), which installs the PDF libraries by itself. The skill is free. Claude itself runs on your own plan.

## Use

```text
Fact-check annual-report-2025.pdf
```

Claude extracts the sentences, tells you the expected token cost, checks each claim and writes two files:

- `report.md` with each finding, a proposed correction and the evidence
- an annotated copy of your PDF with the findings highlighted on the page

| Depth | What it does |
|---|---|
| quick | Inside the document only: arithmetic, totals, contradictions |
| standard | quick, plus web checks for the external claims that carry the document |
| deep | Web checks for every external claim |

Works on PDF, HTML, Markdown and plain text. Scanned PDFs need OCR first (`ocrmypdf in.pdf out.pdf`).

## Example

[`examples/nordhaven`](examples/nordhaven) is a fictional annual report with planted errors. A real run gave 7 claims, 6 findings and 100% coverage of number-bearing sentences:

| Claim in the report | Verdict | Why |
|---|---|---|
| Operating profit rose 14.8% | wrong | 165 to 186 is 12.7% |
| Requests answered within 60 days, as GDPR Article 12 requires | wrong | Article 12(3) says one month |
| Workforce grew to 3,120 | inconsistent | The key figures table says 3,210 |
| Food waste has fallen every year since 2021 | inconsistent | The table shows a rise in 2023 |
| Segments sum to the group total of 1,840 | inconsistent | They sum to 1,860, and the eliminations row is missing |
| 410 MWh powers about 12,000 Danish households | misleading | At 4,000 kWh a home it covers about 103 |

Read the [full report](examples/nordhaven/report.md) or open the [annotated PDF](examples/nordhaven/nordhaven-annual-report.annotated.pdf).

## What it can't do

- The script proves a quote exists. It can't prove the source is right, so judging sources stays with Claude and with you.
- Reading images and charts is limited to the text a PDF contains.
- A clean report means the checked claims passed. Treat it as a strong first pass before human review of anything that matters.

## Development

```bash
python3 -m unittest discover tests
```

Issues and pull requests are welcome. MIT licensed, see [LICENSE](LICENSE).
