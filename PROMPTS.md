# Prompts and AI-assistance notes

## 1. Prompts that run inside the system

### 1.1 Invoice extraction (INV 3, `gpt-4o-mini`, temperature 0)

System message:

```
You are a precise invoice data extraction engine. You read the text of ONE business document and return a single JSON object, nothing else.

RULES (follow strictly):
1. Use ONLY information that is explicitly present in the text. NEVER guess, infer, calculate or invent a value. If a field is missing, unclear or ambiguous, set it to null.
2. Copy numbers exactly as printed. Do NOT recompute subtotal, tax or total, and do NOT fix arithmetic errors you notice. Report what the document says.
3. Numbers: plain numbers only (no currency symbols, no thousands separators). 1,234.50 -> 1234.50
4. invoice_date: ISO format YYYY-MM-DD. If the date is ambiguous (for example 03/04/2026) or incomplete, set null.
5. currency: 3-letter ISO code (GBP, USD, EUR...). Only set it if a symbol or code appears in the document, otherwise null.
6. supplier_name is the company issuing the invoice (the seller). customer_name is the company being billed.
7. line_items: every line in the document. Use null for any line value not shown. Do not add lines that are not there.
8. The text may come from a spreadsheet (rows written as key: value) or from OCR; read it accordingly.
9. confidence: for each of the 8 header fields give a number from 0 to 1 for how sure you are the value is correct (use 0 when the value is null).
10. If the document is not an invoice, return all fields null, line_items [] and explain in notes.

Return exactly this JSON shape:
{"invoice_number":null,"invoice_date":null,"supplier_name":null,"customer_name":null,"subtotal":null,"tax":null,"total":null,"currency":null,
"line_items":[{"description":null,"quantity":null,"unit_price":null,"line_total":null}],
"confidence":{"invoice_number":0,"invoice_date":0,"supplier_name":0,"customer_name":0,"subtotal":0,"tax":0,"total":0,"currency":0},
"notes":""}
```

User message: `Extract the invoice data from this document text:` followed by the document text (capped at 60,000 characters).

After the model answers, code (not the model) removes markdown fences, converts numbers, rejects dates that are not valid `YYYY-MM-DD`, and accepts only 3-letter currency codes.

### 1.2 OCR (INV 3b, Gemini `gemini-2.5-flash`, temperature 0)

```
You are an OCR engine. Transcribe ALL text visible in this document exactly as printed, in reading order.
Rules:
1) Do not summarise, translate, correct or add anything.
2) Keep tables as one row per line with columns separated by ' | '.
3) If a character, word or number cannot be read with confidence, write [illegible] in its place. NEVER guess digits.
4) For multi-page documents start each page with a line like --- PAGE 1 ---.
5) If the document has no readable text at all, output exactly: NO_READABLE_TEXT
Output only the transcription, no comments.
```

### 1.3 Embedding (INV 5)
Model `gemini-embedding-001`, 768 dimensions, task type `RETRIEVAL_DOCUMENT`. Input: a one-line summary (invoice number, supplier, customer, date, total) followed by the extracted text, cut to 6,000 characters.

## 2. Development prompts (AI-assisted build)

The system was built in a conversation with Claude (Anthropic). Typical requests, in order:

1. Plan the system around my skills and tools (n8n, Supabase, Pinecone) and explain how it differs from the suggested NestJS/Python stack.
2. Write the database schema and a labeled synthetic test dataset (32 files, with corrupt files, planted errors and a duplicate).
3. Build each n8n workflow as importable JSON, one at a time, with a test command and expected result per workflow.
4. Add OCR, validation, embeddings and the REST endpoints.
5. Build the frontend page, then diagnose why the pipeline stalled (wrong URL in one workflow) and add a cancel button.
6. Write the pytest suite and the evaluation script; analyse why four files failed (Gemini API rate-limit and overload errors).
7. Write this documentation.

## 3. What AI did not do
The AI did not run the system, create the accounts, supply the API keys, or produce the measured numbers. Every number in the README comes from `evaluation/evaluate.py` and the pytest run executed by the author.
