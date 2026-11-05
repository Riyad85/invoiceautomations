-- Document Intelligence System: Supabase / PostgreSQL schema
create extension if not exists "pgcrypto";

create table documents (
  id            uuid primary key default gen_random_uuid(),
  filename      text not null,
  file_type     text not null,                 -- pdf, jpg, png, tiff, xlsx, xls, csv
  storage_path  text,
  sha256        text,                          -- detects identical re-uploads
  page_count    int,
  status        text not null default 'queued'
                check (status in ('queued','processing','completed','needs_review','failed')),
  error_message text,
  attempts      int not null default 0,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);
create index on documents (status);
create index on documents (sha256);

create table extracted_text (
  document_id uuid primary key references documents(id) on delete cascade,
  method      text not null,                   -- direct_pdf, ocr, spreadsheet
  full_text   text,
  fts         tsvector generated always as (to_tsvector('simple', coalesce(full_text,''))) stored
);
create index on extracted_text using gin (fts);

create table invoices (
  id             uuid primary key default gen_random_uuid(),
  document_id    uuid not null references documents(id) on delete cascade,
  invoice_number text,
  invoice_date   date,
  supplier_name  text,
  customer_name  text,
  subtotal       numeric(14,2),
  tax            numeric(14,2),
  total          numeric(14,2),
  currency       text,
  needs_review   boolean not null default false,
  confidence     jsonb,                        -- per-field confidence from the LLM
  raw_json       jsonb,                        -- exact model output, for audit
  created_at     timestamptz not null default now()
);
-- NOT unique on purpose: duplicates must be stored and flagged, not rejected.
create index on invoices (supplier_name, invoice_number);
create index on invoices (invoice_date);
create index on invoices (needs_review);

create table line_items (
  id          uuid primary key default gen_random_uuid(),
  invoice_id  uuid not null references invoices(id) on delete cascade,
  position    int,
  description text,
  quantity    numeric(12,3),
  unit_price  numeric(14,2),
  line_total  numeric(14,2)
);
create index on line_items (invoice_id);

create table validation_errors (
  id          uuid primary key default gen_random_uuid(),
  invoice_id  uuid references invoices(id) on delete cascade,
  document_id uuid not null references documents(id) on delete cascade,
  code        text not null,   -- MISSING_FIELD, TOTAL_MISMATCH, LINE_SUM_MISMATCH, DUPLICATE_INVOICE, BAD_NUMBER, BAD_DATE
  severity    text not null check (severity in ('warning','error')),
  field       text,
  message     text not null,
  resolved    boolean not null default false,
  created_at  timestamptz not null default now()
);
create index on validation_errors (document_id);

create table corrections (
  id           uuid primary key default gen_random_uuid(),
  invoice_id   uuid not null references invoices(id) on delete cascade,
  field        text not null,
  old_value    text,
  new_value    text,
  corrected_at timestamptz not null default now()
);

create table processing_history (
  id          uuid primary key default gen_random_uuid(),
  document_id uuid not null references documents(id) on delete cascade,
  step        text not null,    -- upload, route, text_extraction, ocr, llm_extraction, validation, embedding, reprocess
  status      text not null,    -- started, ok, failed
  detail      text,
  duration_ms int,
  created_at  timestamptz not null default now()
);
create index on processing_history (document_id, created_at);

-- Keep updated_at fresh
create or replace function touch_updated_at() returns trigger as $$
begin new.updated_at = now(); return new; end; $$ language plpgsql;
create trigger documents_touch before update on documents
for each row execute function touch_updated_at();
